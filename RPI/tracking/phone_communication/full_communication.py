#!/usr/bin/env python3
import asyncio
import uuid
from statistics import mean

import cv2
import numpy as np
import websockets
import logging
import threading
import socket
import json
import os
import time
import netifaces
from typing import Optional, Callable

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

MS = lambda: time.time() * 1000.0  # ms as float with sub-ms


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


def jpeg_bytes_to_bgr_image(jpeg_bytes: bytes):
    arr = np.frombuffer(jpeg_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)  # BGR
    return img  # None если невалидный jpeg

def announce_ip(interval: float = ANNOUNCE_INTERVAL, port: int = ANNOUNCE_PORT):
    logging.info("Announcer thread starting (multi-if, safer)")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    while not _stop_announce.is_set():
        try:
            interfaces = netifaces.interfaces()
            # keep track of global targets we already sent to (avoid duplicates)
            seen_targets = set()

            for ifname in interfaces:
                try:
                    addrs = netifaces.ifaddresses(ifname)
                    inet_info = addrs.get(netifaces.AF_INET)
                    if not inet_info:
                        continue

                    for addrinfo in inet_info:
                        addr = addrinfo.get('addr')
                        bcast = addrinfo.get('broadcast')  # may be None
                        netmask = addrinfo.get('netmask')
                        if not addr:
                            continue
                        # skip loopback addresses
                        if addr.startswith("127."):
                            continue

                        ws_url = f"ws://{addr}:{WS_PORT}"
                        msg = {"type": "tracker_announce", "ws": ws_url, "token": TOKEN, "ts": int(time.time() * 1000)}
                        payload = json.dumps(msg).encode("utf-8")

                        # build list of targets to try for this iface
                        targets = []
                        if bcast:
                            targets.append((bcast, port))
                        else:
                            # try compute broadcast from addr + netmask
                            try:
                                import ipaddress
                                if netmask:
                                    ipi = ipaddress.IPv4Interface(f"{addr}/{netmask}")
                                    baddr = str(ipi.network.broadcast_address)
                                    targets.append((baddr, port))
                            except Exception:
                                pass

                        # always add global broadcast as fallback (but once per loop)
                        targets.append((BCAST_ADDR, port))

                        # send on per-interface socket (create and close)
                        for tgt in targets:
                            if tgt in seen_targets:
                                continue
                            seen_targets.add(tgt)
                            try:
                                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                                # try to bind socket to device (most robust) — requires root
                                try:
                                    # Python requires bytes for device name
                                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, ifname.encode())
                                    logging.debug(f"Bound socket to device {ifname} for sending")
                                except Exception:
                                    # fallback: bind to iface address as source (best-effort)
                                    try:
                                        s.bind((addr, 0))
                                    except Exception:
                                        # final fallback: bind to wildcard
                                        try:
                                            s.bind(('', 0))
                                        except Exception:
                                            logging.exception(f"Failed to bind socket for iface {ifname} addr {addr}")
                                # now send
                                s.sendto(payload, tgt)
                                logging.info(f"Announce sent to {tgt[0]}:{tgt[1]} (iface {ifname} addr {addr}) -> {ws_url}")
                            except PermissionError:
                                logging.exception(f"PermissionError sending announce on iface {ifname} -> {tgt}; maybe need root for SO_BINDTODEVICE")
                            except Exception:
                                logging.exception(f"Failed to send announce to {tgt} via iface {ifname}")
                            finally:
                                try:
                                    s.close()
                                except Exception:
                                    pass

                except Exception:
                    logging.exception("Error processing interface " + str(ifname))

        except Exception:
            logging.exception("Announce loop error (continuing)")

        _stop_announce.wait(interval)

    logging.info("Announcer thread exiting")

PROBE_TIMEOUT = 1.0


async def send_probe_and_wait(ws, timeout=PROBE_TIMEOUT):
    """
    Отправляет один probe, ждёт reply и возвращает tuple (offset_ms, delay_ms)
    по формуле NTP, где offset в терминах (server_time - client_time).
    Возвращает None при таймауте/ошибке.
    """
    probe_id = uuid.uuid4().hex
    t0 = MS()
    req = {"type": "time_sync_probe", "id": probe_id, "t0": t0}
    # используем future для ожидания ответа (handler должен установить этот future)
    fut = asyncio.get_running_loop().create_future()
    # attach future to ws (we keep per-ws dict on ws object)
    if not hasattr(ws, "pending_time_probes"):
        ws.pending_time_probes = {}
    ws.pending_time_probes[probe_id] = fut

    # send probe
    await ws.send(json.dumps(req))

    try:
        reply = await asyncio.wait_for(fut, timeout=timeout)
        # reply is expected to be dict with t1,t2 and id
        t3 = MS()
        t1 = float(reply.get("t1"))
        t2 = float(reply.get("t2"))
        # compute delay and offset (server_time - client_time)
        delay = (t3 - t0) - (t2 - t1)
        offset = ((t1 - t0) + (t2 - t3)) / 2.0
        return offset, delay
    except asyncio.TimeoutError:
        logging.warning("Probe timeout id=%s", probe_id)
        return None
    except Exception as e:
        logging.exception("Probe error")
        return None
    finally:
        # cleanup
        ws.pending_time_probes.pop(probe_id, None)


async def compute_offset_round(ws, num_probes=40, best_fraction=0.2):
    """
    Выполнить num_probes, отобрать лучший best_fraction по минимальному delay,
    вернуть среднее offset.
    """
    results = []
    for i in range(num_probes):
        res = await send_probe_and_wait(ws)
        if res:
            results.append(res)
        await asyncio.sleep(0.02)  # небольшой интервал между пробами (20 ms)

    if not results:
        return None

    # сортируем по delay (интервал вторым элементом)
    results.sort(key=lambda x: x[1])
    take = max(1, int(len(results) * best_fraction))
    best = results[:take]
    offsets = [o for (o, d) in best]
    avg_offset = mean(offsets)
    avg_delay = mean([d for (o, d) in best])
    logging.info("Offset round: probes=%d got=%d best=%d avg_offset=%.3f ms avg_delay=%.3f ms",
                 num_probes, len(results), take, avg_offset, avg_delay)
    return avg_offset


async def handler(ws, path=None):
    """WebSocket handler — сохраняет бинарные сообщения и отвечает на auth."""
    global camera_callback
    ws.phone_to_pi_offset_ms = None

    # запустим background task один раз на подключение, чтобы обновлять offset периодически
    async def offset_updater():
        while True:
            try:
                new_offset = await compute_offset_round(ws, num_probes=30, best_fraction=0.2)
                if new_offset is not None:
                    ws.phone_to_pi_offset_ms = new_offset
                    logging.info("Updated offset for client %s: %.3f ms", ws.remote_address, new_offset)
                else:
                    logging.warning("Offset round failed")
            except Exception:
                logging.exception("Offset updater error (will retry)")
            await asyncio.sleep(5.0)  # повторяем каждые 5 s (настройка)

    offset_task = asyncio.create_task(offset_updater())
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

                        if data.get("type") == "time_sync_reply" and "id" in data:
                            pid = data["id"]
                            fut = None
                            if hasattr(ws, "pending_time_probes"):
                                fut = ws.pending_time_probes.get(pid)
                            if fut and not fut.done():
                                fut.set_result(data)
                                continue

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
        offset_task.cancel()
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Interrupted by user — exiting")
