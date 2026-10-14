#coding=utf-8
"""HTTP-controlled, request-driven WebSocket WAV streamer."""
import argparse, asyncio, json, os, subprocess, wave, unicodedata, re
import websockets
from aiohttp import web

class Controller:
    def __init__(self, file_path, chunk_frames):
        self.file_path = os.path.abspath(file_path) if file_path else None
        self.chunk_frames = chunk_frames
        self.clients = set(); self.sessions = {}
        self.generation = 0
        self.lock = asyncio.Lock()

    async def send_chunk(self, ws, wf, index):
        data = wf.readframes(self.frames(wf))
        if not data: return False
        await ws.send(json.dumps({'type':'chunk','index':index,'length':len(data),'last':False}))
        await ws.send(data)
        return True

    def frames(self, wf):
        bpf = wf.getnchannels() * wf.getsampwidth()
        n = self.chunk_frames or max(1, 16384 // max(1, bpf))
        return min(n, max(1, 16384 // max(1, bpf)))

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
                    if not self.file_path or not os.path.isfile(self.file_path):
                        await ws.send(json.dumps({'type':'error','message':'no file selected'}))
                        continue
                    window = msg.get('window', 0)
                    if not isinstance(window, int) or not 1 <= window <= 32: continue
                    with wave.open(self.file_path, 'rb') as wf:
                        await ws.send(json.dumps({'type':'info','channels':wf.getnchannels(),'sample_width':wf.getsampwidth(),'sample_rate':wf.getframerate()}))
                        expected = 0
                        for _ in range(window):
                            if not await self.send_chunk(ws, wf, expected):
                                await ws.send(json.dumps({'type':'end','total':expected})); break
                            expected += 1
                        self.sessions[ws] = expected
                elif typ == 'next':
                    # Keep a per-connection cursor in the websocket object.
                    expected = self.sessions.get(ws)
                    if expected is None: continue
                    if msg.get('index') != expected:
                        await ws.send(json.dumps({'type':'error','message':'unexpected index','expected':expected})); continue
                    with wave.open(self.file_path, 'rb') as wf:
                        wf.setpos(expected * self.frames(wf))
                        if await self.send_chunk(ws, wf, expected): expected += 1
                        else: await ws.send(json.dumps({'type':'end','total':expected}))
                    self.sessions[ws] = expected
        except Exception as exc: print('Connection error:', repr(exc), flush=True)
        finally:
            self.clients.discard(ws); self.sessions.pop(ws, None); print('Connection finished', flush=True)

    async def play(self, request):
        raw_body = await request.read()
        try:
            body = raw_body.decode('utf-8')
        except UnicodeDecodeError:
            body = raw_body.decode('gb18030')
        # Extract the path directly as a compatibility fallback for PowerShell
        # bodies containing unescaped Windows backslashes (e.g. "E:\\music").
        match = re.search(r'"file"\s*:\s*"(.*)"', body, re.DOTALL)
        if match:
            requested_file = re.sub(
                r'\\u([0-9a-fA-F]{4})',
                lambda m: chr(int(m.group(1), 16)),
                match.group(1),
            ).replace('\\', '/')
            data = {'file': requested_file}
        else:
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = json.loads(body.replace('\\', '/'))
        path = os.path.abspath(data.get('file',''))
        if not os.path.isfile(path):
            # Recover from Unicode normalization or a client-side path mismatch
            # by matching the requested filename in tools/wavs.
            wanted = unicodedata.normalize('NFC', os.path.basename(path)).casefold()
            root = os.path.join(os.path.dirname(__file__), 'wavs')
            for name in os.listdir(root) if os.path.isdir(root) else []:
                if unicodedata.normalize('NFC', name).casefold() == wanted:
                    path = os.path.abspath(os.path.join(root, name)); break
        if not os.path.isfile(path):
            return web.json_response({'ok':False,'error':'file not found','requested':path}, status=404)
        self.file_path = path
        self.generation += 1
        notified = 0
        old_clients = list(self.clients)
        for ws in old_clients:
            self.sessions[ws] = None
        for ws in old_clients:
            try:
                await ws.send(json.dumps({'type':'play','generation':self.generation}))
                notified += 1
            except Exception:
                pass
        if notified:
            print(f'Play command sent to {notified} client(s)', flush=True)
        return web.json_response({'ok':True,'file':path,'clients_notified':notified,'generation':self.generation})

    async def status(self, request):
        return web.json_response({'file':self.file_path,'clients':len(self.clients)})

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
