#!/usr/bin/env python3
import argparse
import datetime
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
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
    path.write_text(content, encoding="utf-8", newline="\n")


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

    affected_files = "\n".join(
        f"- {pointer_file['relative_path']} "
        f"(expected {pointer_file['size']} bytes, oid sha256:{pointer_file['oid']})"
        for pointer_file in pointer_files[:5]
    )
    if len(pointer_files) > 5:
        affected_files += f"\n- ... and {len(pointer_files) - 5} more"

    raise RuntimeError(
        "Git LFS pointer files were detected in the client repository, so publishing "
        "would ship broken binaries.\n"
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
