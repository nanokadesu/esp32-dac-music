#coding=utf-8
"""HTTP-controlled, request-driven WebSocket WAV streamer."""
import argparse, asyncio, json, os, subprocess, wave, unicodedata, re
import websockets
from aiohttp import web

class Controller:
    def __init__(self, file_path, chunk_frames):
        self.default_file = os.path.abspath(file_path) if file_path else None
        self.chunk_frames = chunk_frames
        self.clients = set(); self.sessions = {}
        self.devices = {}
        self.device_files = {}
        self.device_generations = {}
        self.device_states = {}
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
        def resolve_file(value):
            path = os.path.abspath(str(value or ''))
            if os.path.isfile(path):
                return path
            wanted = unicodedata.normalize('NFC', os.path.basename(path)).casefold()
            root = os.path.join(os.path.dirname(__file__), 'wavs')
            for name in os.listdir(root) if os.path.isdir(root) else []:
                if unicodedata.normalize('NFC', name).casefold() == wanted:
                    candidate = os.path.abspath(os.path.join(root, name))
                    if os.path.isfile(candidate):
                        return candidate
            return None

        # A playlist maps individual devices to individual audio files.
        # Existing {file, device} requests remain supported; {file, devices}
        # broadcasts one file to a selected set of devices.
        if isinstance(data.get('items'), list):
            requests = data['items']
        else:
            devices = data.get('devices')
            if isinstance(devices, list):
                requests = [{'device': device, 'file': data.get('file')} for device in devices]
            else:
                requests = [{'device': data.get('device'), 'file': data.get('file')}]

        prepared = []
        for item in requests:
            if not isinstance(item, dict) or not item.get('file'):
                return web.json_response({'ok': False, 'error': 'each play item needs a file and optional device'}, status=400)
            path = resolve_file(item['file'])
            if not path:
                return web.json_response({'ok': False, 'error': 'file not found', 'requested': item['file']}, status=404)
            target = str(item['device']).strip() if item.get('device') is not None else None
            prepared.append((target or None, path))

        requested_devices = [target for target, _ in prepared if target]
        if len(requested_devices) != len(set(requested_devices)):
            return web.json_response({'ok': False, 'error': 'a device may appear only once in a playlist'}, status=400)

        results = []
        for target, path in prepared:
            selected = list(self.clients)
            if target:
                self.device_files[target] = path
                self.device_states[target] = 'starting'
                selected = [ws for ws in selected if self.devices.get(ws) == target and target != 'unknown']
                if not selected:
                    results.append({'device': target, 'file': path, 'ok': True, 'pending': True,
                                    'error': 'device offline; assignment saved for reconnect'})
                    continue
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

        assigned = sum(1 for result in results if result['ok'])
        notified = sum(1 for result in results if result['ok'] and not result.get('pending'))
        queued = sum(1 for result in results if result.get('pending'))
        return web.json_response({'ok': assigned == len(results) and bool(results), 'results': results,
                                  'clients_notified': notified, 'offline_assignments': queued})

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

    async def stop(self, request):
        for ws in list(self.clients):
            await ws.close(code=1000, reason='stopped')
        return web.json_response({'ok':True})

async def main_async(args):
    ctl=Controller(args.file,args.chunk_frames)
    app=web.Application(); app.router.add_post('/play',ctl.play); app.router.add_post('/stop',ctl.stop); app.router.add_get('/status',ctl.status)
    runner=web.AppRunner(app); await runner.setup(); await web.TCPSite(runner,args.host,args.http_port).start()
    async with websockets.serve(ctl.ws_handler,args.host,args.port,ping_interval=None):
        print(f'WS: ws://{args.host}:{args.port}/stream; HTTP: http://{args.host}:{args.http_port}',flush=True)
        await asyncio.Future()

def main():
    p=argparse.ArgumentParser(); p.add_argument('-f','--file',default=None); p.add_argument('--host',default='0.0.0.0'); p.add_argument('--port',type=int,default=8000); p.add_argument('--http-port',type=int,default=8080); p.add_argument('--chunk-frames',type=int,default=0); a=p.parse_args(); asyncio.run(main_async(a))
if __name__=='__main__': main()


