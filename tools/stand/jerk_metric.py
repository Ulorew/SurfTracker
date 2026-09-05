#!/usr/bin/env python3
"""Критерий дрожания вала. ОБЪЯВЛЕН ДО ПРОГОНА МАТРИЦЫ.

    дрожание = СКО остатка угла в полосе 1..20 Гц за вычетом шумового пола,
               в градусах.

Почему так, по частям:

ОСТАТОК. Угол минус линейный тренд. При постоянной команде угол растёт
линейно; всё интересное — в остатке.

ПОЛОСА 1..20 Гц. Снизу отрезаны дрейф и артефакты снятия тренда: на четырёх-
восьмисекундном окне самые низкие бины забиты остатком тренда, и в пробном
прогоне бин 0.25 Гц выходил в лидеры, не означая ничего. Сверху отрезан
широкополосный шум датчика. Глаз и видео замечают именно эту полосу.

ВЫЧЕТ ПОЛА. Шум датчика некогерентный: в спектре он размазан, и его уровень
хорошо оценивается МЕДИАНОЙ бинов полосы — редкие когерентные пики в медиану
не попадают. Вычитается в квадратуре, потому что складываются мощности, а не
амплитуды. Без этого вычета мы мерили бы датчик, как уже вышло однажды.

ГРАДУСЫ. При кропе шириной около 30 градусов один пиксель кадра 640 — это
0.047 градуса. То есть 0.1 — примерно два пикселя дрожания, 0.5 — десять.

Цель, объявленная заранее: <= 0.1 градуса во всём рабочем диапазоне скоростей.
"""
import sys, math
import numpy as np

BAND_LO, BAND_HI = 1.0, 20.0
NEBW_HANN = 1.5      # шумовая полоса окна Ханна, в бинах
TARGET_DEG = 0.1
R = 57.2957795


def load(path):
    runs, cur = [], None
    for line in open(path, errors="replace"):
        line = line.strip()
        if line.startswith("#НАЧАЛО"):
            m = dict(kv.split("=") for kv in line.split()[1:])
            cur = {"w": float(m["w"]), "volts": float(m["volts"]),
                   "fs": int(m["fs"]), "data": []}
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


def jitter(y, fs, lo=None, hi=None):
    """-> (дрожание в градусах, пол в градусах, частота сильнейшего пика, факт. скорость)

    ИСПРАВЛЕНО 11 августа. Прежняя редакция возвращала 0.098 градуса на ЧИСТОМ
    шуме с проектным размахом 1.57 — то есть объявленная цель 0.1 лежала ниже
    собственного пьедестала прибора и была недостижима по построению, каким бы
    ни был мотор. Три ошибки, каждая в свою сторону:

    1. КЛИППИНГ. max(0, a^2 - floor^2) обнулял отрицательные вклады примерно
       половины бинов, снимая ровно половину мощности шума вместо всей.
       Вычитать надо со знаком: положительные и отрицательные отклонения от
       пола в среднем гасят друг друга, и остаётся только когерентная часть.
    2. ОЦЕНКА ПОЛА. Медиана амплитуд для рэлеевского шума занижена: медиана
       МОЩНОСТИ равна ln2 от средней. Делим на ln2, иначе пол занижен.
    3. ШУМОВАЯ ПОЛОСА ОКНА. У Ханна NEBW = 1.5: окно размазывает мощность по
       полутора бинам, и без деления получалось +22% систематики.

    Проверка встроена: metric_selftest() обязана вернуть около нуля на чистом
    шуме. Прибор, не проверенный на отсутствии сигнала, меряет себя.
    """
    y = np.asarray(y, dtype=float)
    n = len(y)
    x = np.arange(n)
    a, b = np.polyfit(x, y, 1)
    res = y - (a * x + b)
    speed = a * fs

    win = np.hanning(n)
    sp = np.fft.rfft(res * win)
    amp = 2.0 * np.abs(sp) / (n * 0.5)
    freq = np.fft.rfftfreq(n, 1.0 / fs)
    # ПОЛОСА МОЖЕТ БЫТЬ ЗАДАНА СНАРУЖИ. Умолчание 1-20 Гц привязано ко
    # ВРЕМЕНИ, а зубцовая помеха привязана к ОБОРОТУ: 22 цикла на оборот при
    # 11 парах полюсов. На малых скоростях она уходит вниз из полосы —
    # v=0.1 рад/с даёт 0.350 Гц, v=0.2 даёт 0.700, то есть ДВЕ скорости из
    # пяти в матрице мерились вслепую. Вызывающий, знающий скорость, должен
    # уметь задать полосу в порядках. Умолчание не меняю: по нему сняты все
    # прежние числа, и подмена молча переписала бы историю.
    _lo = BAND_LO if lo is None else lo
    _hi = BAND_HI if hi is None else hi
    sel = (freq >= _lo) & (freq <= _hi)
    a_band, f_band = amp[sel], freq[sel]
    if len(a_band) == 0:
        return 0.0, 0.0, 0.0, speed

    p = a_band ** 2
    # Медиана МОЩНОСТИ рэлеевского шума = ln2 * средняя мощность.
    p_noise = float(np.median(p)) / math.log(2.0)
    # Вычитание СО ЗНАКОМ и деление на шумовую полосу окна Ханна.
    coh = float((p - p_noise).sum()) / NEBW_HANN
    rms = math.sqrt(max(0.0, coh) / 2.0)
    # Пол печатается как СКО ПО ВСЕЙ ПОЛОСЕ, а не по одному бину: прежняя
    # колонка занижала его почти в девять раз и потому никого не настораживала.
    floor_rms = math.sqrt(p_noise * len(a_band) / NEBW_HANN / 2.0)
    peak_f = float(f_band[int(np.argmax(a_band))])
    return rms * R, floor_rms * R, peak_f, speed


def order_amplitude(res, theta_cmd, order):
    """Амплитуда линии заданного ПОРЯДКА по командному углу, радианы.

    Проекция на cos/sin(order * theta_cmd) напрямую, без спектра. Не зависит
    от пьедестала вовсе: шум некогерентен с командным углом и усредняется в
    ноль, а не добавляется к результату. Для 60 с записи шум проекции около
    0.006 градуса — в тридцать раз ниже сигнала.
    """
    res = np.asarray(res, dtype=float)
    th = np.asarray(theta_cmd, dtype=float)
    z = 2.0 * np.mean(res * np.exp(-1j * order * th))
    return abs(z)


def metric_selftest(n=1600, fs=200, span_deg=1.57, trials=40, seed=0):
    """Прибор на ОТСУТСТВИИ сигнала. Обязан вернуть около нуля.

    Шум берётся равномерный с тем же размахом, что даёт датчик на неподвижном
    вале (1.57 градуса) — то есть проверяется ровно тот пьедестал, который
    прежняя редакция принимала за дрожание.
    """
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(trials):
        y = rng.uniform(-span_deg / 2, span_deg / 2, n) / R
        out.append(jitter(y, fs)[0])
    return float(np.mean(out)), float(np.max(out))


def main(paths):
    m, mx = metric_selftest()
    print(f"САМОПРОВЕРКА на чистом шуме размахом 1.57°: среднее {m:.4f}°, "
          f"худшее {mx:.4f}° (обязано быть около нуля)")
    print()
    cells = {}
    for p in paths:
        for r in load(p):
            j, fl, pf, sp = jitter(r["data"], r["fs"])
            cells[(r["volts"], r["w"])] = (j, fl, pf, sp)
    volts = sorted({k[0] for k in cells})
    speeds = sorted({k[1] for k in cells})

    print("ДРОЖАНИЕ, градусы СКО в полосе "
          f"{BAND_LO}..{BAND_HI} Гц за вычетом пола")
    print("           " + "".join(f"{s:>9.2f}" for s in speeds) + "   рад/с")
    for v in volts:
        row = f"  {v:4.1f} В  "
        for s in speeds:
            c = cells.get((v, s))
            row += f"{c[0]:>9.3f}" if c else "        -"
        print(row)

    print("\nчастота сильнейшего пика, Гц")
    print("           " + "".join(f"{s:>9.2f}" for s in speeds))
    for v in volts:
        row = f"  {v:4.1f} В  "
        for s in speeds:
            c = cells.get((v, s))
            row += f"{c[2]:>9.2f}" if c else "        -"
        print(row)

    print("\nотношение факт/команда (провал = потеря синхронизма)")
    print("           " + "".join(f"{s:>9.2f}" for s in speeds))
    for v in volts:
        row = f"  {v:4.1f} В  "
        for s in speeds:
            c = cells.get((v, s))
            row += f"{c[3]/s:>9.3f}" if c else "        -"
        print(row)

    ok = [(v, s, c[0]) for (v, s), c in cells.items() if c[0] <= TARGET_DEG]
    print(f"\nячеек в пределах цели {TARGET_DEG}°: {len(ok)} из {len(cells)}")
    best_v = None
    for v in volts:
        worst = max(cells[(v, s)][0] for s in speeds if (v, s) in cells)
        print(f"  {v:4.1f} В: худшая скорость даёт {worst:.3f}°")
        if best_v is None or worst < best_v[1]:
            best_v = (v, worst)
    print(f"\nлучшее напряжение по худшей скорости: {best_v[0]:.1f} В "
          f"({best_v[1]:.3f}°)")


if __name__ == "__main__":
    main(sys.argv[1:])
