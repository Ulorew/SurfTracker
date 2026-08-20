#!/usr/bin/env python3
"""Разбор лога pwm_bench и расчёт метрик сравнения трактов.

    python3 analyze_pwm.py log.bin --static
    python3 analyze_pwm.py sweep.bin --sweep
"""
import argparse
import struct
import sys

import numpy as np

REC = "<HHIIIIIfHH"
SIZE = struct.calcsize(REC)
SYNC = 0xA55A

TIM_HZ = 170_000_000
FRAME_CLK = 4119.0
OFFSET_CLK = 16.0
SPAN = 4096.0
DEG_PER_STEP = 360.0 / SPAN          # 0.0879 град


def load(path):
    data = open(path, "rb").read()
    recs, i, dropped = [], 0, 0
    while i + SIZE <= len(data):
        if data[i] != (SYNC & 0xFF) or data[i + 1] != (SYNC >> 8):
            i += 1                    # ресинхронизация по маркеру
            dropped += 1
            continue
        r = struct.unpack(REC, data[i:i + SIZE])
        if (sum(data[i:i + SIZE - 2]) & 0xFFFF) == r[9]:
            recs.append(r)
            i += SIZE
        else:
            i += 1
            dropped += 1
    if dropped:
        print(f"[!] пропущено байт при ресинхронизации: {dropped}", file=sys.stderr)
    if not recs:
        sys.exit("нет валидных записей")
    a = np.array(recs, dtype=object)
    return {
        "seq": a[:, 1].astype(np.int64),
        "t": a[:, 2].astype(np.float64) * 1e-6,
        "cap_p": a[:, 3].astype(np.float64),
        "cap_h": a[:, 4].astype(np.float64),
        "isr_p": a[:, 5].astype(np.float64),
        "isr_h": a[:, 6].astype(np.float64),
        "ref": a[:, 7].astype(np.float64),
        "flags": a[:, 8].astype(np.int64),
    }


def angles(d, naive_min=3.9, naive_max=998.0):
    """Три варианта угла в градусах из одних и тех же кадров."""
    out = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = d["cap_h"] / d["cap_p"] * FRAME_CLK - OFFSET_CLK
    out["capture"] = raw * DEG_PER_STEP

    with np.errstate(divide="ignore", invalid="ignore"):
        raw = d["isr_h"] / d["isr_p"] * FRAME_CLK - OFFSET_CLK
    out["isr"] = raw * DEG_PER_STEP

    raw = (d["isr_h"] - naive_min) * SPAN / (naive_max - naive_min)
    out["naive"] = raw * DEG_PER_STEP
    return out


def unwrap(deg):
    return np.degrees(np.unwrap(np.radians(deg * 4.0))) / 4.0  # период 360


def static_report(d):
    """Тест 1: ротор неподвижен, значит весь разброс — шум тракта."""
    a = angles(d)
    med_p = np.median(d["cap_p"])
    print(f"кадров: {len(d['seq'])}   период: "
          f"{med_p / TIM_HZ * 1e6:.1f} мкс  "
          f"({TIM_HZ / med_p:.1f} Гц)")
    print(f"шаг энкодера: {DEG_PER_STEP * 1000:.1f} мград\n")

    hdr = f"{'тракт':<10}{'СКО':>10}{'p99.9':>10}{'макс':>10}{'битых':>10}"
    print(hdr)
    print("-" * len(hdr))
    for name in ("naive", "isr", "capture"):
        x = a[name]
        ok = np.isfinite(x)
        x = x[ok]
        if len(x) < 10:
            continue
        x = x - np.median(x)
        x = (x + 180) % 360 - 180          # на случай перехода через ноль
        bad = np.mean(~ok) * 100
        print(f"{name:<10}{np.std(x):>9.4f}°"
              f"{np.percentile(np.abs(x), 99.9):>9.4f}°"
              f"{np.max(np.abs(x)):>9.4f}°"
              f"{bad:>9.3f}%")

    # доля кадров, где период уехал больше чем на 1% от медианы
    for name, p in (("isr", d["isr_p"] * (TIM_HZ / 1e6)), ("capture", d["cap_p"])):
        dev = np.abs(p - np.median(p)) / np.median(p)
        print(f"\n{name}: кадров с уходом периода >1%: "
              f"{np.mean(dev > 0.01) * 100:.3f}%")

    print("\nЖду примерно десятикратного падения СКО naive → capture.")
    print("Если получилось меньше чем вдвое — ищи ошибку в методике.")


def sweep_report(d):
    """Тест 2: электрический угол задан кварцем и служит эталоном."""
    a = angles(d)
    ref_mech = np.degrees(d["ref"]) / 11.0        # 11 пар полюсов
    print(f"развёртка: {ref_mech[-1] - ref_mech[0]:.1f}° по механике, "
          f"{len(ref_mech)} точек\n")

    hdr = f"{'тракт':<10}{'ошибка масштаба':>18}{'остаток СКО':>14}{'пик':>10}"
    print(hdr)
    print("-" * len(hdr))
    for name in ("naive", "isr", "capture"):
        y = a[name]
        ok = np.isfinite(y)
        if ok.sum() < 100:
            continue
        y = unwrap(y[ok])
        x = ref_mech[ok]
        k, b = np.polyfit(x, y, 1)
        res = y - (k * x + b)
        print(f"{name:<10}{(k - 1) * 100:>17.3f}%"
              f"{np.std(res):>13.4f}°{np.max(np.abs(res)):>9.4f}°")

    print("\nНаклон — это ошибка масштаба от разброса частоты кадра.")
    print("У capture и isr он должен быть около нуля: период измеряется.")
    print("Остаток одинаков у всех трактов — это нелинейность магнита.")
    print("\nПодгонка констант кадра по capture:")
    y = unwrap(a["capture"][np.isfinite(a["capture"])])
    x = ref_mech[np.isfinite(a["capture"])]
    k, _ = np.polyfit(x, y, 1)
    print(f"  frame_clk = {FRAME_CLK / k:.1f}  (номинал {FRAME_CLK:.0f})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--static", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    d = load(args.log)
    if args.sweep:
        sweep_report(d)
    else:
        static_report(d)


if __name__ == "__main__":
    main()
