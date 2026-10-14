param(
    [string]$Name
)

$ErrorActionPreference = 'Stop'
$wavDir = Join-Path $PSScriptRoot 'wavs'

if ([string]::IsNullOrWhiteSpace($Name)) {
    $file = Get-ChildItem -LiteralPath $wavDir -Filter '*.wav' -File | Select-Object -First 1
} else {
    $file = Get-ChildItem -LiteralPath $wavDir -Filter $Name -File | Select-Object -First 1
}

if ($null -eq $file) {
    Write-Error "No matching WAV file found in $wavDir"
    Get-ChildItem -LiteralPath $wavDir -Filter '*.wav' -File | ForEach-Object { Write-Host $_.Name }
    exit 1
}

$body = @{ file = $file.FullName } | ConvertTo-Json -Compress
$utf8 = [System.Text.Encoding]::UTF8.GetBytes($body)

Write-Host "Requesting playback: $($file.FullName)"
Invoke-RestMethod 'http://127.0.0.1:8080/play' `
    -Method Post `
    -ContentType 'application/json; charset=utf-8' `
    -Body $utf8
