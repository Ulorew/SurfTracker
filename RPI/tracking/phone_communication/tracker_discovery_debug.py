# tracker_minimal_debug.py
# pip install websockets

import asyncio, websockets, logging, threading, socket, json, os, time

import cv2
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
WS_PORT = 8765
DISCOVERY_PORT = 9999
DISCOVER_MSG = b"DISCOVER_TRACKER"

def get_local_ip():
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
    local_ip = get_local_ip()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
    except Exception as e:
        logging.exception("UDP bind failed")
        return
    logging.info(f"UDP discovery responder listening on 0.0.0.0:{DISCOVERY_PORT} (advertise ws://{local_ip}:{WS_PORT})")
    while True:
        try:
            data, addr = sock.recvfrom(4096)
            if data.strip() == DISCOVER_MSG:
                resp = json.dumps({"ws": f"ws://{local_ip}:{WS_PORT}"})
                sock.sendto(resp.encode("utf-8"), addr)
                logging.info(f"Responded to discovery from {addr}")
        except Exception:
            logging.exception("UDP responder error")

async def handler(ws, path=None):
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
    try:
        async for msg in ws:
            try:
                if isinstance(msg, bytes):
                    img_bgr = jpeg_bytes_to_bgr_image(msg)
                    if img_bgr is None:
                        return None
                    cv2.imwrite("test_frame.jpg", img_bgr)
                    logging.info("Saved test_frame.jpg")

                    # logging.info(f"Saved {fname} ({len(msg)} bytes) from {peer}")
                    # await ws.send(json.dumps({"ack": True, "saved": fname}))
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
    t = threading.Thread(target=udp_responder, daemon=True)
    t.start()
    logging.info("Starting WS server")
    async with websockets.serve(handler, "0.0.0.0", WS_PORT, max_size=50*1024*1024):
        logging.info(f"WS server listening on 0.0.0.0:{WS_PORT}")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
