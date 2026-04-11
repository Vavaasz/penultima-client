#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CLIENT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
WEBSITE_ROOT="${1:-}"
PUBLISH_SCRIPT="$SCRIPT_DIR/publish-website-client-assets.py"
LOG_PATH="$CLIENT_ROOT/publish-website-client-assets.log"
HOOK_PATH="$CLIENT_ROOT/.git/hooks/post-merge"

resolve_website_root() {
  for candidate in \
    "$CLIENT_ROOT/../ultima-myaac" \
    "$CLIENT_ROOT/../www" \
    "$CLIENT_ROOT/../UniServerZ/www"
  do
    if [ -f "$candidate/system/pages/downloadclient.php" ]; then
      CDPATH= cd -- "$candidate" && pwd
      return 0
    fi
  done

  return 1
}

if [ ! -d "$CLIENT_ROOT/.git" ]; then
  printf 'Client repository not found at %s\n' "$CLIENT_ROOT" >&2
  exit 1
fi

if [ ! -f "$PUBLISH_SCRIPT" ]; then
  printf 'Publish script not found at %s\n' "$PUBLISH_SCRIPT" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  printf 'python3 is required on the VPS.\n' >&2
  exit 1
fi

if [ -z "$WEBSITE_ROOT" ]; then
  if ! WEBSITE_ROOT=$(resolve_website_root); then
    printf 'Unable to infer the website root. Pass it explicitly, for example:\n' >&2
    printf '  sh %s /home/penultima/ultima-myaac\n' "$0" >&2
    exit 1
  fi
fi

if [ ! -f "$WEBSITE_ROOT/system/pages/downloadclient.php" ]; then
  printf 'Website root does not look correct: %s\n' "$WEBSITE_ROOT" >&2
  exit 1
fi

cat > "$HOOK_PATH" <<EOF
#!/usr/bin/env sh
set -eu
if command -v git-lfs >/dev/null 2>&1; then
  git lfs post-merge "\$@"
fi

log_file="$LOG_PATH"
printf '\n[%s] Running website client publish after post-merge in %s\n' "\$(date '+%Y-%m-%d %H:%M:%S')" "\$(pwd)" >> "\$log_file"
python3 "$PUBLISH_SCRIPT" --client-root "$CLIENT_ROOT" --website-root "$WEBSITE_ROOT" --rebuild-metadata >> "\$log_file" 2>&1 || {
  printf 'Website client publish failed in %s\n' "\$(pwd)" >> "\$log_file"
}
EOF

chmod +x "$HOOK_PATH"
printf 'Installed post-merge hook at %s\n' "$HOOK_PATH"
