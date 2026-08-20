#!/usr/bin/env python3
"""Метрики обоих трактов энкодера по сырому DIAG-логу.

ДВА ТРАКТА, ДВЕ ШКАЛЫ. Боевой тракт (micros в обработчике прерывания) уже
откалиброван: SENS_MIN/MAX подобраны на этом железе. Тракт захвата даёт
скважность, и во что она переводится — вопрос, а не данность.

ПОЭТОМУ МОСТ МЕЖДУ ШКАЛАМИ МЕРЯЕТСЯ ЯВНО. Наклон и смещение берутся
подгонкой по данным вращения, а не подставляются из даташита. Проверка «а
похоже на правду» такой мост не проверяет: она одинаково довольна и верным
переводом, и сдвинутым на постоянную.

МЕТРИКИ РАЗНЫЕ ДЛЯ РАЗНЫХ ТРАКТОВ. «Минимальный интервал между уровнями»
осмыслен только там, где уровней мало: у захвата их почти столько же, сколько
проб, и интервал вырождается в ноль. Он оставлен как ПРИЗНАК КВАНТОВАНИЯ, а
сравниваются тракты по СКО и робастной оценке.
"""
import argparse, csv, math, sys
from collections import defaultdict

SENS_MIN_US, SENS_MAX_US = 3.0, 919.0
CPR_ISR = SENS_MAX_US - SENS_MIN_US + 1.0       # 917, как в MagneticSensorPWM
QUANT_ISR = 360.0 / CPR_ISR                     # 0.39258 град на тик micros()
TIM2_HZ = 170e6

DF = {1: "cap_bad", 2: "isr_bad", 4: "cap_lost", 8: "no_edges"}


def isr_deg(row):
    """Формула боевого тракта — ровно та, что применяет MagneticSensorPWM."""
    return (float(row["isr_high"]) - SENS_MIN_US) / CPR_ISR * 360.0


def cap_frac(row):
    """Сырая скважность захвата, 0..1. Ни во что не переведена намеренно."""
    p = float(row["cap_period"])
    return float(row["cap_high"]) / p if p > 0 else float("nan")


def unwrap(xs, period=360.0):
    out, off = [], 0.0
    for i, x in enumerate(xs):
        if i:
            d = x + off - out[-1]
            if d > period / 2:
                off -= period
            elif d < -period / 2:
                off += period
        out.append(x + off)
    return out


def mad_sd(xs):
    """Робастная оценка СКО: MAD * 1.4826.

    ВЫРОЖДАЕТСЯ НА КВАНТОВАННЫХ ДАННЫХ и возвращает тогда nan, а не ноль.
    Если больше половины проб попали в один уровень — а на тихом стенде у
    micros-тракта это норма, — медиана отклонений равна нулю. Ноль здесь
    означает «мера неприменима», но выглядит как «шума нет», и дальше из него
    получается выигрыш «0.0x». Такое число обязано быть отмечено, а не
    напечатано наравне с измеренными.
    """
    if not xs:
        return float("nan")
    m = sorted(xs)[len(xs) // 2]
    d = sorted(abs(x - m) for x in xs)
    v = d[len(d) // 2] * 1.4826
    return v if v > 0 else float("nan")


def sd(xs):
    n = len(xs)
    if n < 2:
        return float("nan")
    mu = sum(xs) / n
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1))


def min_level_gap(xs, eps=1e-9):
    """Типичный интервал между соседними РАЗЛИЧНЫМИ уровнями.

    Медиана разностей, а не минимум: минимум ловит одну случайную близкую
    пару и занижает шаг. Для неквантованного тракта величина смысла не имеет
    — она тут именно чтобы квантованность было видно.
    """
    u = sorted(set(round(x, 9) for x in xs))
    if len(u) < 2:
        return float("nan")
    gaps = sorted(b - a for a, b in zip(u, u[1:]) if b - a > eps)
    return gaps[len(gaps) // 2] if gaps else float("nan")


def detrend(xs, ts):
    """Убрать линейный ход. Остаток — то, что не объясняется вращением.

    На вращении СКО меряет сам ход вала (сотни градусов) и о тракте не
    говорит ничего. После снятия прямой в остатке остаются шум тракта И
    настоящие неровности хода — зубцовый момент, неравномерность разгона.
    Вторые ОБЩИЕ для обоих трактов, поэтому по остатку тракты сравнивать
    можно, а называть остаток шумом датчика — нельзя.
    """
    n = len(xs)
    if n < 3:
        return xs
    mt = sum(ts) / n
    mx = sum(xs) / n
    stt = sum((t - mt) ** 2 for t in ts)
    if stt < 1e-12:
        return [x - mx for x in xs]
    k = sum((t - mt) * (x - mx) for t, x in zip(ts, xs)) / stt
    return [x - (mx + k * (t - mt)) for x, t in zip(xs, ts)]


def _lsq(frac, deg):
    n = len(frac)
    mx, my = sum(frac) / n, sum(deg) / n
    sxx = sum((x - mx) ** 2 for x in frac)
    if sxx < 1e-12:
        return None
    k = sum((x - mx) * (y - my) for x, y in zip(frac, deg)) / sxx
    b = my - k * mx
    return k, b, sd([y - (k * x + b) for x, y in zip(frac, deg)])


def split_runs(frac, deg, jump=0.5, minlen=50):
    """Разбить ход на участки БЕЗ ПЕРЕХОДА через шов кадра датчика.

    ЗАЧЕМ ЭТО НУЖНО. Скважность пробегает не всю единицу: часть кадра
    служебная. На переходе она падает с верхней границы диапазона сразу на
    нижнюю, и развёртка фазы этот мёртвый участок ПРОГЛАТЫВАЕТ — добавляет
    ровно 1.0 вместо настоящего размаха. Наклон, посчитанный сквозь переход,
    схлопывается к наивным 360 независимо от того, каков он на самом деле.

    Поймано подложкой: заложенные 371.5 восстанавливались как 359.5.
    """
    runs, cur = [], [0]
    for i in range(1, len(frac)):
        if abs(frac[i] - frac[i - 1]) > jump:
            runs.append(cur); cur = []
        cur.append(i)
    runs.append(cur)
    return [r for r in runs if len(r) >= minlen]


def fit_bridge(frac, deg, minlen=50):
    """СЫРЫЕ frac и deg -> мост. -> (наклон, смещение, остаток, N, разброс).

    Величины подаются НЕРАЗВЁРНУТЫМИ намеренно: разбиение ищет швы кадра
    именно по скачкам, а на развёрнутых данных швов уже нет — первая
    редакция получала один сплошной участок и тот же схлопнутый наклон.

    Наклон берётся МЕДИАНОЙ по участкам, а рядом печатается их разброс:
    если участки дают разные наклоны, мост не описывается прямой, и
    единственное число об этом бы умолчало.
    """
    runs = split_runs(frac, deg, minlen=minlen)
    fits = []
    for r in runs:
        # Внутри участка шва нет, но угол боевого тракта всё равно может
        # перевалить через 360 — его разворачиваем локально.
        f = _lsq([frac[i] for i in r], unwrap([deg[i] for i in r]))
        if f:
            fits.append(f)
    if not fits:
        return None
    ks = sorted(f[0] for f in fits)
    k = ks[len(ks) // 2]
    bs = sorted(f[1] for f in fits)
    b = bs[len(bs) // 2]
    resid = sorted(f[2] for f in fits)[len(fits) // 2]
    spread = (ks[-1] - ks[0]) if len(ks) > 1 else 0.0
    return k, b, resid, len(fits), spread


def load(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def fmt(v):
    return "  вырожд." if v != v else f"{v:>8.4f}°"


def report_static(name, rows, gain, span=1.0, moving=False):
    """Шум тракта. gain — градусов на единицу скважности."""
    isr = unwrap([isr_deg(r) for r in rows])
    # Захват разворачивается В ШКАЛЕ СКВАЖНОСТИ (период 1.0), и только потом
    # переводится в градусы. Разворот по 360 был бы разворотом в чужой шкале:
    # период захвата равен наклону моста, а не 360.
    cap = [f * gain for f in unwrap([cap_frac(r) for r in rows], period=span)]
    ts = [float(r["t_host"]) for r in rows]
    kind = "вал едет, ход снят" if moving else "вал стоит"
    if moving:
        # ХОД СНИМАЕТСЯ ВНУТРИ УЧАСТКОВ БЕЗ ШВА, а не сквозь них. Разворот
        # через шов опирается на измеренный размах скважности, и его ошибка
        # даёт СТУПЕНЬКУ, которую снятие прямой размазывает по всему отрезку
        # как шум. На подложке это завышало шум захвата вдвое.
        raw = [cap_frac(r) for r in rows]
        runs = split_runs(raw, raw, minlen=20) or [list(range(len(rows)))]
        isr_o, cap_o = [], []
        for r in runs:
            isr_o += detrend(unwrap([isr[i] for i in r]), [ts[i] for i in r])
            cap_o += detrend([raw[i] * gain for i in r], [ts[i] for i in r])
        isr, cap = isr_o, cap_o
        rows = [rows[i] for r in runs for i in r]
    print(f"\n  --- {name}: {len(rows)} проб, {kind} ---")
    hdr = f"    {'тракт':<24}{'СКО':>10}{'робастно':>11}{'уровней':>9}{'интервал':>11}"
    print(hdr)
    for tag, xs in (("micros(), сырой", isr), ("аппаратный захват", cap)):
        print(f"    {tag:<24}{sd(xs):>9.4f}°{fmt(mad_sd(xs)):>11}"
              f"{len(set(round(x, 6) for x in xs)):>9}"
              f"{fmt(min_level_gap(xs)):>11}")
    r_isr, r_cap = mad_sd(isr), mad_sd(cap)
    if r_cap == r_cap and r_isr == r_isr and r_cap > 0:
        print(f"    выигрыш робастно: {r_isr / r_cap:>6.1f}x     "
              f"по СКО: {sd(isr) / sd(cap):.1f}x")
    elif sd(cap) > 0 and sd(isr) > 0:
        print(f"    выигрыш по СКО: {sd(isr) / sd(cap):.1f}x"
              f"   (робастная оценка вырождена — сравнивать по СКО)")
    else:
        # Нулевое СКО — не «идеальный тракт», а замерший: значение не
        # меняется вовсе. Печатать бесконечный выигрыш было бы враньём.
        who = "захвата" if sd(cap) == 0 else "micros()"
        print(f"    ТРАКТ {who} НЕ МЕНЯЕТСЯ ВОВСЕ: СКО ровно ноль."
              f" Это признак замершего тракта, а не отсутствия шума.")
    if moving:
        # Разность трактов: общий ход вала сокращается целиком, остаётся
        # сумма шумов. Тракт захвата тише на порядок, поэтому разность
        # практически равна шуму боевого тракта.
        d = [c - i for c, i in zip(cap, isr)]
        print(f"    разность трактов: СКО {sd(d):.4f}°"
              f"  <- ход вала сократился, остались шумы")
    return dict(name=name, n=len(rows), sd_isr=sd(isr), sd_cap=sd(cap),
                mad_isr=r_isr, mad_cap=r_cap)


def report_flags(rows):
    c = defaultdict(int)
    for r in rows:
        fl = int(r["flags"])
        for bit, nm in DF.items():
            if fl & bit:
                c[nm] += 1
    n = len(rows)
    print(f"\n  признаки негодности на {n} пакетов:")
    if not c:
        print("    чисто: ни одного")
    for nm, k in sorted(c.items()):
        print(f"    {nm:<12}{k:>7}  ({100.0 * k / n:5.2f}%)")
    return dict(c)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--gain", type=float, default=360.0,
                    help="градусов на единицу скважности; по умолчанию наивные 360")
    a = ap.parse_args()

    rows = load(a.csv)
    if not rows:
        sys.exit("пустой файл")
    print(f"файл: {a.csv}, строк {len(rows)}")

    # Годными считаем только те, где ОБА тракта дали годный отсчёт: иначе
    # тракты сравнивались бы на разных подмножествах.
    good = [r for r in rows if int(r["flags"]) & 0b1011 == 0]
    print(f"годных (оба тракта): {len(good)}")
    report_flags(rows)

    per = defaultdict(list)
    for r in good:
        per[r["label"]].append(r)

    # Период кадра — независимая проверка тактовой и константы кадра.
    ps = [float(r["cap_period"]) for r in good]
    if ps:
        mu = sum(ps) / len(ps)
        print(f"\n  период кадра захвата: {mu / TIM2_HZ * 1e6:.2f} мкс "
              f"({mu:.0f} тиков), ожидалось ~921 мкс")
        print(f"  квант захвата (1 тик TIM2): {a.gain / mu:.5f}°"
              f"   квант micros(): {QUANT_ISR:.5f}°"
              f"   отношение {QUANT_ISR / (a.gain / mu):.0f}x")

    # ---- МОСТ МЕЖДУ ШКАЛАМИ: только по отрезкам с ходом --------------------
    gain, span = a.gain, 1.0
    moving = [r for r in good if r["mode"] == "spin"]
    if len(moving) > 50:
        frac_raw = [cap_frac(r) for r in moving]
        deg_raw = [isr_deg(r) for r in moving]
        fit = fit_bridge(frac_raw, deg_raw)
        # РАЗМАХ СКВАЖНОСТИ МЕРЯЕТСЯ, А НЕ ПРИНИМАЕТСЯ ЗА ЕДИНИЦУ. Часть кадра
        # служебная, скважность всю единицу не пробегает, и разворот по 1.0
        # оставлял бы на каждом шве разрыв величиной с мёртвый участок.
        # РАЗМАХ МЕРЯЕТСЯ ПО ВЕЛИЧИНЕ СКАЧКА НА ШВЕ, а не по краям выборки.
        # На шве скважность падает с верхней границы диапазона на нижнюю —
        # то есть скачок РАВЕН размаху, прямо и без предположений. Оценка по
        # процентилям занижала его на всё, что не покрыл ход вала, и сверка
        # «наклон x размах = 360» не сходилась на 5.5°.
        jumps = [abs(b - a) for a, b in zip(frac_raw, frac_raw[1:])
                 if abs(b - a) > 0.5]
        ss = sorted(frac_raw)
        lo, hi = ss[0], ss[-1]
        if jumps:
            span = sorted(jumps)[len(jumps) // 2]
        else:
            # Швов не было — ход не покрыл оборота. Тогда размах по краям, и
            # это ОЦЕНКА СНИЗУ: непокрытая часть диапазона в неё не вошла.
            span = hi - lo
            print("\n  ВНИМАНИЕ: швов кадра в ходе нет, размах скважности —"
                  " оценка снизу по краям выборки")
        if fit:
            k, b, resid, nruns, spread = fit
            print(f"\n  МОСТ МЕЖДУ ШКАЛАМИ ({len(moving)} проб вращения, "
                  f"{nruns} участков без шва кадра)")
            print(f"    наклон:  {k:9.3f} град на единицу скважности"
                  f"   (наивно ожидалось 360)")
            print(f"    смещение:{b:9.3f} град")
            print(f"    разброс наклона по участкам: {spread:.3f}"
                  f"  <- велик => мост не прямая")
            print(f"    СКО остатка: {resid:.4f}°  <- рассогласование трактов")
            print(f"    отличие наклона от наивного: {(k / 360.0 - 1) * 100:+.2f}%")
            print(f"    размах скважности: {span:.4f}"
                  f"   (края выборки {lo:.4f}..{hi:.4f})"
                  f"   (мёртвый участок кадра {100 * (1 - span):.1f}%)")
            # НЕЗАВИСИМАЯ СВЕРКА: наклон и размах измерены по-разному —
            # первый подгонкой к боевому тракту, второй просто краями. Их
            # произведение обязано дать полный оборот.
            print(f"    сверка: наклон x размах = {k * span:.2f}°"
                  f"  (обязано быть 360°, расхождение {k * span - 360:+.2f}°)")
            gain = k
        else:
            print("\n  МОСТ НЕ ПОДОГНАЛСЯ: участков без шва кадра не нашлось.")
    else:
        print("\n  МОСТ НЕ ИЗМЕРЕН: отрезков с вращением в логе нет.")
        print("  Наклон взят наивным (360 град на скважность) — это ДОПУЩЕНИЕ,")
        print("  и шум захвата в градусах ниже настолько же условен.")

    print(f"\n  === шум по отрезкам (наклон {gain:.1f} град/скважность) ===")
    for label in per:
        if per[label]:
            report_static(label, per[label], gain, span,
                          moving=(per[label][0]["mode"] == "spin"))


if __name__ == "__main__":
    main()
