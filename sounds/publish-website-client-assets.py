#!/usr/bin/env python3
import argparse
import base64
import datetime
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from zipfile import ZIP_DEFLATED, ZipFile


TRACKED_DIRECTORIES = ("assets", "bin", "conf", "sounds")
PORTABLE_DIRECTORIES = ("3rdpartylicences", "assets", "bin", "conf", "sounds")
EXCLUDED_CLIENT_FILES = {
    "sounds/install-vps-client-hook.ps1",
    "sounds/install-vps-client-hook.sh",
    "sounds/publish-website-client-assets.ps1",
    "sounds/publish-website-client-assets.py",
}
METADATA_FILE_NAMES = (
    "package.json",
    "package.json.version",
    "assets.json",
    "assets.json.sha256",
    "version.txt",
)
VERSION_SUFFIX_PATTERN = re.compile(r"^(.*)-[0-9a-f]{7,40}$", re.IGNORECASE)
LFS_POINTER_PREFIX = "version https://git-lfs.github.com/spec/v1"
LFS_POINTER_OID_PATTERN = re.compile(r"^oid sha256:([0-9a-f]{64})$", re.IGNORECASE)
LFS_POINTER_SIZE_PATTERN = re.compile(r"^size ([0-9]+)$")
GITHUB_HOSTS = {"github.com", "www.github.com"}
HTTP_DOWNLOAD_TIMEOUT_SECONDS = 30 * 60


def parse_args() -> argparse.Namespace:
    script_root = Path(__file__).resolve().parent
    client_root_default = script_root.parent
    parser = argparse.ArgumentParser(
        description="Publish website client feed and portable package from the client repository."
    )
    parser.add_argument(
        "--client-root",
        default=str(client_root_default),
        help="Path to the client repository root.",
    )
    parser.add_argument(
        "--website-root",
        default="",
        help="Path to the website repository root.",
    )
    parser.add_argument(
        "--version",
        default="auto",
        help='Published client version. Defaults to "auto".',
    )
    parser.add_argument(
        "--rebuild-metadata",
        action="store_true",
        help="Rebuild package.json and assets.json from the raw client files.",
    )
    return parser.parse_args()


def ensure_path_exists(path: Path, label: str) -> None:
    if not path.exists():
        raise RuntimeError(f"{label} not found: {path}")


def is_website_root(path: Path) -> bool:
    return (path / "system" / "pages" / "downloadclient.php").is_file()


def resolve_website_root(client_root: Path, provided_root: str) -> Path:
    if provided_root:
        website_root = Path(provided_root).resolve()
        if not is_website_root(website_root):
            raise RuntimeError(
                f"Website root does not look correct: {website_root}. "
                "Expected system/pages/downloadclient.php."
            )
        return website_root

    candidates = (
        client_root.parent / "ultima-myaac",
        client_root.parent / "www",
        client_root.parent / "UniServerZ" / "www",
    )
    for candidate in candidates:
        if is_website_root(candidate):
            return candidate.resolve()

    raise RuntimeError(
        "Unable to infer the website root. Pass --website-root "
        "(for example /home/penultima/ultima-myaac)."
    )


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def get_sha256_hex(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_candidate_tracked_files(source_root: Path):
    for directory_name in TRACKED_DIRECTORIES:
        directory_path = source_root / directory_name
        if not directory_path.exists():
            continue
        for file_path in sorted(path for path in directory_path.rglob("*") if path.is_file()):
            relative_path = file_path.relative_to(source_root).as_posix()
            if relative_path in EXCLUDED_CLIENT_FILES:
                continue
            yield file_path, relative_path


def parse_lfs_pointer(path: Path):
    try:
        if path.stat().st_size > 1024:
            return None
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines or lines[0] != LFS_POINTER_PREFIX:
        return None

    oid = None
    size = None
    for line in lines[1:]:
        oid_match = LFS_POINTER_OID_PATTERN.match(line)
        if oid_match:
            oid = oid_match.group(1).lower()
            continue
        size_match = LFS_POINTER_SIZE_PATTERN.match(line)
        if size_match:
            size = int(size_match.group(1))

    if not oid or size is None:
        return None

    return {
        "oid": oid,
        "size": size,
    }


def find_lfs_pointer_files(source_root: Path):
    pointer_files = []
    for file_path, relative_path in iter_candidate_tracked_files(source_root):
        pointer_info = parse_lfs_pointer(file_path)
        if not pointer_info:
            continue
        pointer_files.append(
            {
                "relative_path": relative_path,
                "source_path": file_path,
                "oid": pointer_info["oid"],
                "size": pointer_info["size"],
            }
        )
    return pointer_files


def git_lfs_is_available(repository_root: Path) -> bool:
    try:
        subprocess.run(
            ["git", "-C", str(repository_root), "lfs", "version"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def get_git_remote_url(repository_root: Path, remote_name: str = "origin") -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repository_root), "remote", "get-url", remote_name],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(
            f"Unable to resolve git remote '{remote_name}' in {repository_root}: {error}"
        ) from error


def parse_github_remote(remote_url: str):
    remote_url = remote_url.strip()
    ssh_match = re.match(r"^(?P<user>[^@]+)@(?P<host>[^:]+):(?P<path>.+)$", remote_url)
    if ssh_match:
        host = ssh_match.group("host").lower()
        if host not in GITHUB_HOSTS:
            raise RuntimeError(f"Unsupported SSH git host for LFS hydration: {host}")
        repo_path = ssh_match.group("path").lstrip("/")
        if not repo_path.endswith(".git"):
            repo_path += ".git"
        return {
            "transport": "ssh",
            "host": host,
            "user": ssh_match.group("user"),
            "repo_path": repo_path,
        }

    parsed = urllib_parse.urlparse(remote_url)
    if parsed.scheme in {"ssh", "git+ssh"}:
        host = (parsed.hostname or "").lower()
        if host not in GITHUB_HOSTS:
            raise RuntimeError(f"Unsupported SSH git host for LFS hydration: {host}")
        repo_path = parsed.path.lstrip("/")
        if not repo_path.endswith(".git"):
            repo_path += ".git"
        return {
            "transport": "ssh",
            "host": host,
            "user": parsed.username or "git",
            "repo_path": repo_path,
        }

    if parsed.scheme in {"https", "http"}:
        host = (parsed.hostname or "").lower()
        if host not in GITHUB_HOSTS:
            raise RuntimeError(f"Unsupported HTTPS git host for LFS hydration: {host}")
        repo_path = parsed.path.lstrip("/")
        if not repo_path.endswith(".git"):
            repo_path += ".git"
        return {
            "transport": "https",
            "host": host,
            "repo_path": repo_path,
            "username": urllib_parse.unquote(parsed.username or ""),
            "password": urllib_parse.unquote(parsed.password or ""),
        }

    raise RuntimeError(f"Unsupported git remote format for LFS hydration: {remote_url}")


def get_git_credentials_via_helper(remote_info, repository_root: Path):
    if remote_info["transport"] != "https":
        return None

    if remote_info.get("username") and remote_info.get("password"):
        return remote_info["username"], remote_info["password"]

    credential_input = (
        f"url=https://{remote_info['host']}/{remote_info['repo_path']}\n\n"
    ).encode("utf-8")
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_root), "credential", "fill"],
            input=credential_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    username = ""
    password = ""
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "username":
            username = value
        elif key == "password":
            password = value

    if username and password:
        return username, password
    return None


def build_authorization_headers(remote_info, repository_root: Path):
    if remote_info["transport"] != "https":
        return None

    credentials = get_git_credentials_via_helper(remote_info, repository_root)
    if not credentials:
        return None

    username, password = credentials
    token_bytes = f"{username}:{password}".encode("utf-8")
    return {
        "Authorization": "Basic "
        + base64.b64encode(token_bytes).decode("ascii"),
    }


def request_json(url: str, body: dict, headers=None):
    data = json.dumps(body).encode("utf-8")
    request_headers = {
        "Accept": "application/vnd.git-lfs+json",
        "Content-Type": "application/vnd.git-lfs+json",
    }
    if headers:
        request_headers.update(headers)
    request = urllib_request.Request(url, data=data, headers=request_headers, method="POST")
    with urllib_request.urlopen(request, timeout=HTTP_DOWNLOAD_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def authenticate_github_lfs_over_ssh(remote_info):
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        f"{remote_info['user']}@{remote_info['host']}",
        "git-lfs-authenticate",
        remote_info["repo_path"],
        "download",
    ]
    try:
        output = subprocess.check_output(
            command,
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        details = ""
        if isinstance(error, subprocess.CalledProcessError) and error.output:
            details = error.output.strip()
        if details:
            details = f" {details}"
        raise RuntimeError(
            "Unable to authenticate GitHub LFS over SSH."
            f"{details}"
        ) from error

    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"GitHub SSH LFS authentication returned invalid JSON: {output}"
        ) from error

    href = payload.get("href", "").rstrip("/")
    if not href:
        raise RuntimeError("GitHub SSH LFS authentication did not return an href.")
    return href, payload.get("header") or {}


def build_lfs_batch_endpoint_candidates(auth_href: str, remote_info):
    candidates = []
    if auth_href.endswith("/objects/batch"):
        candidates.append(auth_href)
    else:
        candidates.append(auth_href + "/objects/batch")
        candidates.append(auth_href + "/info/lfs/objects/batch")
        candidates.append(auth_href + ".git/info/lfs/objects/batch")

    repo_https_base = f"https://{remote_info['host']}/{remote_info['repo_path']}"
    candidates.append(repo_https_base + "/info/lfs/objects/batch")

    deduplicated = []
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduplicated.append(candidate)
    return deduplicated


def fetch_lfs_batch_response(pointer_files, repository_root: Path):
    remote_info = parse_github_remote(get_git_remote_url(repository_root))
    body = {
        "operation": "download",
        "transfer": ["basic"],
        "objects": [
            {"oid": pointer_file["oid"], "size": pointer_file["size"]}
            for pointer_file in pointer_files
        ],
    }

    errors = []

    if remote_info["transport"] == "ssh":
        auth_href, auth_headers = authenticate_github_lfs_over_ssh(remote_info)
        for batch_endpoint in build_lfs_batch_endpoint_candidates(auth_href, remote_info):
            try:
                return request_json(batch_endpoint, body, auth_headers)
            except urllib_error.HTTPError as error:
                errors.append(f"{batch_endpoint} -> HTTP {error.code}")
            except urllib_error.URLError as error:
                errors.append(f"{batch_endpoint} -> {error.reason}")
        raise RuntimeError(
            "GitHub SSH LFS authentication succeeded, but all batch API endpoints failed:\n"
            + "\n".join(f"- {detail}" for detail in errors)
        )

    auth_headers = build_authorization_headers(remote_info, repository_root)
    if auth_headers:
        try:
            endpoint = f"https://{remote_info['host']}/{remote_info['repo_path']}/info/lfs/objects/batch"
            return request_json(endpoint, body, auth_headers)
        except urllib_error.HTTPError as error:
            errors.append(f"https batch endpoint -> HTTP {error.code}")
        except urllib_error.URLError as error:
            errors.append(f"https batch endpoint -> {error.reason}")

    raise RuntimeError(
        "Git LFS pointer files were found, but the repository could not authenticate to GitHub "
        "without git-lfs. Configure an SSH remote or install git-lfs.\n"
        + ("\n".join(f"- {detail}" for detail in errors) if errors else "")
    )


def download_lfs_object(target_path: Path, download_url: str, headers: dict, expected_size: int, expected_oid: str):
    request = urllib_request.Request(download_url, headers=headers or {})
    with tempfile.NamedTemporaryFile(prefix="penultima-lfs-", delete=False) as temporary_file:
        temp_path = Path(temporary_file.name)

    digest = hashlib.sha256()
    total_size = 0
    try:
        with urllib_request.urlopen(request, timeout=HTTP_DOWNLOAD_TIMEOUT_SECONDS) as response:
            with temp_path.open("wb") as output_handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output_handle.write(chunk)
                    digest.update(chunk)
                    total_size += len(chunk)

        if total_size != expected_size:
            raise RuntimeError(
                f"Downloaded Git LFS object has incorrect size for {target_path}: "
                f"expected {expected_size}, got {total_size}"
            )

        actual_oid = digest.hexdigest()
        if actual_oid != expected_oid:
            raise RuntimeError(
                f"Downloaded Git LFS object has incorrect sha256 for {target_path}: "
                f"expected {expected_oid}, got {actual_oid}"
            )

        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.replace(target_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def hydrate_lfs_pointer_files(repository_root: Path, pointer_files) -> None:
    print(
        f"Hydrating {len(pointer_files)} Git LFS file(s) from GitHub before publishing...",
        flush=True,
    )
    batch_response = fetch_lfs_batch_response(pointer_files, repository_root)

    pointer_files_by_oid = {}
    for pointer_file in pointer_files:
        pointer_files_by_oid.setdefault(pointer_file["oid"], []).append(pointer_file)

    for object_info in batch_response.get("objects", []):
        oid = (object_info.get("oid") or "").lower()
        if oid not in pointer_files_by_oid:
            continue
        if object_info.get("error"):
            raise RuntimeError(
                f"GitHub LFS batch request failed for oid {oid}: {object_info['error']}"
            )

        download_info = (object_info.get("actions") or {}).get("download")
        if not download_info:
            raise RuntimeError(f"GitHub LFS batch response did not include a download action for {oid}.")

        download_url = download_info.get("href")
        if not download_url:
            raise RuntimeError(f"GitHub LFS download action missing href for {oid}.")

        download_headers = download_info.get("header") or {}
        for pointer_file in pointer_files_by_oid[oid]:
            download_lfs_object(
                pointer_file["source_path"],
                download_url,
                download_headers,
                pointer_file["size"],
                pointer_file["oid"],
            )


def ensure_lfs_files_hydrated(source_root: Path) -> None:
    pointer_files = find_lfs_pointer_files(source_root)
    if not pointer_files:
        return

    if git_lfs_is_available(source_root):
        try:
            subprocess.run(
                ["git", "-C", str(source_root), "lfs", "pull", "--exclude="],
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError(
                "Git LFS pointer files were found in the client repository and "
                f"`git lfs pull` failed in {source_root}: {error}"
            ) from error
        pointer_files = find_lfs_pointer_files(source_root)
        if not pointer_files:
            return

    try:
        hydrate_lfs_pointer_files(source_root, pointer_files)
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"Automatic Git LFS hydration failed in {source_root}: {error}"
        ) from error

    pointer_files = find_lfs_pointer_files(source_root)
    if not pointer_files:
        return

    affected_files = "\n".join(
        f"- {pointer_file['relative_path']} "
        f"(expected {pointer_file['size']} bytes, oid sha256:{pointer_file['oid']})"
        for pointer_file in pointer_files[:5]
    )
    if len(pointer_files) > 5:
        affected_files += f"\n- ... and {len(pointer_files) - 5} more"

    raise RuntimeError(
        "Git LFS pointer files were detected in the client repository, so publishing "
        "would ship broken binaries even after the automatic hydration attempt.\n"
        f"Install Git LFS on the VPS and run:\n  git -C {source_root} lfs pull\n"
        "Affected files:\n"
        f"{affected_files}"
    )


def resolve_publish_version(repository_root: Path, requested_version: str) -> str:
    if requested_version and requested_version != "auto":
        return requested_version.strip()

    version_prefix = None
    for file_name in ("package.json.version", "version.txt"):
        version_path = repository_root / file_name
        if not version_path.is_file():
            continue
        current_version = version_path.read_text(encoding="utf-8").strip()
        if not current_version:
            continue
        match = VERSION_SUFFIX_PATTERN.match(current_version)
        version_prefix = match.group(1) if match else current_version
        break

    if not version_prefix:
        version_prefix = "client"

    git_short_commit = ""
    try:
        git_short_commit = (
            subprocess.check_output(
                ["git", "-C", str(repository_root), "rev-parse", "--short=12", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
    except (OSError, subprocess.CalledProcessError):
        git_short_commit = ""

    if not git_short_commit:
        timestamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
        return f"{version_prefix}-{timestamp}"

    return f"{version_prefix}-{git_short_commit}"


def get_existing_metadata_version(source_root: Path) -> str:
    for file_name in ("package.json.version", "version.txt"):
        path = source_root / file_name
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    return ""


def can_use_existing_metadata(source_root: Path, requested_version: str, rebuild_metadata: bool) -> bool:
    if rebuild_metadata:
        return False
    if requested_version and requested_version != "auto":
        return False
    if not get_existing_metadata_version(source_root):
        return False
    return all((source_root / file_name).is_file() for file_name in METADATA_FILE_NAMES)


def iter_tracked_files(source_root: Path):
    for file_path, relative_path in iter_candidate_tracked_files(source_root):
        yield {
            "relative_path": relative_path,
            "sha256": get_sha256_hex(file_path),
            "size": file_path.stat().st_size,
            "bootstrap_only": relative_path.startswith("conf/"),
            "source_path": file_path,
        }


def get_tracked_files_from_assets_manifest(source_root: Path):
    assets_manifest = json.loads((source_root / "assets.json").read_text(encoding="utf-8"))
    tracked_files = []
    for tracked_file in assets_manifest.get("tracked_files", []):
        if tracked_file["path"] in EXCLUDED_CLIENT_FILES:
            continue
        source_path = source_root / Path(tracked_file["path"])
        if not source_path.is_file():
            raise RuntimeError(f"Tracked client file from assets.json is missing: {source_path}")
        tracked_files.append(
            {
                "relative_path": tracked_file["path"],
                "sha256": tracked_file["sha256"],
                "size": int(tracked_file["size"]),
                "bootstrap_only": bool(tracked_file.get("bootstrap_only")),
                "source_path": source_path,
            }
        )
    return tracked_files


def copy_tracked_files_to_feed(tracked_files, feed_root: Path) -> None:
    for tracked_file in tracked_files:
        destination = feed_root / Path(tracked_file["relative_path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tracked_file["source_path"], destination)


def copy_existing_metadata_files_to_feed(source_root: Path, feed_root: Path) -> None:
    for file_name in METADATA_FILE_NAMES:
        shutil.copy2(source_root / file_name, feed_root / file_name)


def write_feed_metadata_files(tracked_files, feed_root: Path, publish_version: str) -> None:
    package_manifest = {
        "version": publish_version,
        "files": [
            {
                "url": tracked_file["relative_path"],
                "localfile": tracked_file["relative_path"],
                "packedhash": tracked_file["sha256"],
                "packedsize": tracked_file["size"],
                "unpack": False,
                "bootstrap_only": tracked_file["bootstrap_only"],
            }
            for tracked_file in tracked_files
        ],
    }
    write_text(feed_root / "package.json", json.dumps(package_manifest, indent=2) + "\n")
    write_text(feed_root / "package.json.version", publish_version + "\n")
    write_text(feed_root / "version.txt", publish_version + "\n")

    assets_manifest = {
        "version": publish_version,
        "tracked_files": [
            {
                "path": tracked_file["relative_path"],
                "sha256": tracked_file["sha256"],
                "size": tracked_file["size"],
                "managed_by_launcher": True,
                "bootstrap_only": tracked_file["bootstrap_only"],
            }
            for tracked_file in tracked_files
        ],
    }
    assets_json_path = feed_root / "assets.json"
    write_text(assets_json_path, json.dumps(assets_manifest, indent=2) + "\n")
    write_text(feed_root / "assets.json.sha256", get_sha256_hex(assets_json_path) + "\n")


def copy_directory_contents(source_root: Path, destination_root: Path) -> None:
    if not source_root.exists():
        return
    if source_root.is_file():
        destination_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root, destination_root)
        return
    shutil.copytree(source_root, destination_root, dirs_exist_ok=True)


def build_portable_root(client_root: Path, feed_root: Path, portable_root: Path) -> None:
    for directory_name in PORTABLE_DIRECTORIES:
        if directory_name in TRACKED_DIRECTORIES:
            source_directory = feed_root / directory_name
        else:
            source_directory = client_root / directory_name
        destination_directory = portable_root / directory_name
        copy_directory_contents(source_directory, destination_directory)

    for file_name in METADATA_FILE_NAMES:
        source_file = feed_root / file_name
        if source_file.is_file():
            shutil.copy2(source_file, portable_root / file_name)


def create_zip_from_directory(source_directory: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        for file_path in sorted(path for path in source_directory.rglob("*") if path.is_file()):
            archive.write(file_path, file_path.relative_to(source_directory).as_posix())


def publish_staging_to_website(feed_staging_root: Path, portable_staging_root: Path, downloads_root: Path) -> None:
    feed_root = downloads_root / "client-feed"
    bootstrap_zip_path = downloads_root / "Penultima-Client-Feed.zip"
    portable_zip_path = downloads_root / "Penultima-Client-Portable.zip"

    if feed_root.exists():
        shutil.rmtree(feed_root)
    shutil.copytree(feed_staging_root, feed_root)

    create_zip_from_directory(feed_staging_root, bootstrap_zip_path)
    create_zip_from_directory(portable_staging_root, portable_zip_path)


def write_downloads_metadata(downloads_root: Path, publish_version: str) -> None:
    metadata_path = downloads_root / "penultima-downloads.json"
    launcher_zip_path = downloads_root / "Penultima-Launcher.zip"
    portable_zip_path = downloads_root / "Penultima-Client-Portable.zip"
    bootstrap_zip_path = downloads_root / "Penultima-Client-Feed.zip"

    launcher_metadata = None
    if launcher_zip_path.is_file():
        launcher_metadata = {
            "zip": "downloads/Penultima-Launcher.zip",
            "sha256": get_sha256_hex(launcher_zip_path),
            "size": launcher_zip_path.stat().st_size,
        }

    portable_metadata = None
    if portable_zip_path.is_file():
        portable_metadata = {
            "zip": "downloads/Penultima-Client-Portable.zip",
            "sha256": get_sha256_hex(portable_zip_path),
            "size": portable_zip_path.stat().st_size,
        }

    client_feed_metadata = None
    if bootstrap_zip_path.is_file():
        client_feed_metadata = {
            "version": publish_version,
            "root": "downloads/client-feed",
            "bootstrap_zip": "downloads/Penultima-Client-Feed.zip",
            "bootstrap_sha256": get_sha256_hex(bootstrap_zip_path),
            "bootstrap_size": bootstrap_zip_path.stat().st_size,
        }

    metadata = {
        "generated_at_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "launcher": launcher_metadata,
        "portable_client": portable_metadata,
        "client_feed": client_feed_metadata,
    }
    write_text(metadata_path, json.dumps(metadata, indent=4) + "\n")


def main() -> int:
    args = parse_args()
    client_root = Path(args.client_root).resolve()
    ensure_path_exists(client_root, "Client root")
    ensure_lfs_files_hydrated(client_root)
    website_root = resolve_website_root(client_root, args.website_root)
    downloads_root = website_root / "downloads"
    downloads_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="penultima-client-website-") as temp_dir:
        temp_root = Path(temp_dir)
        feed_staging_root = temp_root / "client-feed"
        portable_staging_root = temp_root / "Penultima-Client-Portable"
        feed_staging_root.mkdir(parents=True, exist_ok=True)
        portable_staging_root.mkdir(parents=True, exist_ok=True)

        if can_use_existing_metadata(client_root, args.version, args.rebuild_metadata):
            publish_version = get_existing_metadata_version(client_root)
            tracked_files = get_tracked_files_from_assets_manifest(client_root)
            copy_tracked_files_to_feed(tracked_files, feed_staging_root)
            copy_existing_metadata_files_to_feed(client_root, feed_staging_root)
        else:
            publish_version = resolve_publish_version(client_root, args.version)
            tracked_files = list(iter_tracked_files(client_root))
            copy_tracked_files_to_feed(tracked_files, feed_staging_root)
            write_feed_metadata_files(tracked_files, feed_staging_root, publish_version)

        if not tracked_files:
            raise RuntimeError(
                f"No tracked client files found in {client_root} under "
                "assets, bin, conf, or sounds."
            )

        build_portable_root(client_root, feed_staging_root, portable_staging_root)
        publish_staging_to_website(feed_staging_root, portable_staging_root, downloads_root)
        write_downloads_metadata(downloads_root, publish_version)
        print(f"Published website client assets to {downloads_root}")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
