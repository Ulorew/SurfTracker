#!/usr/bin/env python3
import asyncio

import cv2
import numpy as np
import websockets
import logging
import threading
import socket
import json
import os
import time
from typing import Optional, Callable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

WS_PORT = 8765
ANNOUNCE_PORT = 9999
ANNOUNCE_INTERVAL = 2.0
BCAST_ADDR = "255.255.255.255"
DOWNLOAD_DIR = "download"
TOKEN = "secret-token"

# Event to stop announcer thread cleanly
_stop_announce = threading.Event()
camera_callback: Optional[Callable] = None
meta_callback: Optional[Callable] = None


def register_camera_callback(callback):
    global camera_callback
    camera_callback = callback


def register_meta_callback(callback):
    global meta_callback
    meta_callback = callback



def get_local_ip() -> str:
    """Return a suitable local IPv4 address (by talking to the internet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def announce_ip(interval: float = ANNOUNCE_INTERVAL, port: int = ANNOUNCE_PORT, bcast: str = BCAST_ADDR):
    """Thread function: periodically broadcast an announce JSON on UDP."""
    logging.info("Announcer thread starting")
    # Ensure download dir exists (not required here but good practice)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # Prepare UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # We don't bind because we only send
        # sock.bind(("", 0))
    except Exception:
        logging.exception("Failed to configure UDP socket")

    try:
        while not _stop_announce.is_set():
            try:
                # compute local ip each loop in case it changes
                local_ip = get_local_ip()
                ws_url = f"ws://{local_ip}:{WS_PORT}"
                msg = {"type": "tracker_announce", "ws": ws_url, "token": TOKEN, "ts": int(time.time() * 1000)}
                payload = json.dumps(msg).encode("utf-8")

                # send to global broadcast
                try:
                    sock.sendto(payload, (bcast, port))
                    logging.debug(f"Announce sent to {bcast}:{port} -> {ws_url}")
                except Exception:
                    logging.exception(f"Failed to send announce to {bcast}:{port}")

                # Optionally: try to also send to interface-specific broadcasts (best-effort)
                try:
                    for ifname in socket.if_nameindex():
                        # if_nameindex returns tuples (index, name)
                        name = ifname[1]
                        try:
                            addrs = socket.getaddrinfo(local_ip, None)
                        except Exception:
                            addrs = []
                        # We skip complex per-interface broadcast resolution to keep dependency-free.
                    # If you need per-interface broadcast, consider `netifaces` package.
                except Exception:
                    # non-critical
                    pass

            except Exception:
                logging.exception("Announce loop error (continuing)")

            # Wait with early exit possibility
            _stop_announce.wait(interval)

    finally:
        try:
            sock.close()
        except Exception:
            pass
        logging.info("Announcer thread exiting")


def jpeg_bytes_to_bgr_image(jpeg_bytes: bytes):
    arr = np.frombuffer(jpeg_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)  # BGR
    return img  # None если невалидный jpeg


async def handler(ws, path=None):
    """WebSocket handler — сохраняет бинарные сообщения и отвечает на auth."""
    global camera_callback
    peer = getattr(ws, "remote_address", None)
    logging.info(f"WS client connected: {peer}")
    counter = 0
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    try:
        async for msg in ws:
            try:
                if isinstance(msg, bytes):
                    logging.info(f"Image {counter} from {peer}")
                    counter += 1
                    img_bgr = jpeg_bytes_to_bgr_image(msg)
                    if img_bgr is None:
                        return None

                    if camera_callback is not None:
                        camera_callback(img_bgr)

                else:
                    logging.info(f"Text from {peer}: {msg}")
                    try:
                        data = json.loads(msg)
                        if data.get("type") == "frame_meta":
                            if meta_callback is not None:
                                meta_callback(data)
                        if data.get("type") == "auth":
                            # optional: validate token from client if needed
                            await ws.send(json.dumps({"type": "auth_ok"}))
                    except json.JSONDecodeError:
                        logging.debug("Received non-JSON text from client")
                    except Exception:
                        logging.exception("Failed to parse/process text message")
            except Exception:
                logging.exception("Error processing one ws message")
    except websockets.ConnectionClosed as e:
        logging.info(f"WS connection closed {peer} code={e.code} reason={e.reason}")
    except Exception:
        logging.exception("Top-level ws handler error")
    finally:
        logging.info(f"Client disconnected: {peer}")



async def main():
    # Start announcer thread (pass function, not call it)
    t = threading.Thread(target=announce_ip, daemon=True)
    t.start()

    logging.info("Starting WS server")
    async with websockets.serve(handler, "0.0.0.0", WS_PORT, max_size=50 * 1024 * 1024):
        logging.info(f"WS server listening on 0.0.0.0:{WS_PORT}")
        try:
            await asyncio.Future()  # run forever
        finally:
            # when shutting down, signal announcer to stop
            _stop_announce.set()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Interrupted by user — exiting")
