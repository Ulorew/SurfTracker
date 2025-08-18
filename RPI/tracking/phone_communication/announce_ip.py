# announce_bcast.py
import socket, time, json


def get_local_ip():
    """Возвращает локальный IP, связанный с default route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


BCAST_ADDR = '255.255.255.255'
PORT = 9999
WS_PORT = 8765
INTERVAL = 1.0  # сек

ws_url = f"ws://{get_local_ip()}:{WS_PORT}"  # или вычисляй get_local_ip()
token = "secret-token"

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.settimeout(0.5)

msg = {"type": "tracker_announce", "ws": ws_url, "token": token}

try:
    while True:
        msg['ts'] = int(time.time() * 1000)
        b = json.dumps(msg).encode('utf-8')
        try:
            sock.sendto(b, (BCAST_ADDR, PORT))
        except Exception as e:
            print("send error", e)
        time.sleep(INTERVAL)
except KeyboardInterrupt:
    pass
finally:
    sock.close()
