<#
.SYNOPSIS
    Verify SignPath-signed artifacts and record the signing evidence.

.DESCRIPTION
    Run by the owner after SignPath returns the signed files. Checks, per file:
      * Authenticode status is Valid (chain to a trusted root);
      * a timestamp countersignature is present;
      * the signer is the expected publisher (default: SignPath Foundation).
    Then writes signing-record.json with publisher, timestamp, size and the
    signed SHA-256. It never writes to the artifacts and never touches keys.

.EXAMPLE
    .\packaging\signing\verify_signed.ps1 -SignedDir signed -Record signing/signing-record.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SignedDir,
    [string]$Record = "signing/signing-record.json",
    [string]$ExpectedPublisher = "SignPath Foundation"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$files = Get-ChildItem -LiteralPath $SignedDir -Filter *.exe -File
if (-not $files) { Write-Host "VERIFY FAILED: no .exe files in $SignedDir" -ForegroundColor Red; exit 2 }

$entries = @()
foreach ($file in $files) {
    $sig = Get-AuthenticodeSignature -LiteralPath $file.FullName
    if ($sig.Status -ne 'Valid') {
        Write-Host ("VERIFY FAILED: {0} status {1}" -f $file.Name, $sig.Status) -ForegroundColor Red; exit 2
    }
    if ($null -eq $sig.TimeStamperCertificate) {
        Write-Host ("VERIFY FAILED: {0} has no RFC 3161 timestamp" -f $file.Name) -ForegroundColor Red; exit 2
    }
    $subject = $sig.SignerCertificate.Subject
    if ($subject -notmatch [regex]::Escape($ExpectedPublisher)) {
        Write-Host ("VERIFY FAILED: {0} signer is '{1}', expected '{2}'" -f $file.Name, $subject, $ExpectedPublisher) -ForegroundColor Red
        exit 2
    }
    $entries += [pscustomobject]@{
        name              = $file.Name
        bytes             = $file.Length
        signed_sha256     = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLower()
        signature_status  = $sig.Status
        signer_subject    = $subject
        signer_thumbprint = $sig.SignerCertificate.Thumbprint
        timestamp_subject = $sig.TimeStamperCertificate.Subject
        timestamp_utc     = $sig.TimeStamperCertificate.NotBefore.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    }
    Write-Host ("OK {0} | {1} | {2}" -f $file.Name, $sig.Status, $entries[-1].signed_sha256)
}

$recordDir = Split-Path -Parent $Record
if ($recordDir) { New-Item -ItemType Directory -Force -Path $recordDir | Out-Null }
[pscustomobject]@{
    verified_utc      = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    expected_publisher = $ExpectedPublisher
    artifacts         = $entries
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $Record -Encoding utf8
Write-Host "record: $Record"
exit 0
