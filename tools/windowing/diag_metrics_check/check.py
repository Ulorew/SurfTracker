#!/usr/bin/env python3
"""Стенд для разборщика DIAG: достаёт ли он ЗАЛОЖЕННЫЙ ответ.

ЗАЧЕМ. Разборщик считает шум и мост между шкалами. Оба числа правдоподобны на
вид при любой ошибке в формуле — а значит глазами их не проверить. Здесь
данные синтезируются с известным ответом, и проверка требует, чтобы он был
восстановлен.

ОТДЕЛЬНО ПРОВЕРЯЕТСЯ РАЗЛИЧАЮЩАЯ СИЛА: подложный лог с ИСПОРЧЕННЫМ трактом
обязан дать другие числа. Стенд, который одинаково доволен исправным и
сломанным, ничего не проверяет — этим уже отличился стенд переноса трекера.
Причём мало, чтобы числа разъехались: на сломанном тракте разборщик обязан
СКАЗАТЬ, что мерить нечего, а не выдать красивый ноль.
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

DT = 0.02                       # шаг опроса, с
SPIN_RAD = 0.30                 # рад/с
SPIN_DEG_S = math.degrees(SPIN_RAD)            # 17.19 град/с

# ДЖИТТЕР ДОСТАВКИ ЗАЛОЖЕН ВСЕГДА, а не только в порче. t_host на живом
# стенде — время приёма пакета по Bluetooth, и оно дрожит (замерено: СКО
# 2.73 мс). Стенд без этого дрожания одинаково доволен разбором по часам
# ноутбука и по часам платы, то есть не проверяет ничего.
HOST_JITTER_SD = 0.00273        # с
JITTER_SHIFT = 0.030            # с, порча: сдвиги приёма до 30 мс


def synth(path, n_spin=1500, n_hold=600, break_capture=False,
          break_isr=False, jitter=0.0, host_jitter=HOST_JITTER_SD):
    rnd = random.Random(20260821)
    rows = []
    isr_stuck = int(round(77.0 / 360.0 * CPR + SENS_MIN))

    def emit(label, mode, ang_deg, t):
        # --- боевой тракт: шум фронта, ЗАТЕМ квантование в микросекунды ---
        a = (ang_deg + rnd.gauss(0, TRUE_NOISE_ISR)) % 360.0
        isr_high = int(round(a / 360.0 * CPR + SENS_MIN))
        if break_isr:                           # ПОРЧА: боевой тракт замер
            isr_high = isr_stuck
        # --- тракт захвата: шум тот же физический + свой мелкий ---
        a2 = (ang_deg + rnd.gauss(0, TRUE_NOISE_CAP)) % 360.0
        frac = FRAC_MIN + a2 / 360.0 * FRAC_SPAN
        if break_capture:                       # ПОРЧА: тракт застрял
            frac = 0.5
        period = int(round(PERIOD_TICKS))
        # cap_age — НАСТОЯЩИЙ, равномерный по кадру. Константа (так было
        # раньше) делала поле безвредным: регрессия, которая заменит возраст
        # захвата фиксированным числом, прошла бы стенд не замеченной, хотя
        # на живых данных это дрожание в целый кадр датчика.
        cap_age = rnd.randrange(period)
        # Часы ПЛАТЫ честные по построению: момент отсчёта равен t, а t_us —
        # это уже момент сборки пакета, то есть t плюс возраст захвата.
        t_us = int(round((t + cap_age / TIM2_HZ) * 1e6))
        t_host = t + (rnd.gauss(0, host_jitter) if host_jitter else 0.0)
        if jitter:
            t_host += rnd.uniform(-jitter, jitter)
        rows.append(dict(label=label, mode=mode, volt=2.0, spin=0.0,
                         t_host=round(t_host, 4), seq=len(rows) & 0x7F,
                         t_us=t_us, isr_high=isr_high,
                         cap_age=cap_age, cap_high=int(round(frac * period)),
                         cap_period=period, flags=0))

    for i in range(n_hold):                     # стоим
        emit("hold_2v", "hold", 77.0, i * DT)
    for i in range(n_spin):                     # едем: 0.30 рад/с
        emit("spin_slow", "spin",
             (77.0 + SPIN_DEG_S * i * DT) % 360.0, 20 + i * DT)

    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def run(path, *args):
    r = subprocess.run([os.path.join(ROOT, ".venv/bin/python"),
                        os.path.join(ROOT, "tools/link/diag_metrics.py"), path,
                        *args], capture_output=True, text=True)
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


def section(text, name):
    """Кусок вывода про один отрезок: числа с одинаковыми подписями есть и в
    hold, и в spin, и без нарезки сравнивалось бы не то с тем."""
    lines, out = text.splitlines(), []
    for ln in lines:
        if ln.strip().startswith("---") and ": " in ln:
            if out:
                break
            if f"--- {name}:" in ln:
                out.append(ln)
        elif out:
            out.append(ln)
    return "\n".join(out)


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
if "МОСТ НЕ ПРЯМАЯ" in out:
    fails.append("прямой мост объявлен непрямым")
if "ДОПУЩЕНИЕ" in out:
    fails.append("мост измерим, но разборщик остался на наивных 360")

# Мост между часами обязан быть НАПЕЧАТАН И ИЗМЕРЕН: ход близко к единице,
# расхождение — это заложенный джиттер доставки, а не ноль.
rate = num_after(out, "ход часов ноутбука")
jit = num_after(out, "расхождение (джиттер доставки)")
if rate is None or abs(rate - 1.0) > 0.01:
    fails.append(f"ход часов не измерен или уехал: {rate}")
elif jit is None or jit < 1.0:
    fails.append(f"джиттер доставки не измерен: {jit} мс, заложено ~2.7 мс")
else:
    print(f"  мост между часами: ход {rate:.6f}, расхождение {jit:.2f} мс"
          f" (заложено {HOST_JITTER_SD * 1e3:.2f} мс)")

# Шум захвата на вращении: ход снят по часам платы, значит остаток обязан
# сойтись с заложенным, а не с заложенным плюс джиттер.
cap_spin = num_after(section(out, "spin_slow"), "аппаратный захват")
if cap_spin is None or abs(cap_spin - TRUE_NOISE_CAP) > 0.015:
    fails.append(f"шум захвата на вращении не восстановлен: {cap_spin}"
                 f" вместо {TRUE_NOISE_CAP}")
else:
    print(f"  шум захвата на вращении: {cap_spin:.4f}° против заложенных"
          f" {TRUE_NOISE_CAP}")

# Колонка квантования обязана показывать КВАНТ боевого тракта, а не остаток
# после снятия хода: на детрендированных значениях сетка уровней рассыпается.
gap_spin = num_after(section(out, "spin_slow"), "micros(), сырой")
gap_line = [l for l in section(out, "spin_slow").splitlines()
            if "micros(), сырой" in l]
quant = float(gap_line[0].replace("°", " ").split()[-1]) if gap_line else None
if quant is None or abs(quant - 360.0 / CPR) > 0.01:
    fails.append(f"интервал уровней micros() на вращении {quant},"
                 f" а квант тракта {360.0 / CPR:.5f}")
else:
    print(f"  квант micros() виден и на вращении: {quant:.5f}°")

print("\n== 2. РАЗЛИЧАЮЩАЯ СИЛА: застрявший тракт захвата ==")
bad = "/tmp/diag_bad.csv"
synth(bad, break_capture=True)
out2 = run(bad)
k2 = num_after(out2, "наклон:")
if k2 is not None and abs(k2 - TRUE_GAIN) < 2.0:
    fails.append("застрявший тракт дал ТОТ ЖЕ мост — стенд слеп")
else:
    print(f"  застрявший тракт мост не даёт (наклон {k2}) — порча видна")

print("\n== 2b. РАЗЛИЧАЮЩАЯ СИЛА: замерший БОЕВОЙ тракт ==")
# Здесь важно не то, что числа разъехались, а то, ЧТО НАПЕЧАТАНО. На мёртвом
# micros-тракте подгонка формально удаётся: наклон 0, остаток 0.0000° — и
# читается как «тракты сошлись идеально». Разборщик обязан назвать причину,
# отказаться от моста и пометить дальнейшие градусы допущением.
badi = "/tmp/diag_bad_isr.csv"
synth(badi, break_isr=True)
out3 = run(badi)
print(section(out3, "spin_slow") or out3[-800:])
k3 = num_after(out3, "наклон:")
if k3 is not None:
    fails.append(f"по замершему боевому тракту напечатан наклон моста {k3}")
if "СКО остатка" in out3:
    fails.append("по замершему боевому тракту напечатано рассогласование"
                 " трактов — это читается как идеальное согласование")
if "МОСТ НЕ ИЗМЕРЕН" not in out3:
    fails.append("замерший боевой тракт не назван: отказа от моста нет")
elif "БОЕВОЙ ТРАКТ НЕ ХОДИТ" not in out3:
    fails.append("отказ есть, но причина (замерший боевой тракт) не названа")
elif "ДОПУЩЕНИЕ" not in out3:
    fails.append("мост не измерен, но наклон 360 не помечен допущением")
else:
    print("  замерший боевой тракт назван, мост отвергнут,"
          " градусы помечены допущением")
if "ВЫИГРЫШ НЕ СЧИТАЕТСЯ" not in out3:
    fails.append("по вырожденной колонке micros() посчитан выигрыш")

print("\n== 2c. РАЗЛИЧАЮЩАЯ СИЛА: джиттер приёма при честных часах платы ==")
# t_host зашумлён сдвигами до 30 мс, t_us и cap_age честные. На 17.19 град/с
# такой сдвиг — это 0.5 град мнимого шума, на порядок больше настоящего.
# Требование: по часам ПЛАТЫ ответ обязан быть БЛИЖЕ к заложенному.
jit_csv = "/tmp/diag_jitter.csv"
synth(jit_csv, jitter=JITTER_SHIFT)
out_b = run(jit_csv)
out_h = run(jit_csv, "--clock", "host")
cap_b = num_after(section(out_b, "spin_slow"), "аппаратный захват")
cap_h = num_after(section(out_h, "spin_slow"), "аппаратный захват")
jit_ms = num_after(out_b, "расхождение (джиттер доставки)")
if cap_b is None or cap_h is None:
    fails.append("шум захвата на вращении не напечатан")
elif abs(cap_b - TRUE_NOISE_CAP) >= abs(cap_h - TRUE_NOISE_CAP):
    fails.append(f"часы платы не помогли: по плате {cap_b}, по ноутбуку"
                 f" {cap_h}, заложено {TRUE_NOISE_CAP}")
elif abs(cap_b - TRUE_NOISE_CAP) > 0.015:
    fails.append(f"по часам платы шум захвата {cap_b} вместо"
                 f" {TRUE_NOISE_CAP} — часы платы читаются неверно")
else:
    print(f"  по часам платы {cap_b:.4f}° (заложено {TRUE_NOISE_CAP}),"
          f" по часам ноутбука {cap_h:.4f}° — джиттер {jit_ms:.1f} мс"
          f" утёк бы в шум тракта")

print("\n== 2d. cap_age: возраст захвата обязан читаться ==")
# ДЖИТТЕР ПРИЁМА ЗДЕСЬ ВЫКЛЮЧЕН НАМЕРЕННО. Поправка на возраст захвата — это
# доли кадра датчика, до 921 мкс; за живым джиттером доставки в 2.7 мс её не
# разглядеть, и на рабочей скорости стенда 0.3 рад/с она теряется в шуме
# тракта (0.0393 против 0.0391 град — не отличить). Без джиттера часы платы
# обязаны СОВПАСТЬ с t_host точно, и невычтенный cap_age сразу вылезает
# расхождением в четверть миллисекунды.
clean = "/tmp/diag_clean_clock.csv"
synth(clean, host_jitter=0.0)
out4 = run(clean)
rate4 = num_after(out4, "ход часов ноутбука")
res4 = num_after(out4, "расхождение (джиттер доставки)")
if rate4 is None or abs(rate4 - 1.0) > 1e-4:
    fails.append(f"без джиттера ход часов вышел {rate4}, а обязан быть 1")
elif res4 is None or res4 > 0.10:
    fails.append(f"часы платы разошлись с t_host на {res4} мс без всякого"
                 f" джиттера — похоже, cap_age не вычитается (он даёт 0.27 мс)")
else:
    print(f"  без джиттера часы платы совпадают с t_host: ход {rate4:.6f},"
          f" расхождение {res4:.2f} мс")

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
    """Кадр DIAG ПО ТЕКУЩЕЙ раскладке протокола.

    Хвост (ack_mode/ack_volt/ack_spin) добавили в пакет позже, а здесь кадр
    собирался вручную по старой длине — и стенд молча терял ВСЕ пакеты: CRC
    ложился не на своё место, разбор его отвергал, и это выглядело как
    поломка кадрирования. Длина сверяется с DIAG_LEN явно, чтобы следующий
    рост пакета был назван, а не проявился ложным отказом.
    """
    body = (bytes([MAGIC_DIAG, seq]) + struct.pack("<IIIII", 1, 2, 3, 4, 5)
            + bytes([0, 0, 200]) + struct.pack("<b", 0))
    if len(body) != DIAG_LEN - 1:
        sys.exit(f"стенд собирает кадр DIAG в {len(body) + 1} байт, "
                 f"а протокол ждёт {DIAG_LEN}: раскладка пакета изменилась")
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
print("ИТОГ: разборщик достаёт заложенный ответ, обе порчи называет вслух,")
print("      ход снимает по часам платы, кадрирование держит мусор,")
print("      предохранители прижимают.")
