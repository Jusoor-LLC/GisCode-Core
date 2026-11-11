<#
.SYNOPSIS
    Owner-run gate that validates a frozen artifact set before it is submitted
    to SignPath Foundation. It never signs, never stores a credential and never
    falls back to a self-signed certificate.

.DESCRIPTION
    Order of checks, each of which aborts on failure:
      1. the owner approval marker exists and names the release it approves;
      2. every artifact still matches the SHA-256 in the manifest;
      3. the manifest itself has not been edited (manifest_sha256);
      4. an owner-held SignPath API token is present in the environment.
    When step 4 fails the script prints SIGNING SKIPPED and exits 0, leaving the
    artifacts untouched. The submission payload is then prepared as a ZIP that
    the owner uploads (or submits with their own token); approval happens in the
    SignPath UI, so no signing action is possible from an agent session.

.EXAMPLE
    # Owner, on the release machine, after approving the build:
    Set-Content signing\APPROVED.txt "release=0.1.0 artifacts=dist"
    $env:SIGNPATH_API_TOKEN = <from the owner's secret store>
    .\packaging\signing\sign_release.ps1 -Manifest signing\manifest.json -ApprovalMarker signing\APPROVED.txt
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Manifest,
    [Parameter(Mandatory = $true)][string]$ApprovalMarker,
    [string]$OutDir = "signing/request"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
# Write-Host, not Write-Error: with ErrorActionPreference=Stop a Write-Error would
# terminate the script with the generic error exit code instead of our own.
function Fail([string]$Message) { Write-Host "SIGNING ABORTED: $Message" -ForegroundColor Red; exit 2 }
function Skip([string]$Message) { Write-Host "SIGNING SKIPPED: $Message" -ForegroundColor Yellow; exit 0 }

if (-not (Test-Path -LiteralPath $Manifest)) { Fail "manifest not found: $Manifest" }
if (-not (Test-Path -LiteralPath $ApprovalMarker)) {
    Fail "owner approval marker missing - the release gate has not been passed ($ApprovalMarker)"
}

$marker = Get-Content -LiteralPath $ApprovalMarker -Raw
if ($marker -notmatch 'release\s*=\s*\S+') {
    Fail "approval marker must name the approved release, e.g. 'release=0.1.0 artifacts=dist'"
}

# The manifest's own integrity comes from the detached digest written beside it
# by make_manifest.py. Hashing the file bytes is language-independent, unlike a
# JSON re-serialisation, which cannot reproduce Python's canonical form.
$sidecar = "$Manifest.sha256"
if (-not (Test-Path -LiteralPath $sidecar)) { Fail "manifest digest file missing: $sidecar" }
$recorded = ((Get-Content -LiteralPath $sidecar -Raw).Trim() -split '\s+')[0].ToLower()
$actual = (Get-FileHash -LiteralPath $Manifest -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $recorded) {
    Fail "manifest has been modified (recorded $recorded, actual $actual)"
}
$data = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json

$root = Split-Path -Parent (Resolve-Path -LiteralPath $Manifest)
$artifactRoot = Join-Path $root $data.artifacts_root
Write-Host "validating $($data.artifacts.Count) artifact(s) from $artifactRoot"
foreach ($artifact in $data.artifacts) {
    $path = Join-Path $artifactRoot $artifact.relative_path
    if (-not (Test-Path -LiteralPath $path)) { Fail "artifact missing: $path" }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $artifact.sha256) {
        Fail "hash mismatch for $($artifact.name): manifest $($artifact.sha256), actual $actual"
    }
    $size = (Get-Item -LiteralPath $path).Length
    if ($size -ne $artifact.bytes) { Fail "size mismatch for $($artifact.name)" }
}
Write-Host "all artifact hashes match the manifest"

if (-not $env:SIGNPATH_API_TOKEN) {
    Skip "owner credentials unavailable (SIGNPATH_API_TOKEN is not set); artifacts left untouched"
}

$targets = $data.artifacts | Where-Object { $_.signed_candidate }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$zip = Join-Path $OutDir ("giscode-core-{0}-unsigned.zip" -f $data.release_version)
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path ($targets | ForEach-Object { Join-Path $artifactRoot $_.relative_path }) -DestinationPath $zip
Write-Host "submission payload: $zip"
Write-Host ("targets: " + (($targets | ForEach-Object { $_.name }) -join ", "))
Write-Host "publisher on the resulting signatures: SignPath Foundation (Foundation-issued certificate)"
Write-Host "Next (owner only): submit the payload, approve the request in SignPath, then run verify_signed.ps1."
Write-Host "No signing occurred in this script; no private key exists on this machine."
exit 0
