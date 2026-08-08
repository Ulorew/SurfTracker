#!/usr/bin/env python3
"""Эхо-тест протокола по любому байтовому транспорту.

Клиент транспортно-независим ровно потому, что таков протокол: он читает и
пишет байты, и ему всё равно, идут они по проводу или по Bluetooth. Отсюда
прямая сопоставимость чисел.

    провод:    --port /dev/ttyACM0
    bluetooth: сначала связать и привязать rfcomm, потом --port /dev/rfcomm0

        bluetoothctl --  scan on / pair <MAC> / trust <MAC>
        sudo rfcomm bind 0 <MAC> 1


Тикет «замыкание контура», ступень 2 требует эхо-тест с телефона. Но на
телефоне сейчас нестабильна сама шина USB, и потери там мерили бы качество
контакта, а не протокол. Этот клиент гоняет ТОТ ЖЕ протокол с ноутбука через
`/dev/ttyACM0`, где транспорт исправен, и даёт опорные числа: всё, что потом
окажется хуже на телефоне, — вина транспорта, а не прошивки.

Мотор при этом не участвует: прошивка залита сборкой MOTOR_ENABLED 0.

    python link_test.py --port /dev/ttyACM0 --seconds 60 --hz 25
"""
import argparse
import json
import struct
import sys
import time

MAGIC = 0xA5
REQ_LEN, RESP_LEN = 7, 8

ST_WATCHDOG, ST_ENC_OK, ST_VEL_CLIP, ST_CRC_DROP = 1, 2, 4, 8


def crc8(data, poly=0x07, init=0x00):
    c = init
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ poly) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def build(seq, w):
    body = bytes([MAGIC, seq & 0xFF]) + struct.pack("<f", w)
    return body + bytes([crc8(body)])


def parse(buf):
    """-> (seq, theta, status) | None. Разбор строго как в прошивке."""
    if len(buf) != RESP_LEN or buf[0] != MAGIC:
        return None
    if crc8(buf[:-1]) != buf[-1]:
        return None
    return buf[1], struct.unpack("<f", buf[2:6])[0], buf[6]


def self_check():
    """Контрольные векторы из спецификации. Если не сойдутся — дальше идти
    нельзя: значит клиент и прошивка считают CRC по-разному, и любые потери
    будут объясняться этим, а не связью."""
    cases = [((0, 0.0), "5A"), ((1, 1.5), "68"), ((255, -2.25), "7A"),
             ((42, 0.017453292), "3B")]
    for (seq, w), want in cases:
        got = f"{build(seq, w)[-1]:02X}"
        assert got == want, f"CRC запроса seq={seq}: {got} вместо {want}"
    for seq, th, st, want in ((0, 0.0, 0x02, "8F"), (1, 3.14159265, 0x02, "41"),
                               (255, -1.0, 0x03, "A0"), (42, 0.5, 0x06, "8F")):
        body = bytes([MAGIC, seq]) + struct.pack("<f", th) + bytes([st])
        got = f"{crc8(body):02X}"
        assert got == want, f"CRC ответа seq={seq}: {got} вместо {want}"
    print("контрольные векторы спецификации сошлись")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0",
                     help="/dev/ttyACM0 — провод к Nucleo, /dev/rfcomm0 — "
                          "Bluetooth к ESP32 (см. --help-bt)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--hz", type=float, default=25.0)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--omega", type=float, default=0.0,
                     help="какую скорость просить, рад/с. По умолчанию 0 — "
                          "эхо-тест не должен зависеть от того, что делает мотор")
    ap.add_argument("--watchdog-probe", action="store_true",
                     help="в конце замолчать на 1 с и проверить, что "
                          "следующий ответ несёт бит сторожевого")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    self_check()

    import serial
    ser = serial.Serial(args.port, args.baud, timeout=0.0)
    time.sleep(0.3)
    ser.reset_input_buffer()

    period = 1.0 / args.hz
    n = int(args.seconds * args.hz)
    rows = []
    rx = bytearray()
    t_start = time.perf_counter()
    next_t = t_start
    sent = ok = lost = bad = 0
    seq_mismatch = 0

    for i in range(n):
        # ждём момент такта, не проскакивая накопленное отставание
        now = time.perf_counter()
        if next_t > now:
            time.sleep(next_t - now)
        t_send = time.perf_counter()
        seq = i & 0xFF
        ser.write(build(seq, args.omega))
        sent += 1

        # ответ ждём не дольше периода: опоздавший ответ для контура бесполезен
        deadline = t_send + period
        got = None
        while time.perf_counter() < deadline:
            chunk = ser.read(64)
            if chunk:
                rx.extend(chunk)
                while len(rx) >= RESP_LEN:
                    if rx[0] != MAGIC:
                        rx.pop(0)
                        continue
                    frame = bytes(rx[:RESP_LEN])
                    p = parse(frame)
                    if p is None:
                        rx.pop(0)          # ресинхронизация по магику
                        continue
                    del rx[:RESP_LEN]
                    got = p
                    break
                if got:
                    break
            else:
                time.sleep(0.0005)
        t_got = time.perf_counter()

        if got is None:
            lost += 1
            rows.append({"i": i, "seq": seq, "ok": 0})
        else:
            rseq, theta, st = got
            if rseq != seq:
                seq_mismatch += 1
            ok += 1
            rows.append({"i": i, "seq": seq, "rseq": rseq, "ok": 1,
                          "rtt_ms": (t_got - t_send) * 1e3,
                          "theta": theta, "st": st,
                          "t_send": t_send - t_start})
        next_t += period

    res = {"port": args.port, "baud": args.baud, "hz": args.hz,
            "seconds": args.seconds, "omega": args.omega,
            "sent": sent, "ok": ok, "lost": lost, "seq_mismatch": seq_mismatch}

    periods = [rows[i]["t_send"] - rows[i - 1]["t_send"]
               for i in range(1, len(rows))
               if "t_send" in rows[i] and "t_send" in rows[i - 1]]
    rtts = sorted(r["rtt_ms"] for r in rows if r.get("ok"))
    if periods:
        ps = sorted(p * 1e3 for p in periods)
        res["period_ms"] = {"p50": ps[len(ps) // 2], "p95": ps[int(0.95 * len(ps))],
                             "max": ps[-1]}
    if rtts:
        res["rtt_ms"] = {"p50": rtts[len(rtts) // 2], "p95": rtts[int(0.95 * len(rtts))],
                          "max": rtts[-1]}
    st_any = [r["st"] for r in rows if r.get("ok")]
    if st_any:
        res["статус"] = {
            "сторожевой хоть раз": any(s & ST_WATCHDOG for s in st_any),
            "энкодер жив, доля": sum(1 for s in st_any if s & ST_ENC_OK) / len(st_any),
            "ограничение скорости": any(s & ST_VEL_CLIP for s in st_any),
            "битые кадры на той стороне": sum(1 for s in st_any if s & ST_CRC_DROP),
        }
    thetas = [r["theta"] for r in rows if r.get("ok")]
    if thetas:
        res["theta_rad"] = {"min": min(thetas), "max": max(thetas)}

    if args.watchdog_probe and ok:
        time.sleep(1.0)                      # молчим дольше 300 мс
        ser.reset_input_buffer()
        ser.write(build(0, 0.0))
        t0 = time.perf_counter()
        buf = bytearray()
        while time.perf_counter() - t0 < 0.5 and len(buf) < RESP_LEN:
            buf.extend(ser.read(RESP_LEN - len(buf)))
        p = parse(bytes(buf[:RESP_LEN])) if len(buf) >= RESP_LEN else None
        res["сторожевой"] = ("бит взведён" if p and (p[2] & ST_WATCHDOG)
                              else ("бит НЕ взведён" if p else "ответа нет"))

    ser.close()
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if args.out:
        json.dump({"summary": res, "rows": rows}, open(args.out, "w"),
                   ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
