#!/usr/bin/env python3
"""Развёртка по фильтру скорости и по окну оценки скорости, по ОДНОЙ оси за раз.

ЗАЧЕМ ПО ОДНОЙ. Двумерная настройка уже подводила: P и I масштабировались
одновременно, и это не было сказано; после разделения оказалось, что P в
одиночку дестабилизирует, I монотонно улучшает, а составной оптимум вдвое
хуже. Здесь две оси — Tf и MET — и они меряются раздельно.

ЧТО ЗА ОСИ. Tf борется со СЛЕДСТВИЕМ (шум оценки скорости уже возник),
MET — с ПРИЧИНОЙ (сколько времени копится приращение угла до вычисления
скорости). Правило большого пальца из документации SimpleFOC знает только
про Tf и выводит его из частоты ВРАЩЕНИЯ ВАЛА, а главная помеха здесь сидит
на 22 циклах на оборот. Поэтому предсказания расходятся, см. ПРЕДСКАЗАНИЯ.

МЕТРИКА В ПОРЯДКАХ, а не в герцах: зубцовая помеха привязана к обороту, и
полоса 1-20 Гц теряет её на малых скоростях (0.2 рад/с -> 22-й порядок на
0.700 Гц, вне полосы).
"""
import argparse, math, os, sys, time
import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jerk_metric import jitter

PORT = "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0"
ORDER_LO, ORDER_HI = 5.0, 300.0

# Объявлено ДО прогона. Рабочие точки: 0.25 об/с дома,
# 0.1 об/с в проде -> 1.571 и 0.628 рад/с.
ПРЕДСКАЗАНИЯ = """
  правило SimpleFOC (срез = 5x частота вала):  Tf = 0.127 дома, 0.318 в проде
  довод по порядкам (срез выше 22-го порядка): Tf = 0.010
  измеренное на прошлой матрице:               Tf = 0.05
"""


def открыть():
    s = serial.Serial(); s.port = PORT; s.baudrate = 115200; s.timeout = 1.0
    s.dtr = False; s.rts = False
    s.open(); time.sleep(0.1)
    s.rts = True; time.sleep(0.1); s.rts = False   # аппаратный сброс
    time.sleep(3.0)
    s.reset_input_buffer()
    return s


def команда(s, txt, ждать=0.4):
    s.write((txt + "\n").encode()); time.sleep(ждать)
    return s.read(20000).decode("utf-8", "replace")


def прогон(s, v, t_ms, s_ms):
    """-> (список углов в рад, fs). Читает до #КОНЕЦ."""
    s.reset_input_buffer()
    s.write(f"RUN v={v:.4f} t={t_ms} s={s_ms}\n".encode())
    buf, t0 = "", time.time()
    предел = (t_ms + s_ms) / 1000.0 + 15
    while time.time() - t0 < предел:
        buf += s.read(4096).decode("utf-8", "replace")
        if "#КОНЕЦ" in buf:
            break
    ys, fs = [], 200.0
    for line in buf.splitlines():
        line = line.strip()
        if line.startswith("#НАЧАЛО"):
            for tok in line.split():
                if tok.startswith("fs="):
                    fs = float(tok[3:])
        elif line and not line.startswith("#"):
            try:
                ys.append(float(line))
            except ValueError:
                pass
    return ys, fs


def мера(ys, fs, v):
    """Дрожание в полосе ПОРЯДКОВ и в прежней шкале герц — рядом."""
    import numpy as np
    y = np.array(ys, dtype=float)
    if len(y) < 400:
        return None
    rev = abs(v) / (2 * math.pi)
    d_or, пол, пик, v_ист = jitter(y, fs, lo=ORDER_LO * rev, hi=ORDER_HI * rev)
    d_hz, _, _, _ = jitter(y, fs)
    return dict(orr=d_or, hz=d_hz, пол=пол, пик=пик, v=v_ист, n=len(y))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ось", choices=["Tf", "MET"])
    ap.add_argument("--скорости", default="0.2,0.628,1.571")
    ap.add_argument("--замер", type=int, default=8000)
    ap.add_argument("--разгон", type=int, default=2000)
    ap.add_argument("--пауза", type=float, default=3.0,
                    help="секунд с СНЯТЫМ полем между ячейками")
    a = ap.parse_args()

    сетка = {"Tf": [0.02, 0.05, 0.10, 0.13],
             "MET": [0.002, 0.005, 0.010, 0.020]}[a.ось]
    скорости = [float(x) for x in a.скорости.split(",")]

    print(f"РАЗВЁРТКА ПО {a.ось}: {сетка}")
    print(f"скорости, рад/с: {скорости}")
    print("ПРЕДСКАЗАНИЯ, записаны до прогона:" + ПРЕДСКАЗАНИЯ)
    print("вал не трогать\n")

    s = открыть()
    print(команда(s, "STATE", 1.0).strip()[-200:], "\n")
    итог = []
    try:
        for знач in сетка:
            команда(s, f"SET {a.ось}={знач}")
            for v in скорости:
                ys, fs = прогон(s, v, a.замер, a.разгон)
                команда(s, "OFF", 0.3)
                m = мера(ys, fs, v)
                if m is None:
                    print(f"  {a.ось}={знач:<6} v={v:<6.3f}  ОТСЧЁТОВ МАЛО ({len(ys)})")
                    continue
                отст = m["v"] / v if v else float("nan")
                print(f"  {a.ось}={знач:<6} v={v:<6.3f} отстав {отст:6.3f}  "
                      f"дрож(порядки) {m['orr']:7.4f}°  (1-20Гц {m['hz']:7.4f}°)  "
                      f"пол {m['пол']:.4f}")
                итог.append((знач, v, отст, m["orr"], m["hz"]))
                time.sleep(a.пауза)     # поле снято: нагрев не копится
    finally:
        команда(s, "OFF", 0.5)
        s.close()
        print("\nполе снято")

    if итог:
        print(f"\nСВОД по {a.ось} (среднее дрожание в порядках по скоростям):")
        # ГЕЙТ НА ОТСТАВАНИЕ — ДО усреднения. Неподвижный вал «плавен» по
        # построению: в прошлой матрице Tf=0.01 и 0.02 дали ЛУЧШЕЕ дрожание в
        # таблице, потому что мотор не тронулся, а метрика честно померила
        # ровность неподвижности. Ячейка, не удержавшая скорость, в свод не
        # входит вовсе — иначе провал выглядит победой.
        ОТСТ_МИН, ОТСТ_МАКС = 0.95, 1.05
        from collections import defaultdict
        g, отвал = defaultdict(list), defaultdict(list)
        for знач, v, отст, orr, hz in итог:
            (g if ОТСТ_МИН <= отст <= ОТСТ_МАКС else отвал)[знач].append((v, отст, orr))
        for знач in сетка:
            годн = g[знач]
            if годн:
                ср = sum(o for _, _, o in годн) / len(годн)
                print(f"  {a.ось}={знач:<6} {ср:.4f}°  ({len(годн)} годных ячеек)")
            else:
                print(f"  {a.ось}={знач:<6} НЕТ ГОДНЫХ ЯЧЕЕК — скорость не держится")
            for v, отст, orr in отвал[знач]:
                print(f"      отброшено: v={v:.3f} отставание {отст:.3f}"
                      f"{'  (вал не тронулся)' if отст < 0.1 else ''}")


if __name__ == "__main__":
    main()
