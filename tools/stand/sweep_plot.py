#!/usr/bin/env python3
"""Спектрограммы развёрток: разделение зубцового момента и резонанса.

Идея замера принадлежит Hero. Дискретная матрица не смогла различить две
гипотезы: по одной выборке на ячейку, и «сильнейший пик» скакал между
гармониками. Развёртка решает это без статистики — глазом:

  зубцовый момент -> частота ПРОПОРЦИОНАЛЬНА скорости. В герцах это лучи из
                     начала координат, в циклах за оборот — ГОРИЗОНТАЛИ на
                     целых кратностях числа пар полюсов.
  резонанс        -> частота ПОСТОЯННА. В герцах горизонталь, в циклах за
                     оборот — гипербола.

Тренд снимается В КАЖДОМ ОКНЕ отдельно, а не по всей записи: скорость меняется,
и один линейный тренд на сто секунд оставил бы в остатке саму развёртку.
"""
import sys, math
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm

R = 57.2957795
POLE_PAIRS = 11
WIN_S, HOP_S = 4.0, 0.5
FMAX = 30.0


def load(path):
    secs, cur = [], None
    for line in open(path, errors="replace"):
        line = line.strip()
        if line.startswith("#РАЗВЁРТКА"):
            cur = {"title": line[1:], "rows": []}
        elif line.startswith("#КОНЕЦ"):
            if cur and len(cur["rows"]) > 1000:
                secs.append(cur)
            cur = None
        elif line.startswith("t_ms,") or line.startswith("#"):
            continue
        else:
            p = line.split(",")
            if len(p) != 4:
                continue
            try:
                r = (float(p[0]) / 1000.0, float(p[1]), float(p[2]), float(p[3]))
            except ValueError:
                continue
            if cur is None:                      # заголовок A мог не попасть в лог
                cur = {"title": "РАЗВЁРТКА A (заголовок потерян)", "rows": []}
            cur["rows"].append(r)
    if cur and len(cur["rows"]) > 1000:
        secs.append(cur)
    return secs


def spectrogram(rows, fs):
    t = np.array([r[0] for r in rows])
    th = np.array([r[1] for r in rows])
    w = np.array([r[2] for r in rows])
    v = np.array([r[3] for r in rows])
    nwin, nhop = int(WIN_S * fs), int(HOP_S * fs)
    cols, tc, wc, vc = [], [], [], []
    win = np.hanning(nwin)
    for s in range(0, len(th) - nwin, nhop):
        seg = th[s:s + nwin]
        x = np.arange(nwin)
        a, b = np.polyfit(x, seg, 1)
        res = (seg - (a * x + b)) * win
        sp = 2 * np.abs(np.fft.rfft(res)) / (nwin * 0.5)
        cols.append(sp)
        tc.append(t[s + nwin // 2]); wc.append(w[s + nwin // 2]); vc.append(v[s + nwin // 2])
    S = np.array(cols).T * R                      # градусы
    f = np.fft.rfftfreq(nwin, 1 / fs)
    keep = f <= FMAX
    return S[keep], f[keep], np.array(tc), np.array(wc), np.array(vc)


def band_rms(S, f, lo=1.0, hi=20.0):
    sel = (f >= lo) & (f <= hi)
    A = S[sel]
    floor = np.median(A, axis=0)
    coh = np.maximum(0.0, A ** 2 - floor ** 2)
    return np.sqrt(coh.sum(axis=0) / 2.0)


if __name__ == "__main__":
    secs = load(sys.argv[1])
    fs = 200
    fig = plt.figure(figsize=(15, 11))
    fig.suptitle("Дрожание вала: развёртки по скорости и напряжению\n"
                 "B-G431B-ESC1, BGM4108, 11 пар полюсов, разомкнутый контур",
                 fontsize=13)

    A = secs[0]
    S, f, tc, wc, vc = spectrogram(A["rows"], fs)

    ax = fig.add_subplot(3, 2, 1)
    m = ax.pcolormesh(wc, f, np.maximum(S, 1e-4), norm=LogNorm(vmin=3e-3, vmax=0.6),
                      cmap="magma", shading="nearest")
    ax.set_xlabel("скорость, рад/с"); ax.set_ylabel("частота, Гц")
    ax.set_title("A. Спектрограмма в герцах при 2.0 В")
    for k, lbl in [(2, "2×эл"), (6, "6×эл"), (12, "12×эл")]:
        ax.plot(wc, wc / (2 * np.pi) * POLE_PAIRS * k, "c--", lw=0.8, alpha=0.7)
        ax.annotate(lbl, (wc[-1], wc[-1] / (2 * np.pi) * POLE_PAIRS * k),
                    color="c", fontsize=8, va="center")
    ax.set_ylim(0, FMAX)
    fig.colorbar(m, ax=ax, label="амплитуда, град")

    # то же в циклах за оборот: зубцы становятся горизонталями
    ax = fig.add_subplot(3, 2, 2)
    cyc_grid = np.linspace(2, 200, 400)
    Z = np.zeros((len(cyc_grid), len(wc)))
    for j in range(len(wc)):
        rev_s = wc[j] / (2 * np.pi)
        if rev_s <= 0:
            continue
        Z[:, j] = np.interp(cyc_grid * rev_s, f, S[:, j], left=0, right=0)
    m = ax.pcolormesh(wc, cyc_grid, np.maximum(Z, 1e-4),
                      norm=LogNorm(vmin=3e-3, vmax=0.6), cmap="magma", shading="nearest")
    for k in (2, 6, 12):
        ax.axhline(k * POLE_PAIRS, color="c", ls="--", lw=0.8, alpha=0.8)
        ax.annotate(f"{k}×эл = {k*POLE_PAIRS}", (wc[1], k * POLE_PAIRS + 3),
                    color="c", fontsize=8)
    ax.set_xlabel("скорость, рад/с"); ax.set_ylabel("циклов за оборот вала")
    ax.set_title("A. То же в циклах за оборот\n(зубцы = горизонтали, резонанс = гипербола)")
    fig.colorbar(m, ax=ax, label="амплитуда, град")

    ax = fig.add_subplot(3, 2, 3)
    j_a = band_rms(S, f)
    ax.plot(wc, j_a, lw=1.6, color="#c1121f")
    ax.axhline(0.1, color="g", ls="--", lw=1, label="цель 0.1°")
    ax.set_xlabel("скорость, рад/с"); ax.set_ylabel("дрожание, град СКО")
    ax.set_title("A. Критерий против скорости"); ax.grid(alpha=0.3); ax.legend()

    B = secs[1] if len(secs) > 1 else None
    if B:
        S2, f2, tc2, wc2, vc2 = spectrogram(B["rows"], fs)
        ax = fig.add_subplot(3, 2, 4)
        m = ax.pcolormesh(vc2, f2, np.maximum(S2, 1e-4),
                          norm=LogNorm(vmin=3e-3, vmax=0.6), cmap="magma", shading="nearest")
        for k in (2, 6, 12):
            ax.axhline(1.0 / (2 * np.pi) * POLE_PAIRS * k, color="c", ls="--", lw=0.8, alpha=0.7)
        ax.set_xlabel("напряжение, В"); ax.set_ylabel("частота, Гц")
        ax.set_title("B. Спектрограмма при 1.0 рад/с")
        ax.set_ylim(0, FMAX)
        fig.colorbar(m, ax=ax, label="амплитуда, град")

        ax = fig.add_subplot(3, 2, 5)
        j_b = band_rms(S2, f2)
        ax.plot(vc2, j_b, lw=1.6, color="#003049")
        ax.axhline(0.1, color="g", ls="--", lw=1, label="цель 0.1°")
        ax.set_xlabel("напряжение, В"); ax.set_ylabel("дрожание, град СКО")
        ax.set_title("B. Критерий против напряжения при 1.0 рад/с")
        ax.grid(alpha=0.3); ax.legend()

        ax = fig.add_subplot(3, 2, 6)
        for k, c in [(2, "#c1121f"), (6, "#003049"), (12, "#588157")]:
            fk = 1.0 / (2 * np.pi) * POLE_PAIRS * k
            i = np.argmin(np.abs(f2 - fk))
            band = S2[max(0, i - 1):i + 2].max(axis=0)
            ax.plot(vc2, band, lw=1.5, color=c, label=f"{k}×электрической ({fk:.1f} Гц)")
        ax.set_xlabel("напряжение, В"); ax.set_ylabel("амплитуда гармоники, град")
        ax.set_title("B. Отдельные гармоники против напряжения")
        ax.grid(alpha=0.3); ax.legend(fontsize=8)

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    out = sys.argv[2]
    fig.savefig(out, dpi=120)
    print("сохранено:", out)
    print(f"\nA: дрожание {j_a.min():.3f}..{j_a.max():.3f}°, худшее при "
          f"{wc[int(np.argmax(j_a))]:.2f} рад/с")
    if B:
        print(f"B: дрожание {j_b.min():.3f}..{j_b.max():.3f}°, лучшее при "
              f"{vc2[int(np.argmin(j_b))]:.2f} В")
