param(
    [string]$Name,
    [string]$Device,
    [string[]]$Devices,
    [string]$Playlist
)

$ErrorActionPreference = 'Stop'
$wavDir = Join-Path $PSScriptRoot 'wavs'

if (-not [string]::IsNullOrWhiteSpace($Playlist)) {
    $playlistPath = (Resolve-Path -LiteralPath $Playlist).Path
    $entries = @(Get-Content -LiteralPath $playlistPath -Raw -Encoding UTF8 | ConvertFrom-Json)
    if ($entries.Count -eq 0) { throw "Playlist is empty: $playlistPath" }
    $items = foreach ($entry in $entries) {
        if ([string]::IsNullOrWhiteSpace($entry.device) -or [string]::IsNullOrWhiteSpace($entry.name)) {
            throw 'Each playlist item must contain device and name fields.'
        }
        $audioPath = Join-Path $wavDir $entry.name
        if (-not (Test-Path -LiteralPath $audioPath -PathType Leaf)) {
            throw "WAV not found for device '$($entry.device)': $audioPath"
        }
        @{ device = [string]$entry.device; file = (Get-Item -LiteralPath $audioPath).FullName }
    }
    $request = @{ items = @($items) }
    $body = $request | ConvertTo-Json -Depth 5 -Compress
    $utf8 = [System.Text.Encoding]::UTF8.GetBytes($body)
    Write-Host "Requesting playlist for $($items.Count) device(s)"
    Invoke-RestMethod 'http://127.0.0.1:8080/play' `
        -Method Post `
        -ContentType 'application/json; charset=utf-8' `
        -Body $utf8
    exit 0
}

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

$request = @{ file = $file.FullName }
if (-not [string]::IsNullOrWhiteSpace($Device)) { $request.device = $Device }
if ($Devices -and $Devices.Count -gt 0) { $request.devices = $Devices }
$body = $request | ConvertTo-Json -Compress
$utf8 = [System.Text.Encoding]::UTF8.GetBytes($body)

Write-Host "Requesting playback: $($file.FullName)"
Invoke-RestMethod 'http://127.0.0.1:8080/play' `
    -Method Post `
    -ContentType 'application/json; charset=utf-8' `
    -Body $utf8

