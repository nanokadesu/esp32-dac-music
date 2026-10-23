"""Send POST /send_message to the playback server (device event report).

Examples:
    python tools/sim_device.py                            # pos=front, event=1
    python tools/sim_device.py --pos room --event 1
    python tools/sim_device.py --pos front --event 1 --restart
"""
import argparse, json, sys, urllib.error, urllib.request


def send_message(host, port, pos, event, restart, timeout):
    payload = {'pos': pos, 'event': event}
    if restart:
        payload['restart'] = True
    request = urllib.request.Request(f'http://{host}:{port}/send_message',
                                     data=json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json; charset=utf-8'},
                                     method='POST')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', 'replace')
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def main():
    parser = argparse.ArgumentParser(description='POST /send_message to the playback server.')
    parser.add_argument('--host', default='192.168.18.100', help='server address (default: 192.168.18.100)')
    parser.add_argument('--port', type=int, default=8080, help='HTTP port (default: 8080)')
    parser.add_argument('--pos', default='front', help='target device id, i.e. its DEVICE_ID (default: front)')
    parser.add_argument('--event', type=int, default=1, help='event/audio number (default: 1)')
    parser.add_argument('--restart', action='store_true', help='force a replay even if it is already playing')
    parser.add_argument('--timeout', type=float, default=5.0, help='request timeout in seconds (default: 5)')
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

    payload = {'pos': args.pos, 'event': args.event}
    if args.restart:
        payload['restart'] = True
    print(f'POST http://{args.host}:{args.port}/send_message {json.dumps(payload, ensure_ascii=False)}', flush=True)
    try:
        status, result = send_message(args.host, args.port, args.pos, args.event, args.restart, args.timeout)
    except OSError as exc:
        print(f'cannot reach {args.host}:{args.port}: {exc}', flush=True)
        print(f'is the server running there? python tools\\ws_upload_server.py --host 0.0.0.0 --http-port {args.port}', flush=True)
        raise SystemExit(1)
    print(f'HTTP {status} ' + (json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)), flush=True)
    if status != 200:
        raise SystemExit(1)
    if isinstance(result, dict) and result.get('handled') is False:
        print('note: no mapping for this pos/event, nothing was played', flush=True)
    elif isinstance(result, dict) and result.get('skipped'):
        print('note: already playing the same audio; add --restart to force a replay', flush=True)


if __name__ == '__main__':
    main()