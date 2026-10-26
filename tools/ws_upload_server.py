#coding=utf-8
"""HTTP-controlled, request-driven WebSocket WAV streamer."""
import argparse, asyncio, json, os, shutil, subprocess, wave, unicodedata, re
import websockets
from aiohttp import web
try:
    import yaml
except ImportError:  # only needed for the player page's config.yaml
    yaml = None

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
WAV_DIR = os.path.join(TOOLS_DIR, 'wavs')
SOURCE_DIR = os.path.join(TOOLS_DIR, 'source')
CONFIG_PATH = os.path.join(TOOLS_DIR, 'config.yaml')
PLAYER_PAGE = os.path.join(TOOLS_DIR, 'player.html')
AUDIO_EXTENSIONS = ('.wav', '.mp3', '.flac', '.m4a', '.m4b', '.m4r', '.aac', '.ogg', '.opus',
                    '.wma', '.mp4', '.m4v', '.mka', '.webm', '.aif', '.aiff', '.amr', '.ape', '.wv', '.3gp')
WAV_SAMPLE_RATE = 16000
WAV_CHANNELS = 2
# Used when tools/config.yaml is absent, so GET / always has rows to show.
DEFAULT_DEVICES = [
    {'name': 'front', 'alias': '前门'},
    {'name': 'insight', 'alias': '内屋'},
    {'name': 'room', 'alias': '外屋'},
]

def load_devices():
    """Load the player page device list (name + alias) from tools/config.yaml."""
    if not os.path.isfile(CONFIG_PATH):
        return [dict(device) for device in DEFAULT_DEVICES]
    if yaml is None:
        raise RuntimeError('PyYAML is required to read config.yaml; run: pip install -r requirements.txt')
    with open(CONFIG_PATH, 'r', encoding='utf-8') as fp:
        data = yaml.safe_load(fp) or {}
    entries = data.get('devices') if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise RuntimeError('config.yaml must contain a "devices" list')
    devices, seen = [], set()
    for entry in entries:
        if isinstance(entry, str):
            name, alias = entry.strip(), ''
        elif isinstance(entry, dict):
            name = str(entry.get('name') or entry.get('id') or '').strip()
            alias = str(entry.get('alias') or '').strip()
        else:
            continue
        if not name or name in seen:
            continue
        seen.add(name)
        devices.append({'name': name, 'alias': alias})
    return devices

def find_ffmpeg(explicit=None):
    """Locate the system FFmpeg executable via --ffmpeg, FFMPEG_PATH or PATH."""
    for candidate in (explicit, os.environ.get('FFMPEG_PATH'), 'ffmpeg'):
        if not candidate:
            continue
        found = shutil.which(candidate)
        if found:
            return found
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None

async def convert_to_wav(ffmpeg, source, target):
    """Convert one audio file to 16 kHz stereo PCM WAV.

    FFmpeg writes to "<target>.part" and the result is moved into place only
    after a successful exit, so a failed run never publishes a truncated WAV.
    """
    part = target + '.part'
    cmd = [ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error', '-y', '-i', source,
           '-map', '0:a:0', '-vn', '-sn', '-dn', '-map_metadata', '-1',
           '-ar', str(WAV_SAMPLE_RATE), '-ac', str(WAV_CHANNELS), '-c:a', 'pcm_s16le', '-f', 'wav', part]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        try: os.remove(part)
        except OSError: pass
        lines = stderr.decode('utf-8', 'replace').strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f'ffmpeg exited with {proc.returncode}')
    os.replace(part, target)

def resolve_file(value):
    """Resolve a WAV path to an existing file.

    Relative paths are resolved against the process working directory, the
    tools directory and tools/wavs, so clients never have to send absolute
    host paths. A bare file name is also matched inside tools/wavs.
    """
    text = str(value or '').strip().strip('"\'')
    if not text:
        return None
    raw = text.replace('\\', os.sep).replace('/', os.sep)
    candidates = [raw] if os.path.isabs(raw) else [
        os.path.join(os.getcwd(), raw),
        os.path.join(TOOLS_DIR, raw),
        os.path.join(WAV_DIR, raw),
    ]
    for candidate in candidates:
        candidate = os.path.abspath(candidate)
        if os.path.isfile(candidate):
            return candidate
    wanted = unicodedata.normalize('NFC', os.path.basename(raw)).casefold()
    for name in os.listdir(WAV_DIR) if os.path.isdir(WAV_DIR) else []:
        if unicodedata.normalize('NFC', name).casefold() == wanted:
            candidate = os.path.abspath(os.path.join(WAV_DIR, name))
            if os.path.isfile(candidate):
                return candidate
    return None

def resolve_config(value):
    """Resolve a JSON config path against the working and tools directories."""
    raw = str(value or '').strip().replace('\\', os.sep).replace('/', os.sep)
    if not raw:
        return None
    candidates = [raw] if os.path.isabs(raw) else [os.path.join(os.getcwd(), raw), os.path.join(TOOLS_DIR, raw)]
    for candidate in candidates:
        candidate = os.path.abspath(candidate)
        if os.path.isfile(candidate):
            return candidate
    return None

def load_messages(value):
    """Load the pos/event -> audio mapping used by POST /send_message."""
    path = resolve_config(value)
    if not path:
        raise SystemExit(f'messages file not found: {value}')
    with open(path, 'r', encoding='utf-8') as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise SystemExit(f'messages file must contain a JSON object: {path}')
    print(f'Message mapping: {path}', flush=True)
    return data

# Built-in fallback used when the mapping file has no matching pos/event.
BUILTIN_MESSAGES = {'1': '计算中心.wav'}

class Controller:
    def __init__(self, file_path, chunk_frames, messages=None, source_dir=None, ffmpeg_path=None):
        self.default_file = resolve_file(file_path) if file_path else None
        self.chunk_frames = chunk_frames
        self.clients = set(); self.sessions = {}
        self.devices = {}
        self.device_files = {}
        self.device_generations = {}
        self.device_states = {}
        self.messages = messages or {}
        self.source_dir = source_dir or SOURCE_DIR
        self.ffmpeg_path = ffmpeg_path
        self.lock = asyncio.Lock()

    async def send_chunk(self, ws, wf, index):
        data = wf.readframes(self.frames(wf))
        if not data: return False
        await ws.send(json.dumps({'type':'chunk','index':index,'length':len(data),'last':False}))
        await ws.send(data)
        return True

    def frames(self, wf):
        bpf = wf.getnchannels() * wf.getsampwidth()
        # Keep each PCM allocation below the ESP32's largest reliable heap
        # block while allowing three chunks to remain resident.
        max_chunk_bytes = 12288
        n = self.chunk_frames or max(1, max_chunk_bytes // max(1, bpf))
        return min(n, max(1, max_chunk_bytes // max(1, bpf)))

    async def ws_handler(self, ws):
        self.clients.add(ws)
        print('Client connected:', ws.remote_address, flush=True)
        try:
            async for raw in ws:
                print('RX:', repr(raw), flush=True)
                if isinstance(raw, bytes): continue
                try: msg = json.loads(raw)
                except json.JSONDecodeError: continue
                typ = msg.get('type')
                if typ == 'play_ack':
                    print('PLAY ACK:', msg, flush=True)
                    continue
                if typ == 'start':
                    # Register the device before checking whether a file has
                    # been selected. This keeps /status accurate during the
                    # idle state and prevents targeted commands matching an
                    # unknown connection.
                    device = str(msg.get('device', 'unknown')).strip() or 'unknown'
                    self.devices[ws] = device
                    self.device_states[device] = 'ready'
                    print(f'Device registered: {device}', flush=True)
                    file_path = self.device_files.get(device, self.default_file)
                    if not file_path or not os.path.isfile(file_path):
                        await ws.send(json.dumps({'type':'error','message':'no file selected'}))
                        continue
                    window = msg.get('window', 0)
                    if not isinstance(window, int) or not 1 <= window <= 32: continue
                    with wave.open(file_path, 'rb') as wf:
                        self.device_states[device] = 'streaming'
                        print(f'Device registered: {device}; streaming {os.path.basename(file_path)}', flush=True)
                        await ws.send(json.dumps({'type':'info','channels':wf.getnchannels(),'sample_width':wf.getsampwidth(),'sample_rate':wf.getframerate()}))
                        expected = 0
                        finished = False
                        for _ in range(window):
                            if not await self.send_chunk(ws, wf, expected):
                                await ws.send(json.dumps({'type':'end','total':expected})); finished = True; break
                            expected += 1
                            await asyncio.sleep(0)
                        self.sessions[ws] = {'expected': expected, 'file': file_path, 'finished': finished}
                        if finished:
                            self.device_states[device] = 'finished'
                elif typ == 'next':
                    session = self.sessions.get(ws)
                    if session is None: continue
                    if session.get('finished'):
                        # The client may retry the final request. Do not seek
                        # past EOF or tear down the control connection.
                        continue
                    expected = session['expected']
                    if msg.get('index') != expected:
                        await ws.send(json.dumps({'type':'error','message':'unexpected index','expected':expected})); continue
                    try:
                        with wave.open(session['file'], 'rb') as wf:
                            frame_pos = expected * self.frames(wf)
                            total_frames = wf.getnframes()
                            if frame_pos >= total_frames:
                                has_chunk = False
                            else:
                                wf.setpos(min(frame_pos, total_frames))
                                has_chunk = await self.send_chunk(ws, wf, expected)
                        if has_chunk:
                            expected += 1
                        else:
                            await ws.send(json.dumps({'type':'end','total':expected}))
                            session['finished'] = True
                            self.device_states[self.devices.get(ws, 'unknown')] = 'finished'
                    except (EOFError, wave.Error, ValueError, OSError) as exc:
                        print(f'INFO: end of stream for {self.devices.get(ws, "unknown")}: {exc}', flush=True)
                        await ws.send(json.dumps({'type':'end','total':expected}))
                        session['finished'] = True
                        self.device_states[self.devices.get(ws, 'unknown')] = 'finished'
                    session['expected'] = expected
        except Exception as exc: print('Connection error:', repr(exc), flush=True)
        finally:
            device = self.devices.pop(ws, None)
            self.clients.discard(ws)
            self.sessions.pop(ws, None)
            if device and not any(self.devices.get(client) == device for client in self.clients):
                self.device_states[device] = 'offline'
            print('Connection finished', flush=True)

    async def play(self, request):
        raw_body = await request.read()
        try:
            body = raw_body.decode('utf-8')
        except UnicodeDecodeError:
            body = raw_body.decode('gb18030')
        # ConvertTo-Json sends valid JSON. Keep a field-level fallback for
        # clients that send raw Windows backslashes, without swallowing device.
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            match = re.search(r'"file"\s*:\s*"(.*?)"\s*,\s*"device"', body, re.DOTALL)
            if not match:
                return web.json_response({'ok': False, 'error': 'invalid JSON'}, status=400)
            requested_file = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), match.group(1))
            requested_file = requested_file.replace('\\\\', '\\').replace('\\', os.sep)
            data = {'file': requested_file}
            dmatch = re.search(r'"device"\s*:\s*"(.*?)"', body, re.DOTALL)
            if dmatch:
                data['device'] = dmatch.group(1)

        # A playlist maps individual devices to individual audio files.
        # Existing {file, device} requests remain supported; {file, devices}
        # broadcasts one file to a selected set of devices. An item (or the
        # whole request) may instead carry "stop": true to silence one device.
        if isinstance(data.get('items'), list):
            requests = data['items']
        elif data.get('stop'):
            targets = data.get('devices') if isinstance(data.get('devices'), list) else [data.get('device')]
            requests = [{'device': target, 'stop': True} for target in targets]
        else:
            devices = data.get('devices')
            if isinstance(devices, list):
                requests = [{'device': device, 'file': data.get('file')} for device in devices]
            else:
                requests = [{'device': data.get('device'), 'file': data.get('file')}]

        prepared = []
        for item in requests:
            if not isinstance(item, dict):
                return web.json_response({'ok': False, 'error': 'each play item must be an object'}, status=400)
            target = str(item['device']).strip() if item.get('device') is not None else None
            if item.get('stop'):
                if not target:
                    return web.json_response({'ok': False, 'error': 'stopping audio requires a device'}, status=400)
                prepared.append((target, None))
                continue
            if not item.get('file'):
                return web.json_response({'ok': False, 'error': 'each play item needs a file, a stop flag and optional device'}, status=400)
            path = resolve_file(item['file'])
            if not path:
                return web.json_response({'ok': False, 'error': 'file not found', 'requested': item['file']}, status=404)
            prepared.append((target or None, path))

        requested_devices = [target for target, _ in prepared if target]
        if len(requested_devices) != len(set(requested_devices)):
            return web.json_response({'ok': False, 'error': 'a device may appear only once in a playlist'}, status=400)

        results = []
        for target, path in prepared:
            if path is None:
                results.extend(await self.stop_device(target))
            else:
                results.extend(await self.assign(target, path))

        assigned = sum(1 for result in results if result['ok'])
        notified = sum(1 for result in results if result['ok'] and not result.get('pending'))
        queued = sum(1 for result in results if result.get('pending'))
        return web.json_response({'ok': assigned == len(results) and bool(results), 'results': results,
                                  'clients_notified': notified, 'offline_assignments': queued})

    async def assign(self, target, path):
        """Point one device (or every connected device) at a WAV and notify it."""
        results = []
        selected = list(self.clients)
        if target:
            self.device_files[target] = path
            self.device_states[target] = 'starting'
            selected = [ws for ws in selected if self.devices.get(ws) == target and target != 'unknown']
            if not selected:
                results.append({'device': target, 'file': path, 'ok': True, 'pending': True,
                                'error': 'device offline; assignment saved for reconnect'})
                print(f'Play assigned: device={target}, file={os.path.basename(path)}, clients=0 (offline, pending)', flush=True)
                return results
        # A missing target means broadcast to all currently registered devices.
        selected = [ws for ws in selected if self.devices.get(ws, 'unknown') != 'unknown']
        if not selected:
            results.append({'device': target or 'all', 'file': path, 'ok': False, 'error': 'no registered devices connected'})
        for ws in selected:
            device = self.devices[ws]
            self.device_files[device] = path
            self.device_states[device] = 'starting'
            generation = self.device_generations.get(device, 0) + 1
            self.device_generations[device] = generation
            self.sessions.pop(ws, None)
            try:
                await ws.send(json.dumps({'type': 'play', 'generation': generation}))
                results.append({'device': device, 'file': path, 'ok': True})
            except Exception as exc:
                results.append({'device': device, 'file': path, 'ok': False, 'error': str(exc)})
        print(f'Play assigned: device={target or "all"}, file={os.path.basename(path)}, clients={len(selected)}', flush=True)
        return results

    async def stop_device(self, device):
        """Silence one device and clear its assigned file.

        The ESP32 has no dedicated stop command, so sending "play" makes it
        drop the current buffer immediately; because the assignment is cleared
        its follow-up "start" gets "no file selected" and audio stays stopped.
        """
        self.device_files[device] = None
        self.device_states[device] = 'idle'
        generation = self.device_generations.get(device, 0) + 1
        self.device_generations[device] = generation
        results = []
        selected = [ws for ws in self.clients if self.devices.get(ws) == device]
        for ws in selected:
            self.sessions.pop(ws, None)
            try:
                await ws.send(json.dumps({'type': 'play', 'generation': generation}))
                results.append({'device': device, 'stop': True, 'ok': True})
            except Exception as exc:
                results.append({'device': device, 'stop': True, 'ok': False, 'error': str(exc)})
        if not selected:
            results.append({'device': device, 'stop': True, 'ok': True, 'pending': True,
                            'error': 'device offline; stop saved for reconnect'})
        print(f'Stop requested: device={device}, clients={len(selected)}', flush=True)
        return results

    def message_target(self, pos, event):
        """Map (pos, event) to (requested file, target device), or None."""
        key = str(event)
        table = self.messages.get(pos) if isinstance(self.messages, dict) else None
        entry = None
        if isinstance(table, dict):
            entry = table.get(key)
            if entry is None and isinstance(event, (str, int, float)):
                entry = table.get(event)
        if isinstance(entry, str):
            return entry, pos
        if isinstance(entry, dict) and entry.get('file'):
            device = str(entry.get('device') or pos).strip() or pos
            return str(entry['file']), device
        if key in BUILTIN_MESSAGES:
            return BUILTIN_MESSAGES[key], pos
        return None

    def device_is_playing(self, device, path):
        for ws in self.clients:
            if self.devices.get(ws) != device:
                continue
            session = self.sessions.get(ws)
            if session and not session.get('finished') and session.get('file') == path:
                return True
        return False

    async def send_message(self, request):
        raw_body = await request.read()
        try:
            body = raw_body.decode('utf-8')
        except UnicodeDecodeError:
            body = raw_body.decode('gb18030')
        print(f'POST /send_message received: {body}', flush=True)
        try:
            data = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError:
            print('send_message: invalid JSON', flush=True)
            return web.json_response({'ok': False, 'error': 'invalid JSON'}, status=400)
        if not isinstance(data, dict):
            print('send_message: body must be a JSON object', flush=True)
            return web.json_response({'ok': False, 'error': 'body must be a JSON object'}, status=400)
        pos = str(data.get('pos') or '').strip()
        event = data.get('event')
        if not pos:
            print('send_message: missing pos', flush=True)
            return web.json_response({'ok': False, 'error': 'pos is required'}, status=400)
        mapping = self.message_target(pos, event)
        if mapping is None:
            print(f'send_message: no mapping for pos={pos} event={event}; ignored', flush=True)
            return web.json_response({'ok': True, 'handled': False, 'pos': pos, 'event': event})
        requested, device = mapping
        path = resolve_file(requested)
        if not path:
            print(f'send_message: file not found for pos={pos} event={event}: {requested}', flush=True)
            return web.json_response({'ok': False, 'handled': True, 'pos': pos, 'event': event,
                                      'error': 'file not found', 'requested': requested}, status=404)
        # Repeating the same event must not restart an announcement that is
        # still playing; clients pass "restart": true to force a replay.
        if not data.get('restart') and self.device_is_playing(device, path):
            print(f'send_message: {device} already playing {os.path.basename(path)}; ignored (restart=true to replay)', flush=True)
            return web.json_response({'ok': True, 'handled': True, 'pos': pos, 'event': event, 'skipped': True,
                                      'results': [{'device': device, 'file': path, 'ok': True, 'skipped': True}]})
        results = await self.assign(device, path)
        ok = bool(results) and all(result['ok'] for result in results)
        print(f'send_message: pos={pos} event={event} -> device={device}, file={os.path.basename(path)}, ok={ok}', flush=True)
        return web.json_response({'ok': ok, 'handled': True, 'pos': pos, 'event': event, 'results': results})

    async def status(self, request):
        connected = {}
        for ws in self.clients:
            device = self.devices.get(ws, 'unknown')
            session = self.sessions.get(ws)
            path = session['file'] if session else self.device_files.get(device, self.default_file)
            connected[device] = {'id': device, 'file': path,
                                 'state': self.device_states.get(device, 'ready'), 'online': True,
                                 'playing': bool(session and not session.get('finished'))}
        for device in set(self.device_files) | set(self.device_states):
            if device not in connected:
                connected[device] = {'id': device, 'file': self.device_files.get(device),
                                     'state': 'offline', 'online': False}
        return web.json_response({'clients':len(self.clients), 'devices':list(connected.values())})

    async def player(self, request):
        """GET /: the 页面播放器 control page."""
        if not os.path.isfile(PLAYER_PAGE):
            return web.json_response({'ok': False, 'error': 'player.html not found'}, status=404)
        return web.FileResponse(PLAYER_PAGE, headers={'Cache-Control': 'no-cache'})

    async def config(self, request):
        """GET /api/config: device names and aliases from tools/config.yaml."""
        try:
            devices = load_devices()
        except Exception as exc:  # missing PyYAML, bad YAML and I/O errors alike
            return web.json_response({'ok': False, 'config': CONFIG_PATH, 'devices': [], 'error': str(exc)},
                                     status=500)
        return web.json_response({'ok': True, 'config': CONFIG_PATH, 'devices': devices})

    async def flush(self, request):
        """GET /api/flush: convert every audio file in tools/source to WAV.

        Files already converted and newer than their source are skipped unless
        "?force=1" is given.
        """
        ffmpeg = find_ffmpeg(self.ffmpeg_path)
        if not ffmpeg:
            return web.json_response({'ok': False, 'ffmpeg': None, 'source_dir': self.source_dir,
                                      'error': 'ffmpeg not found; install FFmpeg or pass --ffmpeg'}, status=503)
        if self.lock.locked():
            return web.json_response({'ok': False, 'error': 'conversion already running'}, status=409)
        force = str(request.query.get('force', '')).lower() in ('1', 'true', 'yes', 'on')
        async with self.lock:
            if not os.path.isdir(self.source_dir):
                return web.json_response({'ok': False, 'source_dir': self.source_dir,
                                          'error': 'source directory not found'}, status=404)
            os.makedirs(WAV_DIR, exist_ok=True)
            converted, skipped, failed = [], [], []
            for name in sorted(os.listdir(self.source_dir), key=lambda n: unicodedata.normalize('NFC', n).casefold()):
                path = os.path.join(self.source_dir, name)
                if not os.path.isfile(path) or os.path.splitext(name)[1].lower() not in AUDIO_EXTENSIONS:
                    continue
                target = os.path.join(WAV_DIR, os.path.splitext(name)[0] + '.wav')
                if not force and os.path.isfile(target) and os.path.getmtime(target) >= os.path.getmtime(path):
                    skipped.append(os.path.basename(target))
                    continue
                print(f'Converting: {name} -> {os.path.basename(target)}', flush=True)
                try:
                    await convert_to_wav(ffmpeg, path, target)
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    failed.append({'file': name, 'error': str(exc)})
                    print(f'Conversion failed: {name}: {exc}', flush=True)
                else:
                    converted.append(os.path.basename(target))
            print(f'Conversion finished: {len(converted)} converted, {len(skipped)} skipped, '
                  f'{len(failed)} failed', flush=True)
            return web.json_response({'ok': not failed, 'ffmpeg': ffmpeg, 'source_dir': self.source_dir,
                                      'wav_dir': WAV_DIR, 'count': len(converted),
                                      'converted': converted, 'skipped': skipped, 'failed': failed},
                                     status=200 if not failed else 500)

    async def list_wavs(self, request):
        """GET /api/wavs: list every WAV file in tools/wavs (never other formats)."""
        files = []
        names = os.listdir(WAV_DIR) if os.path.isdir(WAV_DIR) else []
        for name in sorted(names, key=lambda n: unicodedata.normalize('NFC', n).casefold()):
            if not name.lower().endswith('.wav'):
                continue
            path = os.path.join(WAV_DIR, name)
            if not os.path.isfile(path):
                continue
            try:
                stat = os.stat(path)
                with wave.open(path, 'rb') as wf:
                    rate = wf.getframerate()
                    frames = wf.getnframes()
                    entry = {'name': name, 'path': path, 'size': stat.st_size, 'mtime': int(stat.st_mtime),
                             'channels': wf.getnchannels(), 'sample_width': wf.getsampwidth(),
                             'sample_rate': rate, 'frames': frames,
                             'duration': round(frames / rate, 3) if rate else None}
            except (OSError, EOFError, wave.Error) as exc:
                entry = {'name': name, 'path': path, 'error': str(exc)}
            files.append(entry)
        return web.json_response({'ok': True, 'dir': WAV_DIR, 'count': len(files), 'files': files})

    async def stop(self, request):
        for ws in list(self.clients):
            await ws.close(code=1000, reason='stopped')
        return web.json_response({'ok':True})

async def main_async(args):
    ctl=Controller(args.file,args.chunk_frames,load_messages(args.messages) if args.messages else None,
                   source_dir=args.source,ffmpeg_path=args.ffmpeg)
    app=web.Application(); app.router.add_post('/play',ctl.play); app.router.add_post('/send_message',ctl.send_message); app.router.add_post('/stop',ctl.stop); app.router.add_get('/status',ctl.status)
    app.router.add_get('/api/flush',ctl.flush); app.router.add_get('/api/wavs',ctl.list_wavs)
    app.router.add_get('/',ctl.player); app.router.add_get('/index.html',ctl.player); app.router.add_get('/api/config',ctl.config)
    runner=web.AppRunner(app); await runner.setup(); await web.TCPSite(runner,args.host,args.http_port).start()
    async with websockets.serve(ctl.ws_handler,args.host,args.port,ping_interval=None):
        print(f'WS: ws://{args.host}:{args.port}/stream; HTTP: http://{args.host}:{args.http_port}',flush=True)
        await asyncio.Future()

def main():
    p=argparse.ArgumentParser(); p.add_argument('-f','--file',default=None); p.add_argument('--host',default='0.0.0.0'); p.add_argument('--port',type=int,default=8000); p.add_argument('--http-port',type=int,default=8080); p.add_argument('--chunk-frames',type=int,default=0); p.add_argument('--messages',default=None,help='JSON mapping of pos/event to audio files (see tools/messages.example.json)'); p.add_argument('--source',default=None,help='directory holding source audio for /api/flush (default: tools/source)'); p.add_argument('--ffmpeg',default=None,help='path to ffmpeg executable (default: FFMPEG_PATH or PATH)'); a=p.parse_args(); asyncio.run(main_async(a))
if __name__=='__main__': main()


