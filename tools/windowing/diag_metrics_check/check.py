#!/usr/bin/env python3
"""Стенд для разборщика DIAG: достаёт ли он ЗАЛОЖЕННЫЙ ответ.

ЗАЧЕМ. Разборщик считает шум и мост между шкалами. Оба числа правдоподобны на
вид при любой ошибке в формуле — а значит глазами их не проверить. Здесь
данные синтезируются с известным ответом, и проверка требует, чтобы он был
восстановлен.

ОТДЕЛЬНО ПРОВЕРЯЕТСЯ РАЗЛИЧАЮЩАЯ СИЛА: подложный лог с ИСПОРЧЕННЫМ трактом
обязан дать другие числа. Стенд, который одинаково доволен исправным и
сломанным, ничего не проверяет — этим уже отличился стенд переноса трекера.
"""
import csv, os, random, struct, subprocess, sys, math

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools", "link"))
from proto_v2 import (build_cmd, crc8, MAGIC_DIAG, DIAG_LEN, parse_diag,
                      MAGIC_TEL, build_tel)

SENS_MIN, CPR = 3.0, 917.0
TIM2_HZ = 170e6
PERIOD_TICKS = 921e-6 * TIM2_HZ          # тиков в кадре датчика

# ЗАЛОЖЕННЫЙ ОТВЕТ. Задаётся ЧЕРЕЗ ДИАПАЗОН СКВАЖНОСТИ, а не наклоном
# напрямую: наклон меньше 360 означал бы, что скважность пробегает больше
# единицы, а так датчик не умеет. Первая редакция стенда закладывала ровно
# такую невозможную величину и потом требовала её восстановить.
#
# Диапазон взят НЕ полным (0.031..1.000) намеренно: у кадра есть служебная
# часть, и если разборщик втихую подставит наивные 360 вместо подгонки,
# проверка это увидит.
FRAC_MIN, FRAC_SPAN = 0.031, 0.969
TRUE_GAIN = 360.0 / FRAC_SPAN                  # 371.52 град на скважность
TRUE_OFFSET = -FRAC_MIN * TRUE_GAIN            # -11.52 град
TRUE_NOISE_CAP = 0.040          # град, шум тракта захвата
TRUE_NOISE_ISR = 0.150          # град, шум ФРОНТА (до квантования micros)


def synth(path, n_spin=1500, n_hold=600, break_capture=False):
    rnd = random.Random(20260821)
    rows = []

    def emit(label, mode, ang_deg, t):
        # --- боевой тракт: шум фронта, ЗАТЕМ квантование в микросекунды ---
        a = (ang_deg + rnd.gauss(0, TRUE_NOISE_ISR)) % 360.0
        isr_high = int(round(a / 360.0 * CPR + SENS_MIN))
        # --- тракт захвата: шум тот же физический + свой мелкий ---
        a2 = (ang_deg + rnd.gauss(0, TRUE_NOISE_CAP)) % 360.0
        frac = FRAC_MIN + a2 / 360.0 * FRAC_SPAN
        if break_capture:                       # ПОРЧА: тракт застрял
            frac = 0.5
        period = int(round(PERIOD_TICKS))
        rows.append(dict(label=label, mode=mode, volt=2.0, spin=0.0,
                         t_host=round(t, 4), seq=len(rows) & 0x7F,
                         t_us=int(t * 1e6), isr_high=isr_high,
                         cap_age=1234, cap_high=int(round(frac * period)),
                         cap_period=period, flags=0))

    for i in range(n_hold):                     # стоим
        emit("hold_2v", "hold", 77.0, i * 0.02)
    for i in range(n_spin):                     # едем: 0.30 рад/с
        emit("spin_slow", "spin",
             (77.0 + math.degrees(0.30 * i * 0.02)) % 360.0, 20 + i * 0.02)

    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def run(path):
    r = subprocess.run([os.path.join(ROOT, ".venv/bin/python"),
                        os.path.join(ROOT, "tools/link/diag_metrics.py"), path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        sys.exit("разборщик упал")
    return r.stdout


def num_after(text, marker):
    for line in text.splitlines():
        if marker in line:
            for tok in line.replace("°", " ").replace("%", " ").split():
                try:
                    return float(tok.replace("+", ""))
                except ValueError:
                    continue
    return None


fails = []
print("== 1. исправные данные: мост обязан восстановиться ==")
good = "/tmp/diag_good.csv"
synth(good)
out = run(good)
print(out)

k = num_after(out, "наклон:")
b = num_after(out, "смещение:")
res = num_after(out, "СКО остатка")
if k is None or abs(k - TRUE_GAIN) > 2.0:
    fails.append(f"наклон не восстановлен: {k} вместо {TRUE_GAIN}")
else:
    print(f"  наклон восстановлен: {k:.2f} против заложенных {TRUE_GAIN}")
if b is None or abs(b - TRUE_OFFSET) > 3.0:
    fails.append(f"смещение не восстановлено: {b} вместо {TRUE_OFFSET}")
else:
    print(f"  смещение восстановлено: {b:.2f} против заложенных {TRUE_OFFSET}")

print("\n== 2. РАЗЛИЧАЮЩАЯ СИЛА: застрявший тракт захвата ==")
bad = "/tmp/diag_bad.csv"
synth(bad, break_capture=True)
out2 = run(bad)
k2 = num_after(out2, "наклон:")
if k2 is not None and abs(k2 - TRUE_GAIN) < 2.0:
    fails.append("застрявший тракт дал ТОТ ЖЕ мост — стенд слеп")
else:
    print(f"  застрявший тракт мост не даёт (наклон {k2}) — порча видна")

print("\n== 3. кадрирование приёма: мусор и обрывки не ломают разбор ==")
sys.path.insert(0, os.path.join(ROOT, "tools", "link"))
import stand as S


class FakeSock:
    def __init__(self, data):
        self.data, self.i = data, 0
    def recv(self, n):
        if self.i >= len(self.data):
            raise __import__("socket").timeout()
        chunk = self.data[self.i:self.i + n]
        self.i += len(chunk)
        return chunk
    def sendall(self, b): pass
    def settimeout(self, t): pass
    def close(self): pass


def diag_bytes(seq):
    body = bytes([MAGIC_DIAG, seq]) + struct.pack("<IIIII", 1, 2, 3, 4, 5) + bytes([0])
    return body + bytes([crc8(body)])


stream = (b"\x11\x22" + diag_bytes(1) + b"\x5C\x00" +          # ложное начало
          diag_bytes(2) + build_tel(3, 0.0, 0.0, 8) + diag_bytes(4))
lk = S.Link.__new__(S.Link)
lk.s, lk.buf, lk.crc_bad, lk.junk = FakeSock(stream), bytearray(), 0, 0
got = lk.drain()
seqs = [p["seq"] for p in got if "seq" in p]
if seqs != [1, 2, 4]:
    fails.append(f"кадрирование потеряло пакеты: seq={seqs}, ждали [1, 2, 4]")
else:
    print(f"  все три DIAG найдены среди мусора: seq={seqs}, "
          f"брак CRC {lk.crc_bad}, мусор {lk.junk}")

print("\n== 4. предохранители плана ==")
import io, contextlib
class NoSend(FakeSock):
    def __init__(self): super().__init__(b""); self.sent = []
    def sendall(self, b): self.sent.append(b)
lk2 = S.Link.__new__(S.Link)
lk2.s, lk2.buf, lk2.crc_bad, lk2.junk, lk2.seq = NoSend(), bytearray(), 0, 0, 0
buf = io.StringIO()
w = csv.DictWriter(buf, fieldnames=S.FIELDS, extrasaction="ignore")
with contextlib.redirect_stdout(io.StringIO()):
    S.segment(lk2, w, "x", S.MODE_SPIN, volt=9.9, spin=5.0, seconds=0.2, hz=10)
from proto_v2 import CMD_VOLT, CMD_SPIN
sent_volt = [struct.unpack("<f", p[3:7])[0] for p in lk2.s.sent if p[2] == CMD_VOLT]
sent_spin = [struct.unpack("<f", p[3:7])[0] for p in lk2.s.sent if p[2] == CMD_SPIN]
if not sent_volt or max(sent_volt) > S.VOLT_MAX + 1e-6:
    fails.append(f"потолок напряжения не сработал: {sent_volt}")
else:
    print(f"  9.9 В прижато до {max(sent_volt)} В")
if not sent_spin or max(abs(x) for x in sent_spin) > S.SPIN_MAX + 1e-6:
    fails.append(f"потолок скорости не сработал: {sent_spin}")
else:
    print(f"  5.0 рад/с прижато до {max(abs(x) for x in sent_spin)} рад/с")

print()
if fails:
    for f in fails:
        print("ОТКАЗ:", f)
    sys.exit(2)
print("ИТОГ: разборщик достаёт заложенный ответ, порчу видит,")
print("      кадрирование держит мусор, предохранители прижимают.")
