@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Convert audio files in tools\source to 16 kHz stereo PCM WAV in tools\wavs.
rem Every path below is relative to this script's folder and the script switches
rem into that folder first, so it works from any working directory
rem (double-click in Explorer, run from another console, scheduled task, ...).

pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
    echo ERROR: cannot open script folder "%~dp0".
    exit /b 1
)

set "SOURCE_DIR=source"
set "OUTPUT_DIR=wavs"
set "FFMPEG_LOG=%TEMP%\convert_audio_to_wav.log"

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo ERROR: ffmpeg was not found in PATH.
    echo Install FFmpeg and add it to PATH, then run this script again.
    popd
    exit /b 1
)

if not exist "%SOURCE_DIR%" mkdir "%SOURCE_DIR%"
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

set /a COUNT=0
set /a FAILED=0

for %%E in (wav mp3 flac m4a m4b m4r aac ogg opus wma mp4 m4v mka webm aif aiff amr ape wv 3gp) do (
    for %%F in ("%SOURCE_DIR%\*.%%E") do (
        if exist "%%~fF" (
            echo Converting: %%~nxF
            rem Write to a .part file first so a failed run never leaves a
            rem truncated WAV behind. "-f wav" is required because FFmpeg
            rem cannot guess an output format from the .part extension.
            rem FFmpeg exits with 1 or with a negative code on Windows (for
            rem example -1094995529 for invalid data) and "if errorlevel 1"
            rem is false for negative codes, so use && / || instead.
            set "CONV_ERR="
            ffmpeg -hide_banner -nostdin -loglevel warning -y -i "%%~fF" -map 0:a:0 -vn -sn -dn -map_metadata -1 -ar 16000 -ac 2 -c:a pcm_s16le -f wav "%OUTPUT_DIR%\%%~nF.wav.part" 2>"%FFMPEG_LOG%" && set "CONV_ERR=" || set "CONV_ERR=1"
            if defined CONV_ERR (
                if exist "%OUTPUT_DIR%\%%~nF.wav.part" del /q "%OUTPUT_DIR%\%%~nF.wav.part"
                set /a FAILED+=1
                echo FAILED: %%~nxF
                if exist "%FFMPEG_LOG%" type "%FFMPEG_LOG%"
            ) else (
                move /y "%OUTPUT_DIR%\%%~nF.wav.part" "%OUTPUT_DIR%\%%~nF.wav" >nul
                if errorlevel 1 (
                    if exist "%OUTPUT_DIR%\%%~nF.wav.part" del /q "%OUTPUT_DIR%\%%~nF.wav.part"
                    set /a FAILED+=1
                    echo FAILED: %%~nxF
                    echo   Could not replace "%OUTPUT_DIR%\%%~nF.wav" - it is probably still open in ws_upload_server.py or an audio player.
                ) else (
                    set /a COUNT+=1
                )
            )
        )
    )
)

echo.
echo Converted %COUNT% file(s).
if not "%FAILED%"=="0" echo Failed: %FAILED% file(s) - see the FFmpeg message above.
echo Output directory: "%CD%\%OUTPUT_DIR%"
popd
endlocal
pause
exit /b 0
