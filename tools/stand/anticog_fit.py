#!/usr/bin/env python3
"""Разбор калибровки зубцового момента: шаги 2-3 инструкции.

Считает профиль возмущения по электрическому углу, отделяет трение от зубцов
полусуммой направлений и проверяет форму спектром.

Ключевые решения, отличные от «просто усреднить»:

БИННИНГ ПО МЕДИАНЕ, а не по среднему — выбросы PWM-чтения датчика
несимметричны, и среднее их тащит (§2.3 инструкции).

ПОЛУСУММА направлений — зубцовый момент чётен по положению, трение
направленно. Без разделения трение затекает в таблицу и компенсация начинает
помогать в одну сторону и мешать в другую (§2.4).

СТАРТОВЫЙ УЧАСТОК ВЫБРАСЫВАЕТСЯ. При первом включении поля ротор дёргается,
подстраиваясь под начальное положение: замерено -19.2 градуса за первые две
секунды. Не выбросив его, мы внесли бы в тренд перекос, которого в
установившемся вращении нет.
"""
import sys, math
import numpy as np

POLE_PAIRS = 11
NBINS = 64
SKIP_S = 3.0          # стартовый рывок плюс запас
R = 57.2957795


def load(path):
    secs, cur = [], None
    for line in open(path, errors="replace"):
        s = line.strip()
        if s.startswith("#НАПРАВЛЕНИЕ"):
            m = {}
            for kv in s.split()[2:]:
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    m[k] = v
            cur = {"label": s.split()[1], "w": float(m.get("w", 0)), "rows": []}
        elif s.startswith("#") or s.startswith("t_ms"):
            if s.startswith("#КОНЕЦ") and cur:
                if len(cur["rows"]) > 500:
                    secs.append(cur)
                cur = None
        elif cur is not None:
            p = s.split(",")
            if len(p) != 4:
                continue
            try:
                cur["rows"].append(tuple(float(x) for x in p))
            except ValueError:
                pass
    if cur and len(cur["rows"]) > 500:
        secs.append(cur)
    return secs


def profile(sec):
    a = np.array(sec["rows"])
    t = a[:, 0] / 1000.0
    th = a[:, 1]
    ph = a[:, 2]
    keep = t >= (t[0] + SKIP_S)
    t, th, ph = t[keep], th[keep], ph[keep]

    k, b = np.polyfit(t, th, 1)
    res = th - (k * t + b)          # радианы

    idx = np.clip((ph / (2 * math.pi) * NBINS).astype(int), 0, NBINS - 1)
    med = np.full(NBINS, np.nan)
    cnt = np.zeros(NBINS, int)
    for i in range(NBINS):
        m = idx == i
        cnt[i] = m.sum()
        if cnt[i] >= 5:
            med[i] = np.median(res[m])
    med -= np.nanmean(med)          # постоянная составляющая — в U_база
    return med, cnt, k, len(t)


def harm(prof, n):
    """Комплексная амплитуда n-й гармоники профиля по электрическому углу."""
    ang = (np.arange(NBINS) + 0.5) / NBINS * 2 * math.pi
    good = ~np.isnan(prof)
    return 2 * np.mean(prof[good] * np.exp(-1j * n * ang[good]))


if __name__ == "__main__":
    secs = load(sys.argv[1])
    print(f"секций найдено: {len(secs)}")
    profs = {}
    for s in secs:
        p, c, k, n = profile(s)
        profs[s["label"]] = p
        print(f"\n{s['label']}: отсчётов {n}, скорость {k:.4f} рад/с "
              f"({100*abs(k)/abs(s['w']):.1f}% команды)")
        print(f"  проходов на корзину: медиана {int(np.median(c))}, минимум {int(c.min())}")
        print(f"  размах профиля {np.nanmax(p)*R - np.nanmin(p)*R:.3f}°, "
              f"СКО {np.nanstd(p)*R:.3f}°")

    if len(profs) == 2:
        f = profs.get("ВПЕРЁД"); b = profs.get("НАЗАД")
        cog = (f + b) / 2          # чётная часть: зубцы
        fri = (f - b) / 2          # нечётная: трение
        print(f"\n=== РАЗДЕЛЕНИЕ ===")
        print(f"зубцы  (полусумма):  размах {np.nanmax(cog)*R-np.nanmin(cog)*R:.3f}°, "
              f"СКО {np.nanstd(cog)*R:.3f}°")
        print(f"трение (полуразность): размах {np.nanmax(fri)*R-np.nanmin(fri)*R:.3f}°, "
              f"СКО {np.nanstd(fri)*R:.3f}°")

        print(f"\n=== СПЕКТР ПРОФИЛЯ ЗУБЦОВ по электрическому углу ===")
        print(" гармоника  амплитуда, град   что это на оборот вала")
        tot = 0
        for n in range(1, 7):
            A = abs(harm(cog, n)) * R
            tot += A * A
            print(f"    {n}       {A:8.4f}        {n*POLE_PAIRS} циклов")
        A2 = abs(harm(cog, 2)) * R
        A1 = abs(harm(cog, 1)) * R
        print(f"\nдоминирует {'2-я — ОЖИДАЕМО (22 цикла на оборот)' if A2>=A1 and A2>=max(abs(harm(cog,n))*R for n in [3,4,5,6]) else 'НЕ 2-я — разбираться со привязкой нуля'}")
        np.save(sys.argv[2] if len(sys.argv) > 2 else "/tmp/cog_profile.npy",
                np.vstack([cog, fri]))
        print(f"профиль сохранён")
