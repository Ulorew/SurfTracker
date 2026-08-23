#!/usr/bin/env python3
"""Метрика стандартного протокола сравнения конфигураций мотора.

    tools/stand/motor_metric.py runs/base_2v5.txt [ещё файлы...]
    tools/stand/motor_metric.py --самопроверка

ЗАЧЕМ ОТДЕЛЬНО ОТ jerk_metric.py. Та меряет в полосе 1..20 Гц с вычетом
шумового пола и годится для сравнения ТРАКТОВ. Для сравнения КОНФИГУРАЦИЙ
мотора она слепа: на рабочих скоростях 0.05..0.20 рад/с 70..82% мощности
остатка лежит НИЖЕ 1 Гц, потому что зубцовая помеха 22-го порядка при
0.20 рад/с приходится на 0.70 Гц. Здесь полосы разделены явно, а главным
числом идёт ПОЛНОЕ СКО.

ЧТО СЧИТАЕТСЯ И ПОЧЕМУ ИМЕННО ТАК

  ход/команда   наклон фактического хода к заданному. Отдельным числом, а не
                внутри плавности: «не та скорость» и «не гладко» — разные
                дефекты с разными причинами, и смешивать их в одно число
                значит получить величину, которая растёт с длиной записи.

  полное СКО    остаток после снятия ФАКТИЧЕСКОГО тренда. Главное число.
                Фактического, а не командного: иначе рассинхрон втекает в
                плавность пилой.

  0.15..1 Гц    зубцы и медленное блуждание. Нижняя граница 0.15 Гц, а не 0:
                на 12-секундной записи бины ниже неё забиты остатком снятия
                тренда и от него неотличимы.

  1..20 Гц      рябь. Верхняя граница — там, где начинается широкополосный
                шум датчика.

  Uq            среднее, размах и доля клампа берутся ИЗ ЗАГОЛОВКА прогона:
                их знает только прошивка. Среднее Uq — прокси нагрева.

САМОПРОВЕРКА ОБЯЗАТЕЛЬНА. Синтетический сигнал с линией 0.7 Гц обязан попасть
в нижнюю полосу и почти не попасть в верхнюю. Прибор, не проверенный на
сигнале с известным ответом, меряет себя.
"""
import sys, math
import numpy as np

R = 57.2957795
F_PX = 1299.256      # фокус в пикселях при ширине кадра 1920
W_PX = 1920
LO = (0.15, 1.0)
HI = (1.0, 20.0)
NEBW_HANN = 1.5


def load(path):
    """Читает дампы прошивки closed_loop_g431. Заголовок разбирается целиком:
    поля, которых нет, просто отсутствуют в словаре."""
    runs, cur = [], None
    for line in open(path, errors="replace"):
        line = line.strip()
        if line.startswith("#НАЧАЛО"):
            m = {}
            for kv in line.split()[1:]:
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    m[k] = v
            cur = {"hdr": m, "data": []}
        elif line.startswith("#КОНЕЦ"):
            if cur and len(cur["data"]) > 100:
                runs.append(cur)
            cur = None
        elif cur is not None:
            try:
                cur["data"].append(float(line))
            except ValueError:
                pass
    return runs


def band_rms(res_rad, fs, lo, hi):
    """СКО в полосе, градусы. Остаток подаётся в РАДИАНАХ, тренд уже снят."""
    n = len(res_rad)
    sp = np.abs(np.fft.rfft(res_rad * np.hanning(n))) * 2.0 / (n * 0.5)
    f = np.fft.rfftfreq(n, 1.0 / fs)
    sel = (f >= lo) & (f <= hi)
    return math.sqrt(float((sp[sel] ** 2).sum()) / 2.0 / NEBW_HANN) * R


def measure(run):
    d = np.asarray(run["data"], float)
    hdr = run["hdr"]
    fs = int(hdr.get("fs", 200))
    w = float(hdr.get("w", 0.0))
    t = np.arange(len(d)) / fs
    k, b = np.polyfit(t, d, 1)
    res = d - (k * t + b)
    out = {
        "w": w,
        "n": len(d),
        "T": len(d) / fs,
        "ход": (abs(k) / abs(w)) if abs(w) > 1e-9 else float("nan"),
        "скo": res.std() * R,
        "низ": band_rms(res, fs, *LO),
        "верх": band_rms(res, fs, *HI),
        "режим": hdr.get("режим", "?"),
    }
    out["кадр"] = out["скo"] / R * F_PX / W_PX
    for key, src in (("Uq_сред", "Uq_сред"), ("упор", "упор")):
        if src in hdr:
            try:
                out[key] = float(hdr[src].rstrip("%"))
            except ValueError:
                pass
    if "Uq" in hdr:
        out["Uq_разм"] = hdr["Uq"]
    return out


def report(paths):
    rows = []
    for p in paths:
        for r in load(p):
            m = measure(r)
            m["файл"] = p.split("/")[-1]
            rows.append(m)
    if not rows:
        print("нет прогонов")
        return 2
    print(f"{'режим':<12}{'рад/с':>7}{'ход/ком':>9}{'СКО,°':>8}"
          f"{'0.15-1':>8}{'1-20':>7}{'кадр':>7}{'Uq_ср':>7}{'упор%':>7}")
    for m in rows:
        print(f"{m['режим']:<12}{m['w']:>7.2f}{m['ход']:>9.3f}{m['скo']:>8.3f}"
              f"{m['низ']:>8.3f}{m['верх']:>7.3f}{m['кадр']:>7.1%}"
              f"{m.get('Uq_сред', float('nan')):>7.3f}{m.get('упор', float('nan')):>7.0f}")
    # свод по модулю скорости: обе стороны в одну строку
    print()
    speeds = sorted({round(abs(m["w"]), 3) for m in rows})
    print(f"{'|рад/с|':>8}{'сторон':>8}{'СКО,°':>8}{'0.15-1':>8}{'1-20':>7}{'Uq_ср':>7}")
    for s in speeds:
        sel = [m for m in rows if abs(round(abs(m["w"]), 3) - s) < 1e-9]
        f = lambda k: np.mean([m[k] for m in sel if not math.isnan(m.get(k, float("nan")))])
        print(f"{s:>8.2f}{len(sel):>8d}{f('скo'):>8.3f}{f('низ'):>8.3f}"
              f"{f('верх'):>7.3f}{f('Uq_сред'):>7.3f}")
    return 0


def selftest():
    """Известный ответ: линия 0.7 Гц заданной амплитуды.

    Проверяются три вещи, и каждая может провалиться отдельно:
      1. линия попадает в НИЖНЮЮ полосу, а не в верхнюю;
      2. её амплитуда восстанавливается (СКО синусоиды = A/sqrt(2));
      3. на чистом ходе без ряби метрика читает около нуля.
    """
    fs, T = 200, 12.0
    t = np.arange(int(fs * T)) / fs
    A_deg, f0 = 0.30, 0.70
    ok = True

    y = (0.20 * t) + (A_deg / R) * np.sin(2 * np.pi * f0 * t)
    run = {"hdr": {"fs": str(fs), "w": "0.20", "режим": "тест"}, "data": list(y)}
    m = measure(run)
    want = A_deg / math.sqrt(2)
    print(f"1. линия {f0} Гц амплитудой {A_deg}°: СКО {m['скo']:.4f}° (ждём {want:.4f})")
    if abs(m["скo"] - want) > 0.05 * want:
        print("   ПРОВАЛ: амплитуда не восстановилась"); ok = False
    print(f"   низ {m['низ']:.4f}°  верх {m['верх']:.4f}°")
    if not (m["низ"] > 0.9 * want and m["верх"] < 0.1 * want):
        print("   ПРОВАЛ: линия попала не в ту полосу"); ok = False

    y2 = 0.20 * t
    m2 = measure({"hdr": {"fs": str(fs), "w": "0.20", "режим": "тест"}, "data": list(y2)})
    print(f"2. чистый ход без ряби: СКО {m2['скo']:.6f}° (ждём около нуля)")
    if m2["скo"] > 1e-6:
        print("   ПРОВАЛ: на гладком ходе метрика не нуль"); ok = False

    y3 = 0.10 * t + (A_deg / R) * np.sin(2 * np.pi * f0 * t)
    m3 = measure({"hdr": {"fs": str(fs), "w": "0.20", "режим": "тест"}, "data": list(y3)})
    print(f"3. ход вдвое ниже команды: ход/ком {m3['ход']:.3f} (ждём 0.5), "
          f"СКО {m3['скo']:.4f}° (ждём {want:.4f}, рассинхрон НЕ течёт в плавность)")
    if abs(m3["ход"] - 0.5) > 0.01 or abs(m3["скo"] - want) > 0.05 * want:
        print("   ПРОВАЛ: рассинхрон и плавность перепутаны"); ok = False

    print("\nСАМОПРОВЕРКА:", "всё сошлось" if ok else "ЕСТЬ ПРОВАЛЫ")
    return 0 if ok else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "--самопроверка":
        sys.exit(selftest())
    sys.exit(report(args))
