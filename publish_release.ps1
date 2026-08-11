# publish_release.ps1
# Uploads the built installer to a rolling "latest" GitHub release in the
# private HAMCTRL repo, so test machines can pull the newest build via
# update.bat. Run this on the build machine after each rebuild
# (build_server.py + Inno Setup compile).
#
# Usage: powershell -ExecutionPolicy Bypass -File publish_release.ps1

$Repo = "SQ3MZM/HAMCTRL"
$Tag = "latest"
$Installer = Join-Path $PSScriptRoot "Output\HAMCTRL-Setup.exe"

$Gh = "C:\Program Files\GitHub CLI\gh.exe"
if (-not (Test-Path $Gh)) { $Gh = "gh" }

if (-not (Test-Path $Installer)) {
    Write-Host "Installer not found: $Installer"
    Write-Host "Build it first: py build_server.py, then compile HAMCTRL-installer.iss."
    exit 1
}

$SizeMB = [math]::Round((Get-Item $Installer).Length / 1MB, 1)
Write-Host "Installer: $Installer ($SizeMB MB)"

$Notes = "Build published: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"

& $Gh release view $Tag --repo $Repo *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Updating existing release '$Tag'..."
    & $Gh release upload $Tag $Installer --repo $Repo --clobber
    if ($LASTEXITCODE -ne 0) { exit 1 }
    & $Gh release edit $Tag --repo $Repo --notes $Notes
} else {
    Write-Host "Creating release '$Tag'..."
    & $Gh release create $Tag $Installer --repo $Repo --title "Latest build" --notes $Notes
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

Write-Host ""
Write-Host "Done: https://github.com/$Repo/releases/tag/$Tag"
