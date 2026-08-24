# publish_versioned_release.ps1
# Publishes a REAL, numbered public release (vX.Y.Z, read from the VERSION
# file) to GitHub Releases - separate from publish_release.ps1's rolling
# "latest" tag, which stays exactly as-is for the internal build-machine ->
# test-machine update.bat workflow (fast dev-cycle test builds, not meant to
# be a real version history).
#
# This is what actually makes the app's own update-check useful: webapp.py
# compares SERVER_VERSION against the tag_name of GET /repos/.../releases/latest
# on GitHub - that call returns the most recently PUBLISHED non-prerelease
# release regardless of its tag string, so as long as this script's vX.Y.Z
# release is newer than the rolling "latest" internal one, admins get a
# correct "update available" notice instead of nothing (a release literally
# tagged "latest" parses as version 0 and never triggers the check).
#
# Run this ONLY when actually cutting a public release (not after every test
# build) - after bumping VERSION + SERVER_VERSION (webapp.py) + AppVersion
# (HAMCTRL-installer.iss) together and rebuilding the installer.
#
# Usage: powershell -ExecutionPolicy Bypass -File publish_versioned_release.ps1

$Repo = "SQ3MZM/HAMCTRL"
$VersionFile = Join-Path $PSScriptRoot "VERSION"
$Installer = Join-Path $PSScriptRoot "Output\HAMCTRL-Setup.exe"

$Gh = "C:\Program Files\GitHub CLI\gh.exe"
if (-not (Test-Path $Gh)) { $Gh = "gh" }

if (-not (Test-Path $VersionFile)) {
    Write-Host "VERSION file not found: $VersionFile"
    exit 1
}
$Version = (Get-Content $VersionFile -Raw).Trim()
$Tag = "v$Version"

if (-not (Test-Path $Installer)) {
    Write-Host "Installer not found: $Installer"
    Write-Host "Build it first: py build_server.py, then compile HAMCTRL-installer.iss."
    exit 1
}

$SizeMB = [math]::Round((Get-Item $Installer).Length / 1MB, 1)
Write-Host "Version: $Version  (tag: $Tag)"
Write-Host "Installer: $Installer ($SizeMB MB)"

& $Gh release view $Tag --repo $Repo *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "Release '$Tag' already exists on GitHub."
    Write-Host "Bump VERSION (and SERVER_VERSION / AppVersion to match) before publishing a new one, or delete the existing tag first if this was a mistake."
    exit 1
}

Write-Host "Creating release '$Tag'..."
& $Gh release create $Tag $Installer --repo $Repo --title "HAMCTRL $Tag" --generate-notes
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host ""
Write-Host "Done: https://github.com/$Repo/releases/tag/$Tag"
Write-Host "(the app's own update-check on other installs will now see this as the latest release)"
