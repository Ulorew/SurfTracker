#!/usr/bin/env python3
"""Плавность хода по скоростям: контрольная кривая для замкнутого контура.

ЧТО МЕРЯЕТ. Остаток угла после снятия равномерного хода, в полосе 1-20 Гц.
Это и есть «плавность»: медленный увод в неё не попадает (его снимает ход и
режет нижняя граница), а шум датчика режет верхняя.

ПОЧЕМУ ИМЕННО ТРАКТ ЗАХВАТА. Собственный шум боевого тракта 0.72-0.91 град
шире всего, что мы ищем; мерить им плавность значило бы мерить прибор.
Тракт захвата шумит 0.037-0.069 и годится. Оба тракта считаются рядом
НАМЕРЕННО — разница показывает, что старый тракт вообще не способен увидеть.

ЧАСЫ БЕРУТСЯ С ПЛАТЫ И С ПОПРАВКОЙ НА ВОЗРАСТ ЗАХВАТА: t_us - cap_age/170e6.
Часы ноутбука дают джиттер доставки 3-5 мс, а он на скорости превращается в
фальшивый шум (при 0.5 рад/с это 0.14 град — больше измеряемого).
"""
import csv, math, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diag_metrics import (cap_frac, isr_deg, unwrap, sd, split_runs,
                          BAD_MASK, TIM2_HZ)

BAND_LO, BAND_HI = 1.0, 20.0
NEBW_HANN = 1.5        # шумовая полоса окна Ханна
GAIN = 362.4          # измеренный мост, середина двух прогонов


def sample_ts(r):
    """Точный момент отсчёта по часам ПЛАТЫ, секунды."""
    return int(r["t_us"]) / 1e6 - int(r["cap_age"]) / TIM2_HZ


def resample(ts, xs, fs):
    """На равномерную сетку линейной интерполяцией.

    Спектр требует равномерных отсчётов, а приходят они с джиттером канала.
    Интерполяция слегка душит верх полосы, но одинаково для ОБОИХ трактов,
    поэтому их сравнение она не искажает.
    """
    t0, t1 = ts[0], ts[-1]
    n = int((t1 - t0) * fs)
    out, j = [], 0
    for i in range(n):
        t = t0 + i / fs
        while j + 2 < len(ts) and ts[j + 1] < t:
            j += 1
        a, b = ts[j], ts[j + 1]
        w = 0.0 if b <= a else (t - a) / (b - a)
        out.append(xs[j] * (1 - w) + xs[j + 1] * w)
    return out


def detrend_line(xs):
    n = len(xs)
    mt = (n - 1) / 2.0
    mx = sum(xs) / n
    stt = sum((i - mt) ** 2 for i in range(n))
    k = sum((i - mt) * (x - mx) for i, x in enumerate(xs)) / stt
    return [x - (mx + k * (i - mt)) for i, x in enumerate(xs)]


def band_rms(xs, fs, lo=BAND_LO, hi=BAND_HI):
    """СКО в полосе. Прямым перебором частот — данных мало, скорость не важна,
    зато не нужен внешний БПФ и видно, что именно считается."""
    n = len(xs)
    if n < 64:
        return float("nan"), []
    w = [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]
    # НОРМИРОВКА — КВАДРАТ СУММЫ ВЕСОВ, а не сумма квадратов.
    #
    # Для синуса амплитуды A взвешенная сумма даёт |X_k| = A*sum(w)/2, значит
    # мощность восстанавливается как 2*|X_k|^2 / sum(w)^2 = A^2/2. Деление на
    # sum(w*w) завышает результат в 2n/3 раз (для окна Ханна), то есть на
    # трёх тысячах проб — в сорок раз. Поймано подложкой: заложенный синус
    # 0.1768 град возвращался как 6.85.
    wsum = sum(w) ** 2
    xw = [x * wv for x, wv in zip(xs, w)]
    df = fs / n
    k_lo, k_hi = max(1, int(lo / df)), min(n // 2 - 1, int(hi / df))
    power, lines = 0.0, []
    for k in range(k_lo, k_hi + 1):
        c = s = 0.0
        for i, v in enumerate(xw):
            a = 2 * math.pi * k * i / n
            c += v * math.cos(a); s += v * math.sin(a)
        p = 2.0 * (c * c + s * s) / wsum      # средний квадрат в этом бине
        power += p
        lines.append((p, k * df))
    lines.sort(reverse=True)
    # ДЕЛЕНИЕ НА ШУМОВУЮ ПОЛОСУ ОКНА. Окно Ханна размазывает энергию по
    # соседним бинам, и суммирование бинов её пересчитывает ровно в 1.5 раза.
    # Без этого деления метрика завышала на sqrt(1.5) = 1.225 — одинаково на
    # синусе любой амплитуды и частоты, поэтому по одной проверке ошибку было
    # бы не отличить от калибровки. То же деление стоит в tools/stand/jerk_metric.py.
    return math.sqrt(power / NEBW_HANN), lines[:3]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "runs/ladder_open.csv"
    rows = [r for r in csv.DictReader(open(path)) if int(r["flags"]) & BAD_MASK == 0]
    seg = {}
    for r in rows:
        seg.setdefault(r["label"], []).append(r)

    print(f"файл: {path}, годных проб {len(rows)}")
    print(f"плавность = СКО остатка в полосе {BAND_LO:.0f}-{BAND_HI:.0f} Гц,"
          f" ход снят, часы платы\n")
    print(f"  {'скорость':>9}{'проб':>7}{'частота':>9}"
          f"{'ЗАХВАТ':>10}{'micros':>10}{'сильнейшая линия':>19}")
    print(f"  {'рад/с':>9}{'':>7}{'опроса':>9}{'град':>10}{'град':>10}"
          f"{'Гц (доля)':>19}")
    out = []
    for lab in sorted(seg):
        rr = seg[lab]
        w = float(rr[0]["spin"])
        ts = [sample_ts(r) for r in rr]
        t0 = ts[0]
        ts = [t - t0 for t in ts]
        fs = len(ts) / (ts[-1] - ts[0])

        # Участки без шва кадра: развёртка через шов опирается на измеренный
        # размах, и его ошибка даёт ступеньку, которую снятие хода размажет
        # по всей записи как шум.
        raw = [cap_frac(r) for r in rr]
        runs = split_runs(raw, raw, minlen=200) or [list(range(len(rr)))]
        best = max(runs, key=len)

        tt = [ts[i] for i in best]
        cap = [raw[i] * GAIN for i in best]
        isr = unwrap([isr_deg(rr[i]) for i in best])

        cu = resample(tt, cap, fs)
        iu = resample(tt, isr, fs)
        rc, lines = band_rms(detrend_line(cu), fs)
        ri, _ = band_rms(detrend_line(iu), fs)
        top = f"{lines[0][1]:.1f} ({lines[0][0]/sum(p for p,_ in lines)*100:.0f}%)" if lines else "-"
        print(f"  {w:>9.2f}{len(best):>7}{fs:>8.0f}Гц"
              f"{rc:>10.4f}{ri:>10.4f}{top:>19}")
        out.append((w, rc, ri))

    print()
    worst = max(out, key=lambda x: x[1])
    best_w = min(out, key=lambda x: x[1])
    print(f"  хуже всего: {worst[0]:.2f} рад/с -> {worst[1]:.4f}°")
    print(f"  лучше всего: {best_w[0]:.2f} рад/с -> {best_w[1]:.4f}°")
    print(f"  размах по лестнице: {worst[1]/best_w[1]:.1f}x")
    print()
    print("  ЧТО ВИДИТ СТАРЫЙ ТРАКТ. Его собственный шум 0.72-0.91° шире всей")
    print("  измеряемой величины, поэтому колонка micros почти не зависит от")
    print("  скорости: она показывает прибор, а не вал.")


if __name__ == "__main__":
    main()
