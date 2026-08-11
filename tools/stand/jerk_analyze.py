#!/usr/bin/env python3
"""Разбор записи угла на 200 Гц: спектр остатка после снятия тренда.

Зачем спектр. Шум датчика широкополосный: в спектре он размазан ровно.
Зубцовый момент даёт ПЕРИОДИЧЕСКУЮ помеху на частоте, пропорциональной
скорости, и собирается в узкий пик. Пик виден над шумом даже тогда, когда в
среднеквадратичном разбросе помеха неотличима — а именно так и вышло в
ripple_g431, где окно 0.5 с съедало всё быстрое.

Тренд снимается ЛИНЕЙНЫЙ: при постоянной команде угол растёт линейно, и всё
интересное — это остаток. Не сняв тренд, мы бы получили спектр пилы.
"""
import sys, math, cmath

def load(path):
    runs = []
    cur = None
    for line in open(path, errors="replace"):
        line = line.strip()
        if line.startswith("#НАЧАЛО"):
            meta = dict(kv.split("=") for kv in line.split()[1:])
            cur = {"w": float(meta["w"]), "volts": float(meta["volts"]),
                   "fs": int(meta["fs"]), "data": []}
        elif line.startswith("#КОНЕЦ"):
            if cur and len(cur["data"]) > 100: runs.append(cur)
            cur = None
        elif cur is not None:
            try: cur["data"].append(float(line))
            except ValueError: pass
    return runs

def detrend(y):
    n = len(y); sx = (n-1)*n/2; sxx = sum(i*i for i in range(n))
    sy = sum(y); sxy = sum(i*y[i] for i in range(n))
    d = n*sxx - sx*sx
    a = (n*sxy - sx*sy)/d; b = (sy - a*sx)/n
    return [y[i] - (a*i + b) for i in range(n)], a

def dft_mag(x, fs, fmax):
    """Прямое ДПФ на нужных частотах: numpy тут не нужен, точек мало."""
    n = len(x)
    out = []
    kmax = int(fmax * n / fs)
    for k in range(1, kmax+1):
        w = -2j*math.pi*k/n
        s = sum(x[i]*cmath.exp(w*i) for i in range(n))
        out.append((k*fs/n, 2*abs(s)/n))
    return out

for path in sys.argv[1:]:
    print(f"\n===== {path} =====")
    for r in load(path):
        y, slope = detrend(r["data"])
        fs, w = r["fs"], r["w"]
        meas = slope*fs
        rms = math.sqrt(sum(v*v for v in y)/len(y))
        pk = max(y) - min(y)
        R = 57.2958
        print(f"\n  команда {w:.2f} рад/с при {r['volts']:.1f} В — факт {meas:.4f} "
              f"(отношение {meas/w:.3f})")
        print(f"    остаток: СКО {rms*R:.4f}°, размах {pk*R:.4f}°")
        # Спектр до 60 Гц: зубцовая помеха при 11 парах полюсов и 1 рад/с
        # лежит около 1/(2pi)*1.0*11*2 ~ 3.5 Гц и растёт со скоростью.
        sp = dft_mag(y[:800], fs, 60.0)
        sp.sort(key=lambda t: -t[1])
        floor = sorted(m for _, m in dft_mag(y[:800], fs, 60.0))
        med = floor[len(floor)//2]
        print(f"    шумовой пол спектра (медиана) {med*R:.5f}°")
        print(f"    сильнейшие частоты:")
        for f, m in sp[:4]:
            # Электрический оборот: 11 пар полюсов. Зубцы обычно кратны им.
            harm = f / (w * 11 / (2*math.pi)) if w > 0 else 0
            print(f"      {f:6.2f} Гц  амплитуда {m*R:.4f}°  "
                  f"({m/med:.1f}x над полом, {harm:.2f} от электрической)")
