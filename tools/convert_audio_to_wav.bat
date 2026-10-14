@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Convert tools\source audio files to 16 kHz stereo PCM WAV files.
set "TOOLS_DIR=%~dp0"
set "SOURCE_DIR=%TOOLS_DIR%source"
set "OUTPUT_DIR=%TOOLS_DIR%wavs"

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo ERROR: ffmpeg was not found in PATH.
    echo Install FFmpeg and add it to PATH, then run this script again.
    exit /b 1
)

if not exist "%SOURCE_DIR%" mkdir "%SOURCE_DIR%"
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

set /a COUNT=0
for %%E in (wav mp3 flac m4a aac ogg wma opus mp4) do (
    for %%F in ("%SOURCE_DIR%\*.%%E") do (
        if exist "%%~fF" (
            echo Converting: %%~nxF
            ffmpeg -hide_banner -loglevel warning -y -i "%%~fF" -vn -ar 16000 -ac 2 -c:a pcm_s16le "%OUTPUT_DIR%\%%~nF.wav"
            if errorlevel 1 (
                echo FAILED: %%~nxF
            ) else (
                set /a COUNT+=1
            )
        )
    )
)

echo.
echo Converted !COUNT! file(s).
echo Output directory: "%OUTPUT_DIR%"
endlocal
pause
