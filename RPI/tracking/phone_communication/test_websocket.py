
import asyncio
import websockets
import socket
import threading
import json
import logging

logging.basicConfig(level=logging.INFO)

WS_PORT = 8765
DISCOVERY_PORT = 9999
DISCOVER_MSG = b"DISCOVER_TRACKER"

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

def udp_responder():
    """Блокирующий UDP слушатель, отвечает на broadcast DISCOVER_TRACKER."""
    local_ip = get_local_ip()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", DISCOVERY_PORT))
    logging.info(f"UDP discovery responder listening on 0.0.0.0:{DISCOVERY_PORT} (advertise ws://{local_ip}:{WS_PORT})")
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            if data.strip() == DISCOVER_MSG:
                resp = json.dumps({"ws": f"ws://{local_ip}:{WS_PORT}"})
                sock.sendto(resp.encode("utf-8"), addr)
                logging.info(f"Responded to discovery from {addr}")
        except Exception as e:
            logging.exception("UDP responder error")

async def ws_handler(ws, path=None):
    """
    Совместимая обёртка: websockets старых версий вызывали handler(ws, path),
    новых — handler(connection). path может быть None.
    """
    # в новых версиях объект называется 'ws' или 'connection' — используем как есть
    peer = None
    try:
        # попытка безопасно достать remote_address (у объекта есть remote_address в обеих версиях)
        peer = getattr(ws, "remote_address", None)
    except Exception:
        peer = None

    logging.info(f"WS client connected: {peer}")
    counter = 0
    try:
        async for msg in ws:
            try:
                if isinstance(msg, bytes):
                    counter += 1
                    fname = f"last_recv_{int(time.time())}_{counter}.bin"
                    with open(fname, "wb") as f:
                        f.write(msg)
                    logging.info(f"Saved {fname} ({len(msg)} bytes) from {peer}")
                    await ws.send(json.dumps({"ack": True, "saved": fname}))
                else:
                    logging.info(f"Text from {peer}: {msg}")
                    try:
                        data = json.loads(msg)
                        if data.get("type") == "auth":
                            await ws.send(json.dumps({"type":"auth_ok"}))
                    except Exception:
                        logging.exception("Failed to parse text msg")
            except Exception:
                logging.exception("Error processing one ws message")
    except websockets.ConnectionClosed as e:
        logging.info(f"WS connection closed {peer} code={e.code} reason={e.reason}")
    except Exception:
        logging.exception("Top-level ws handler error")
    finally:
        logging.info(f"Client disconnected: {peer}")

async def main():
    # старт WS сервера
    server = await websockets.serve(ws_handler, "0.0.0.0", WS_PORT, max_size=10*1024*1024)
    logging.info(f"WS server listening on 0.0.0.0:{WS_PORT}")

    # просто держим сервер живым
    await asyncio.Future()

if __name__ == "__main__":
    # старт UDP responder в отдельном треде
    t = threading.Thread(target=udp_responder, daemon=True)
    t.start()
    # старт asyncio loop с ws сервером
    asyncio.run(main())

