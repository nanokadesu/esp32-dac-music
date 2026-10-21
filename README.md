# ESP32 WebSocket I2S 播放器

ESP32 通过 WebSocket 从 Python 主机接收 PCM WAV 切片，再通过 I2S 输出到 GY-PCM5102。当前固件格式为 **16 kHz、16 bit、双声道、PCM**。

## 接线

```text
ESP32 GPIO26 -> BCK
ESP32 GPIO25 -> LRCK/LCK
ESP32 GPIO22 -> DIN
ESP32 GND    -> GND
PCM5102 XSMT -> 3.3V
PCM5102 FMT  -> GND
```

`SCK` 通常悬空；确认模块供电和 `XSMT` 静音控制正确。

## ESP32 编译烧录

修改 `main/main.c` 中的 `WIFI_SSID`、`WIFI_PASS`、`WS_URI`，然后在 ESP-IDF v5.4.4 环境执行：

```powershell
idf.py set-target esp32
idf.py build
idf.py -p COM3 flash monitor
```

## 后端启动

安装依赖：

```powershell
python -m pip install -r tools/requirements.txt
```

启动主机控制服务：

```powershell
python tools/ws_upload_server.py `
  --host 0.0.0.0 `
  --port 8000 `
  --http-port 8080
```

服务同时监听：

```text
WebSocket: ws://主机IP:8000/stream
HTTP:      http://主机IP:8080
```

服务启动时不需要指定音频文件。ESP32 连接后发送 `start`；只有通过 HTTP `/play` 选择文件后，后端才按窗口发送音频切片；ESP32 播放一片后请求下一片。切片序号严格递增，WAV 结束后发送 `end`。

## HTTP 遥控播放

切换文件并让 ESP32 播放：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/play `
  -Method Post `
  -ContentType 'application/json' `
  -Body '{"file":"E:/audio/song.wav"}'
```

Windows 路径建议使用 `/`，例如 `E:/music/song.wav`；也可以使用
`ConvertTo-Json` 生成请求体，避免反斜杠转义问题：

```powershell
$body = @{ file = 'E:\music\song.wav' } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8080/play -Method Post `
  -ContentType 'application/json' -Body $body
```

停止当前连接：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/stop -Method Post
```

查询状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/status
```

`/play` 会立即向目标 ESP32 发送 `play` 打断指令，清空旧缓冲并在当前 WebSocket 上重新获取新文件。

## 批量转换音频

把原始音频放入：

```text
tools/source/
```

双击运行：

```text
tools/convert_audio_to_wav.bat
```

脚本会使用 FFmpeg 将常见音频转换到：

```text
tools/wavs/
```

输出格式为 16 kHz、16 bit、双声道 PCM WAV。

也可以手动转换单个文件：

```powershell
ffmpeg -i input.wav -ar 16000 -ac 2 -sample_fmt s16 output_16000.wav
```

16 kHz 能降低网络带宽；如果改用 22.05 kHz 或 44.1 kHz，必须同步修改 `main/main.c` 的 `SAMPLE_RATE`，并使用相同采样率的 WAV。

### 多设备控制

在 `main/main.c` 中为每块 ESP32 设置唯一 ID：

```c
#define DEVICE_ID "esp32_01"
```

指定设备播放：

```powershell
.\upload.ps1 -Name '四季折の羽.wav' -Device 'esp32_01'
```

每台设备各自保存当前曲目和 WebSocket 切片游标，设备间切歌互不覆盖。省略 `-Device` 时向所有已注册设备广播；同一文件可用 `-Devices` 发给指定设备列表：

```powershell
.\upload.ps1 -Name '四季折の羽.wav' -Devices 'hall_01','front'
```

不同设备播放不同文件时，向 `/play` 发送 `items` 列表，每项独立指定 `device` 和 `file`：

可复制并编辑 `tools/playlist.example.json`，然后直接发送列表：

```powershell
.\upload.ps1 -Playlist .\playlist.json
```

```powershell
$body = @{ items = @(
    @{ device = 'hall_01'; file = 'E:\audio\hall.wav' },
    @{ device = 'front'; file = 'E:\audio\front.wav' }
) } | ConvertTo-Json -Depth 4 -Compress
Invoke-RestMethod http://127.0.0.1:8080/play -Method Post `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

离线设备的曲目分配会保留，设备重连后发送 `start` 时会读取自己的曲目。`/status` 返回每台设备 ID、当前文件、在线状态及 `starting`、`streaming`、`finished` 或 `offline` 状态。
