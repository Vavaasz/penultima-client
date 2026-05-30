param(
  [string]$ClientRoot = (Split-Path -Parent $PSScriptRoot),
  [string]$WebsiteRoot = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "UniServerZ\www"),
  [string]$Version = "auto",
  [switch]$RebuildMetadata
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$trackedDirectories = @("assets", "bin", "conf", "sounds")
$excludedRelativePaths = @(
  "conf/clientoptions.json",
  "conf/gpublacklist.json",
  "sounds/install-vps-client-hook.ps1",
  "sounds/install-vps-client-hook.sh",
  "sounds/publish-website-client-assets.ps1",
  "sounds/publish-website-client-assets.py"
)
$metadataFileNames = @(
  "package.json",
  "package.json.version",
  "assets.json",
  "assets.json.sha256",
  "version.txt"
)

function Assert-PathExists([string]$Path, [string]$Label) {
  if (-not (Test-Path -LiteralPath $Path)) {
    throw "$Label not found: $Path"
  }
}

function New-EmptyDirectory([string]$Path) {
  if (Test-Path -LiteralPath $Path) {
    Remove-Item -LiteralPath $Path -Recurse -Force
  }

  New-Item -ItemType Directory -Path $Path -Force | Out-Null
}

function New-TemporaryDirectory([string]$Prefix) {
  $path = Join-Path ([System.IO.Path]::GetTempPath()) ($Prefix + [guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Path $path -Force | Out-Null
  return $path
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
  $parent = Split-Path -Parent $Path
  if ($parent) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
  }

  [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function New-ZipFromDirectory([string]$SourceDirectory, [string]$ZipPath) {
  $zipParent = Split-Path -Parent $ZipPath
  if ($zipParent) {
    New-Item -ItemType Directory -Path $zipParent -Force | Out-Null
  }

  $tempZipPath = "$ZipPath.tmp"
  if (Test-Path -LiteralPath $tempZipPath) {
    Remove-Item -LiteralPath $tempZipPath -Force
  }

  try {
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
      $SourceDirectory,
      $tempZipPath,
      [System.IO.Compression.CompressionLevel]::Optimal,
      $false
    )
    Assert-NonEmptyFile -Path $tempZipPath -Label "Generated ZIP"
    Move-Item -LiteralPath $tempZipPath -Destination $ZipPath -Force
  }
  finally {
    if (Test-Path -LiteralPath $tempZipPath) {
      Remove-Item -LiteralPath $tempZipPath -Force -ErrorAction SilentlyContinue
    }
  }
}

function Get-RelativePath([string]$BasePath, [string]$FullPath) {
  $resolvedBase = [System.IO.Path]::GetFullPath($BasePath).TrimEnd('\', '/')
  $resolvedFull = [System.IO.Path]::GetFullPath($FullPath)

  if ($resolvedFull.StartsWith($resolvedBase, [System.StringComparison]::OrdinalIgnoreCase)) {
    return $resolvedFull.Substring($resolvedBase.Length).TrimStart('\', '/').Replace('\', '/')
  }

  $baseUri = New-Object System.Uri(($resolvedBase.Replace('\', '/') + '/'))
  $fullUri = New-Object System.Uri($resolvedFull.Replace('\', '/'))
  return [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($fullUri).ToString()).Replace('\', '/')
}

function Test-IsExcludedClientFile([string]$RelativePath) {
  if ($excludedRelativePaths -contains $RelativePath) {
    return $true
  }

  if ($RelativePath.StartsWith("conf/", [System.StringComparison]::OrdinalIgnoreCase) -and
      -not $RelativePath.Equals("conf/config.ini", [System.StringComparison]::OrdinalIgnoreCase)) {
    return $true
  }

  return $false
}

function Get-Sha256Hex([string]$Path) {
  $stream = [System.IO.File]::OpenRead($Path)
  try {
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
      $hashBytes = $sha256.ComputeHash($stream)
    }
    finally {
      $sha256.Dispose()
    }
  }
  finally {
    $stream.Dispose()
  }

  return ([System.BitConverter]::ToString($hashBytes)).Replace("-", "").ToLowerInvariant()
}

function Get-ZipEntrySha256Hex([string]$ZipPath, [string]$EntryFileName) {
  $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
  try {
    $entry = $archive.Entries |
      Where-Object { -not [string]::IsNullOrEmpty($_.Name) -and $_.Name.Equals($EntryFileName, [System.StringComparison]::OrdinalIgnoreCase) } |
      Select-Object -First 1

    if (-not $entry) {
      return $null
    }

    $stream = $entry.Open()
    try {
      $sha256 = [System.Security.Cryptography.SHA256]::Create()
      try {
        $hashBytes = $sha256.ComputeHash($stream)
      }
      finally {
        $sha256.Dispose()
      }
    }
    finally {
      $stream.Dispose()
    }

    return ([System.BitConverter]::ToString($hashBytes)).Replace("-", "").ToLowerInvariant()
  }
  finally {
    $archive.Dispose()
  }
}

function Assert-NonEmptyFile([string]$Path, [string]$Label) {
  if (-not (Test-Path -LiteralPath $Path)) {
    throw "$Label not found: $Path"
  }

  $length = (Get-Item -LiteralPath $Path).Length
  if ($length -le 0) {
    throw "$Label is empty: $Path"
  }
}

function Copy-SynchronizedClientZip([string]$SourceZipPath, [string]$DestinationZipPath) {
  Assert-NonEmptyFile -Path $SourceZipPath -Label "Launcher feed ZIP"

  $tempZipPath = "$DestinationZipPath.tmp"
  if (Test-Path -LiteralPath $tempZipPath) {
    Remove-Item -LiteralPath $tempZipPath -Force
  }

  try {
    Copy-Item -LiteralPath $SourceZipPath -Destination $tempZipPath -Force
    Assert-NonEmptyFile -Path $tempZipPath -Label "Portable client ZIP"

    $sourceItem = Get-Item -LiteralPath $SourceZipPath
    $destinationItem = Get-Item -LiteralPath $tempZipPath
    if ($sourceItem.Length -ne $destinationItem.Length) {
      throw "Portable client ZIP size does not match the launcher feed ZIP after copy."
    }

    $sourceHash = Get-Sha256Hex -Path $SourceZipPath
    $destinationHash = Get-Sha256Hex -Path $tempZipPath
    if ($sourceHash -ne $destinationHash) {
      throw "Portable client ZIP hash does not match the launcher feed ZIP after copy."
    }

    Move-Item -LiteralPath $tempZipPath -Destination $DestinationZipPath -Force
  }
  finally {
    if (Test-Path -LiteralPath $tempZipPath) {
      Remove-Item -LiteralPath $tempZipPath -Force -ErrorAction SilentlyContinue
    }
  }
}

function Get-LfsPointerInfo([string]$Path) {
  $item = Get-Item -LiteralPath $Path -ErrorAction Stop
  if ($item.Length -gt 1024) {
    return $null
  }

  $content = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
  $lines = @($content -split "\r?\n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  if ($lines.Count -eq 0 -or $lines[0] -ne "version https://git-lfs.github.com/spec/v1") {
    return $null
  }

  $oid = $null
  $size = $null
  foreach ($line in $lines) {
    $oidMatch = [System.Text.RegularExpressions.Regex]::Match(
      $line,
      "^oid sha256:([0-9a-f]{64})$",
      [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    if ($oidMatch.Success) {
      $oid = $oidMatch.Groups[1].Value.ToLowerInvariant()
      continue
    }

    $sizeMatch = [System.Text.RegularExpressions.Regex]::Match($line, "^size ([0-9]+)$")
    if ($sizeMatch.Success) {
      $size = [int64]$sizeMatch.Groups[1].Value
    }
  }

  if ([string]::IsNullOrWhiteSpace($oid) -or $null -eq $size) {
    return $null
  }

  return [pscustomobject]@{
    Oid = $oid
    Size = $size
  }
}

function Get-LfsPointerFiles([string]$SourceRoot) {
  $pointerFiles = New-Object System.Collections.Generic.List[object]

  foreach ($directoryName in $trackedDirectories) {
    $directoryPath = Join-Path $SourceRoot $directoryName
    if (-not (Test-Path -LiteralPath $directoryPath)) {
      continue
    }

    Get-ChildItem -LiteralPath $directoryPath -Recurse -File | Sort-Object FullName | ForEach-Object {
      $relativePath = Get-RelativePath -BasePath $SourceRoot -FullPath $_.FullName
      if (Test-IsExcludedClientFile -RelativePath $relativePath) {
        return
      }

      $pointerInfo = Get-LfsPointerInfo -Path $_.FullName
      if ($null -eq $pointerInfo) {
        return
      }

      $pointerFiles.Add([pscustomobject]@{
        RelativePath = $relativePath
        SourcePath = $_.FullName
        Oid = $pointerInfo.Oid
        Size = $pointerInfo.Size
      })
    }
  }

  return $pointerFiles
}

function Test-GitLfsAvailable([string]$RepositoryRoot) {
  try {
    & git -C $RepositoryRoot lfs version *> $null
    return $LASTEXITCODE -eq 0
  }
  catch {
    return $false
  }
}

function Ensure-HydratedLfsFiles([string]$SourceRoot) {
  $pointerFiles = Get-LfsPointerFiles -SourceRoot $SourceRoot
  if ($pointerFiles.Count -eq 0) {
    return
  }

  if (Test-GitLfsAvailable -RepositoryRoot $SourceRoot) {
    try {
      & git -C $SourceRoot lfs pull --exclude=""
      if ($LASTEXITCODE -ne 0) {
        throw "git lfs pull exited with code $LASTEXITCODE"
      }
    }
    catch {
      throw "Git LFS pointer files were found in the client repository and `git lfs pull` failed in $SourceRoot. $($_.Exception.Message)"
    }

    $pointerFiles = Get-LfsPointerFiles -SourceRoot $SourceRoot
    if ($pointerFiles.Count -eq 0) {
      return
    }
  }

  $sample = $pointerFiles | Select-Object -First 5 | ForEach-Object {
    "- $($_.RelativePath) (expected $($_.Size) bytes, oid sha256:$($_.Oid))"
  }

  if ($pointerFiles.Count -gt 5) {
    $sample += "- ... and $($pointerFiles.Count - 5) more"
  }

  $details = ($sample -join [Environment]::NewLine)
  throw "Git LFS pointer files were detected in the client repository, so publishing would ship broken binaries.`nInstall Git LFS and run:`n  git -C `"$SourceRoot`" lfs pull`nAffected files:`n$details"
}

function Resolve-PublishVersion([string]$RepositoryRoot, [string]$RequestedVersion) {
  if (-not [string]::IsNullOrWhiteSpace($RequestedVersion) -and $RequestedVersion -ne "auto") {
    return $RequestedVersion.Trim()
  }

  $gitShortCommit = ""
  try {
    $gitShortCommit = (git -C $RepositoryRoot rev-parse --short=12 HEAD).Trim()
  }
  catch {
    $gitShortCommit = ""
  }

  $existingVersionFiles = @(
    (Join-Path $RepositoryRoot "package.json.version"),
    (Join-Path $RepositoryRoot "version.txt")
  )

  $versionPrefix = $null
  foreach ($versionFile in $existingVersionFiles) {
    if (-not (Test-Path -LiteralPath $versionFile)) {
      continue
    }

    $currentVersion = (Get-Content -LiteralPath $versionFile -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($currentVersion)) {
      continue
    }

    $matchedPrefix = [System.Text.RegularExpressions.Regex]::Match(
      $currentVersion,
      "^(.*)-[0-9a-f]{7,40}$",
      [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    if ($matchedPrefix.Success) {
      $versionPrefix = $matchedPrefix.Groups[1].Value
    } else {
      $versionPrefix = $currentVersion
    }
    break
  }

  if ([string]::IsNullOrWhiteSpace($versionPrefix)) {
    $versionPrefix = "client"
  }

  if ([string]::IsNullOrWhiteSpace($gitShortCommit)) {
    $timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")
    return "$versionPrefix-$timestamp"
  }

  return "$versionPrefix-$gitShortCommit"
}

function Get-ExistingMetadataVersion([string]$SourceRoot) {
  foreach ($fileName in @("package.json.version", "version.txt")) {
    $path = Join-Path $SourceRoot $fileName
    if (Test-Path -LiteralPath $path) {
      $value = (Get-Content -LiteralPath $path -Raw).Trim()
      if (-not [string]::IsNullOrWhiteSpace($value)) {
        return $value
      }
    }
  }

  return ""
}

function Test-CanUseExistingMetadata([string]$SourceRoot, [string]$RequestedVersion, [bool]$ForceRebuild) {
  if ($ForceRebuild) {
    return $false
  }

  if (-not [string]::IsNullOrWhiteSpace($RequestedVersion) -and $RequestedVersion -ne "auto") {
    return $false
  }

  foreach ($fileName in $metadataFileNames) {
    if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot $fileName))) {
      return $false
    }
  }

  return -not [string]::IsNullOrWhiteSpace((Get-ExistingMetadataVersion -SourceRoot $SourceRoot))
}

function Get-TrackedClientFiles([string]$SourceRoot) {
  $files = New-Object System.Collections.Generic.List[object]

  foreach ($directoryName in $trackedDirectories) {
    $directoryPath = Join-Path $SourceRoot $directoryName
    if (-not (Test-Path -LiteralPath $directoryPath)) {
      continue
    }

    Get-ChildItem -LiteralPath $directoryPath -Recurse -File | Sort-Object FullName | ForEach-Object {
      $relativePath = Get-RelativePath -BasePath $SourceRoot -FullPath $_.FullName
      if (Test-IsExcludedClientFile -RelativePath $relativePath) {
        return
      }
      $hash = Get-Sha256Hex -Path $_.FullName
      $bootstrapOnly = $relativePath.StartsWith("conf/", [System.StringComparison]::OrdinalIgnoreCase)
      $metadata = [ordered]@{
        RelativePath = $relativePath
        Sha256 = $hash
        Size = $_.Length
        BootstrapOnly = $bootstrapOnly
        SourcePath = $_.FullName
      }
      $files.Add([pscustomobject]$metadata)
    }
  }

  return $files
}

function Get-TrackedClientFilesFromAssetsManifest([string]$SourceRoot) {
  $assetsManifestPath = Join-Path $SourceRoot "assets.json"
  $assetsManifest = Get-Content -LiteralPath $assetsManifestPath -Raw | ConvertFrom-Json
  $files = New-Object System.Collections.Generic.List[object]

  foreach ($trackedFile in $assetsManifest.tracked_files) {
    if (Test-IsExcludedClientFile -RelativePath $trackedFile.path) {
      continue
    }
    $sourcePath = Join-Path $SourceRoot ($trackedFile.path -replace '/', '\')
    if (-not (Test-Path -LiteralPath $sourcePath)) {
      throw "Tracked client file from assets.json is missing: $sourcePath"
    }

    $metadata = [ordered]@{
      RelativePath = $trackedFile.path
      Sha256 = $trackedFile.sha256
      Size = [int64]$trackedFile.size
      BootstrapOnly = [bool]$trackedFile.bootstrap_only
      SourcePath = $sourcePath
    }
    $files.Add([pscustomobject]$metadata)
  }

  return $files
}

function Copy-TrackedFilesToFeed(
  [System.Collections.Generic.List[object]]$TrackedFiles,
  [string]$FeedRoot
) {
  foreach ($file in $TrackedFiles) {
    $destinationPath = Join-Path $FeedRoot ($file.RelativePath -replace '/', '\')
    $parent = Split-Path -Parent $destinationPath
    if ($parent) {
      New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    Copy-Item -LiteralPath $file.SourcePath -Destination $destinationPath -Force
  }
}

function Copy-ExistingMetadataFilesToFeed([string]$SourceRoot, [string]$FeedRoot) {
  foreach ($fileName in $metadataFileNames) {
    Copy-Item -LiteralPath (Join-Path $SourceRoot $fileName) -Destination (Join-Path $FeedRoot $fileName) -Force
  }
}

function Assert-RequiredPublishedFiles([System.Collections.Generic.List[object]]$TrackedFiles) {
  $requiredRelativePaths = @(
    "bin/client.exe",
    "conf/config.ini"
  )

  $trackedPathSet = @{}
  foreach ($file in $TrackedFiles) {
    $trackedPathSet[$file.RelativePath.ToLowerInvariant()] = $true
  }

  $missing = @()
  foreach ($relativePath in $requiredRelativePaths) {
    if (-not $trackedPathSet.ContainsKey($relativePath.ToLowerInvariant())) {
      $missing += $relativePath
    }
  }

  if ($missing.Count -gt 0) {
    throw "Refusing to publish an incomplete client feed. Missing required files:`n- $($missing -join "`n- ")"
  }

  $nonBootstrapConfFiles = @(
    $TrackedFiles | Where-Object {
      $_.RelativePath.StartsWith("conf/", [System.StringComparison]::OrdinalIgnoreCase) -and -not $_.BootstrapOnly
    } | ForEach-Object { $_.RelativePath }
  )
  if ($nonBootstrapConfFiles.Count -gt 0) {
    throw "Refusing to publish conf files that are not marked bootstrap-only:`n- $($nonBootstrapConfFiles -join "`n- ")"
  }
}

function Get-IniValue([string]$Path, [string]$SectionName, [string]$KeyName) {
  $currentSection = ""
  foreach ($line in [System.IO.File]::ReadLines($Path)) {
    $trimmed = $line.Trim()
    if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith(";")) {
      continue
    }

    if ($trimmed.StartsWith("[") -and $trimmed.EndsWith("]")) {
      $currentSection = $trimmed.Substring(1, $trimmed.Length - 2)
      continue
    }

    if (-not $currentSection.Equals($SectionName, [System.StringComparison]::OrdinalIgnoreCase)) {
      continue
    }

    $separatorIndex = $trimmed.IndexOf("=")
    if ($separatorIndex -lt 0) {
      continue
    }

    $key = $trimmed.Substring(0, $separatorIndex).Trim()
    if ($key.Equals($KeyName, [System.StringComparison]::OrdinalIgnoreCase)) {
      return $trimmed.Substring($separatorIndex + 1).Trim()
    }
  }

  return ""
}

function Test-IsLoopbackUrl([string]$Value) {
  if ([string]::IsNullOrWhiteSpace($Value)) {
    return $false
  }

  try {
    $uri = [System.Uri]$Value.Trim()
    $host = $uri.Host.ToLowerInvariant()
    return $host -eq "localhost" -or $host -eq "127.0.0.1" -or $host -eq "::1"
  }
  catch {
    return $Value.Trim() -match '^(?i:https?://)?(?:localhost|127\.0\.0\.1|\[::1\])(?::|/|$)'
  }
}

function Assert-PublicClientConfig([string]$SourceRoot) {
  $configPath = Join-Path $SourceRoot "conf\config.ini"
  Assert-PathExists -Path $configPath -Label "Client config"

  foreach ($key in @("loginWebService", "clientWebService")) {
    $value = Get-IniValue -Path $configPath -SectionName "URLS" -KeyName $key
    if ([string]::IsNullOrWhiteSpace($value)) {
      throw "Refusing to publish a client package without URLS/$key in $configPath"
    }

    if (Test-IsLoopbackUrl -Value $value) {
      throw "Refusing to publish a public client package with URLS/$key pointing to loopback: $value"
    }
  }
}

function Write-FeedMetadataFiles(
  [System.Collections.Generic.List[object]]$TrackedFiles,
  [string]$FeedRoot,
  [string]$PublishVersion
) {
  $packageFiles = New-Object System.Collections.Generic.List[object]
  $trackedFileMetadata = New-Object System.Collections.Generic.List[object]

  foreach ($file in $TrackedFiles) {
    $packageFiles.Add([ordered]@{
      url = $file.RelativePath
      localfile = $file.RelativePath
      packedhash = $file.Sha256
      packedsize = $file.Size
      unpack = $false
      bootstrap_only = $file.BootstrapOnly
    })

    $trackedFileMetadata.Add([ordered]@{
      path = $file.RelativePath
      sha256 = $file.Sha256
      size = $file.Size
      managed_by_launcher = $true
      bootstrap_only = $file.BootstrapOnly
    })
  }

  $packageManifest = [ordered]@{
    version = $PublishVersion
    files = $packageFiles
  }
  $packageJsonPath = Join-Path $FeedRoot "package.json"
  Write-Utf8NoBom -Path $packageJsonPath -Content ($packageManifest | ConvertTo-Json -Depth 6)

  $packageVersionPath = Join-Path $FeedRoot "package.json.version"
  Write-Utf8NoBom -Path $packageVersionPath -Content "$PublishVersion`n"

  $versionPath = Join-Path $FeedRoot "version.txt"
  Write-Utf8NoBom -Path $versionPath -Content "$PublishVersion`n"

  $assetsManifest = [ordered]@{
    version = $PublishVersion
    tracked_files = $trackedFileMetadata
  }
  $assetsJsonPath = Join-Path $FeedRoot "assets.json"
  Write-Utf8NoBom -Path $assetsJsonPath -Content ($assetsManifest | ConvertTo-Json -Depth 6)

  $assetsHashPath = Join-Path $FeedRoot "assets.json.sha256"
  Write-Utf8NoBom -Path $assetsHashPath -Content ((Get-Sha256Hex -Path $assetsJsonPath) + "`n")
}

function Publish-StagingToWebsite(
  [string]$FeedStagingRoot,
  [string]$DownloadsRoot
) {
  $feedRoot = Join-Path $DownloadsRoot "client-feed"
  $bootstrapZipPath = Join-Path $DownloadsRoot "Penultima-Client-Feed.zip"
  $portableZipPath = Join-Path $DownloadsRoot "Penultima-Client-Portable.zip"

  if (Test-Path -LiteralPath $feedRoot) {
    Remove-Item -LiteralPath $feedRoot -Recurse -Force
  }

  Copy-Item -LiteralPath $FeedStagingRoot -Destination $feedRoot -Recurse -Force
  New-ZipFromDirectory -SourceDirectory $FeedStagingRoot -ZipPath $bootstrapZipPath
  Copy-SynchronizedClientZip -SourceZipPath $bootstrapZipPath -DestinationZipPath $portableZipPath
}

function Write-DownloadsMetadata([string]$DownloadsRoot, [string]$PublishVersion) {
  $metadataPath = Join-Path $DownloadsRoot "penultima-downloads.json"
  $launcherZipPath = Join-Path $DownloadsRoot "Penultima-Launcher.zip"
  $portableZipPath = Join-Path $DownloadsRoot "Penultima-Client-Portable.zip"
  $bootstrapZipPath = Join-Path $DownloadsRoot "Penultima-Client-Feed.zip"

  $existingLauncherMetadata = $null
  $existingFullMinimapMetadata = $null
  if (Test-Path -LiteralPath $metadataPath) {
    try {
      $existingMetadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
      $existingLauncherMetadata = $existingMetadata.launcher
      if ($existingMetadata.PSObject.Properties.Name -contains "full_minimap") {
        $existingFullMinimapMetadata = $existingMetadata.full_minimap
      }
    } catch {
      $existingLauncherMetadata = $null
      $existingFullMinimapMetadata = $null
    }
  }

  $launcherMetadata = $null
  if (Test-Path -LiteralPath $launcherZipPath) {
    $launcherHash = Get-Sha256Hex -Path $launcherZipPath
    $launcherSize = (Get-Item -LiteralPath $launcherZipPath).Length
    $launcherExeHash = Get-ZipEntrySha256Hex -ZipPath $launcherZipPath -EntryFileName "penultima-launcher.exe"
    $launcherMetadata = [ordered]@{
      zip = "downloads/Penultima-Launcher.zip"
      sha256 = $launcherHash
      size = $launcherSize
    }

    $sameLauncherPayload = (
      $null -ne $existingLauncherMetadata -and
      "$($existingLauncherMetadata.sha256)".ToLowerInvariant() -eq $launcherHash.ToLowerInvariant() -and
      [int64]$existingLauncherMetadata.size -eq $launcherSize
    )

    if ($sameLauncherPayload) {
      foreach ($key in @("version", "zip", "sha256", "signed", "signature_status", "exe_sha256")) {
        if ($existingLauncherMetadata.PSObject.Properties.Name -contains $key) {
          $launcherMetadata[$key] = $existingLauncherMetadata.$key
        }
      }
    }

    if (-not [string]::IsNullOrWhiteSpace($launcherExeHash)) {
      $launcherMetadata["exe_sha256"] = $launcherExeHash
    } elseif (
      $null -ne $existingLauncherMetadata -and
      $existingLauncherMetadata.PSObject.Properties.Name -contains "exe_sha256"
    ) {
      $launcherMetadata["exe_sha256"] = $existingLauncherMetadata.exe_sha256
    }
  }

$portableMetadata = $null
if (Test-Path -LiteralPath $portableZipPath) {
  $portableHash = Get-Sha256Hex -Path $portableZipPath
  $portableMetadata = [ordered]@{
      zip = "downloads/Penultima-Client-Portable.zip?sha256=$($portableHash.Substring(0, 12))"
      sha256 = $portableHash
      size = (Get-Item -LiteralPath $portableZipPath).Length
    }
  }

  $clientFeedMetadata = $null
  if (Test-Path -LiteralPath $bootstrapZipPath) {
    $bootstrapHash = Get-Sha256Hex -Path $bootstrapZipPath
    $clientFeedMetadata = [ordered]@{
      version = $PublishVersion
      root = "downloads/client-feed"
      bootstrap_zip = "downloads/Penultima-Client-Feed.zip?sha256=$($bootstrapHash.Substring(0, 12))"
      bootstrap_sha256 = $bootstrapHash
      bootstrap_size = (Get-Item -LiteralPath $bootstrapZipPath).Length
    }
  }

  $metadata = [ordered]@{
    generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    launcher = $launcherMetadata
    portable_client = $portableMetadata
    client_feed = $clientFeedMetadata
  }

  if ($null -ne $existingFullMinimapMetadata) {
    $metadata["full_minimap"] = $existingFullMinimapMetadata
  }

  Write-Utf8NoBom -Path $metadataPath -Content ($metadata | ConvertTo-Json -Depth 6)
}

Assert-PathExists -Path $ClientRoot -Label "Client root"
Assert-PathExists -Path $WebsiteRoot -Label "Website root"
Ensure-HydratedLfsFiles -SourceRoot $ClientRoot
Assert-PublicClientConfig -SourceRoot $ClientRoot

$downloadsRoot = Join-Path $WebsiteRoot "downloads"
New-Item -ItemType Directory -Path $downloadsRoot -Force | Out-Null

$tempRoot = New-TemporaryDirectory -Prefix "penultima-client-website-"
$feedStagingRoot = Join-Path $tempRoot "client-feed"

try {
  New-EmptyDirectory -Path $feedStagingRoot

  if (Test-CanUseExistingMetadata -SourceRoot $ClientRoot -RequestedVersion $Version -ForceRebuild:$RebuildMetadata) {
    $publishVersion = Get-ExistingMetadataVersion -SourceRoot $ClientRoot
    $trackedFiles = Get-TrackedClientFilesFromAssetsManifest -SourceRoot $ClientRoot
    Copy-TrackedFilesToFeed -TrackedFiles $trackedFiles -FeedRoot $feedStagingRoot
    Copy-ExistingMetadataFilesToFeed -SourceRoot $ClientRoot -FeedRoot $feedStagingRoot
  } else {
    $publishVersion = Resolve-PublishVersion -RepositoryRoot $ClientRoot -RequestedVersion $Version
    $trackedFiles = Get-TrackedClientFiles -SourceRoot $ClientRoot
    Copy-TrackedFilesToFeed -TrackedFiles $trackedFiles -FeedRoot $feedStagingRoot
    Write-FeedMetadataFiles -TrackedFiles $trackedFiles -FeedRoot $feedStagingRoot -PublishVersion $publishVersion
  }

  if ($trackedFiles.Count -eq 0) {
    throw "No tracked client files found in assets, bin, conf, or sounds under $ClientRoot"
  }
  Assert-RequiredPublishedFiles -TrackedFiles $trackedFiles

  Publish-StagingToWebsite -FeedStagingRoot $feedStagingRoot -DownloadsRoot $downloadsRoot
  Write-DownloadsMetadata -DownloadsRoot $downloadsRoot -PublishVersion $publishVersion

  Write-Host "Published website client assets to $downloadsRoot"
}
finally {
  if (Test-Path -LiteralPath $tempRoot) {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
  }
}
