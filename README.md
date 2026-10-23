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
  -Body '{"file":"wavs/song.wav"}'
```

`file` 支持相对路径，按以下顺序解析，找到即用：

1. 服务进程的当前工作目录
2. `tools/` 目录（`ws_upload_server.py` 所在目录）
3. `tools/wavs/` 目录
4. 按文件名在 `tools/wavs/` 中匹配（忽略大小写，便于远程客户端）

因此 `"wavs/song.wav"`、`"song.wav"` 都能直接使用，无需写死绝对路径。绝对路径
依然有效；Windows 路径建议使用 `/`，例如 `E:/music/song.wav`，也可以使用
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

## 设备事件上报 `/send_message`

除了控制端主动切歌，设备端也可以反过来通知主机播放某段语音（例如现场有人时自动播报）：

```text
POST http://主机IP:8080/send_message
Content-Type: application/json

{"pos":"insight","event":1}
```

```powershell
Invoke-RestMethod http://127.0.0.1:8080/send_message -Method Post `
  -ContentType 'application/json' `
  -Body '{"pos":"insight","event":1}'
```

- `pos`：目标设备 ID，即 ESP32 连上 WebSocket 时上报的 `DEVICE_ID`。
- `event`：音频编号。未加载映射文件时，内置 `1` → `计算中心.wav`；其它编号不算错误，返回 HTTP 200 且 `handled` 为 `false`。
- 没映射到音频时接口返回 200 + `handled:false`，设备端无需特殊处理。

服务端每收到一次上报都会先打印原始请求，再打印处理结果，便于确认请求是否到达：

```text
POST /send_message received: {"pos": "insight", "event": 1}
Play assigned: device=insight, file=计算中心.wav, clients=1
send_message: pos=insight event=1 -> device=insight, file=计算中心.wav, ok=True
```

返回示例：

```json
{"ok":true,"handled":true,"pos":"insight","event":1,
 "results":[{"device":"insight","file":"...\\tools\\wavs\\计算中心.wav","ok":true}]}
```

设备同一段语音还在播时，重复上报不会从头重播（返回 `"skipped":true`）；需要强制重播时传 `"restart": true`。目标设备离线时分配会被保存，设备重连后自动播放。

自定义 `pos`/`event` 映射：复制 `tools/messages.example.json` 为 `tools/messages.json` 后启动时指定：

```powershell
python tools/ws_upload_server.py --messages tools/messages.json
```

```json
{
  "front":   { "1": { "file": "计算中心.wav", "device": "insight" } },
  "hall_01": { "1": { "file": "悬溺.wav", "device": "hall_01" } }
}
```

值可以直接写 `tools/wavs/` 下的文件名，也可以写 `{file, device}` 对象显式指定目标设备；`file` 支持相对路径（以 `tools/` 为基准）和绝对路径。映射文件里的规则优先于内置的 `event` 查表。

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

支持的源文件格式：wav、mp3、flac、m4a、m4b、m4r、aac、ogg、opus、wma、mp4、m4v、mka、webm、aif/aiff、amr、ape、wv、3gp。
脚本会先切换到自身所在目录再用相对路径（`source/`、`wavs/`）处理，所以从任何工作目录运行结果一致；
转换先写入 `*.wav.part`，成功后才会替换目标文件，失败时不会留下残缺的 WAV，并会打印 FFmpeg 的具体报错。

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
