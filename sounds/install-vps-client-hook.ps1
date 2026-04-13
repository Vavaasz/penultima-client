$ErrorActionPreference = "Stop"

$scriptRoot = $PSScriptRoot
$clientRoot = Split-Path -Parent $scriptRoot
$websiteRoot = Join-Path (Split-Path -Parent $clientRoot) "UniServerZ\www"
$publishScript = Join-Path $scriptRoot "publish-website-client-assets.ps1"
$logPath = Join-Path $clientRoot "publish-website-client-assets.log"
$hookPath = Join-Path $clientRoot ".git\hooks\post-merge"

if (-not (Test-Path -LiteralPath (Join-Path $clientRoot ".git"))) {
  throw "Client repository not found at $clientRoot"
}

if (-not (Test-Path -LiteralPath $websiteRoot)) {
  throw "Website root not found at $websiteRoot"
}

$hook = @'
#!/bin/sh
log_file="__LOG_PATH__"
printf "\n[%s] Running website client publish after post-merge in %s\n" "$(date '+%Y-%m-%d %H:%M:%S')" "$(pwd)" >> "$log_file"
if git lfs version >/dev/null 2>&1; then
  git lfs pull --exclude="" >> "$log_file" 2>&1 || {
    printf "git lfs pull failed in %s\n" "$(pwd)" >> "$log_file"
  }
else
  printf "git lfs is not available; publish script will try GitHub-authenticated LFS hydration.\n" >> "$log_file"
fi
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "__PUBLISH_SCRIPT__" -ClientRoot "__CLIENT_ROOT__" -WebsiteRoot "__WEBSITE_ROOT__" -RebuildMetadata >> "$log_file" 2>&1 || {
  printf "Website client publish failed in %s\n" "$(pwd)" >> "$log_file"
}
'@

$hook = $hook.Replace("__LOG_PATH__", $logPath.Replace('\', '/'))
$hook = $hook.Replace("__PUBLISH_SCRIPT__", $publishScript.Replace('\', '/'))
$hook = $hook.Replace("__CLIENT_ROOT__", $clientRoot.Replace('\', '/'))
$hook = $hook.Replace("__WEBSITE_ROOT__", $websiteRoot.Replace('\', '/'))

Set-Content -Path $hookPath -Value $hook -Encoding Ascii -NoNewline
Write-Host "Installed post-merge hook at $hookPath"
