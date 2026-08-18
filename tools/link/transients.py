#!/usr/bin/env python3
"""Переходы «шёл — встал»: перерегулирование и время успокоения.

Мерка написана ДО прогона с доделанной правкой. Считается не средняя ошибка
по прогону (она меряет установившийся режим, где механизм выключен), а именно
ПЕРЕХОД: человек остановился, вал ещё идёт.

Остановка ищется по данным, а не по расписанию суфлёра: реплика звучит, а
человек встаёт с задержкой в секунду-полторы.

  transients.py <папка> [...]
"""
import csv, math, sys, os


def load(d):
    p = os.path.join(d, 'log.csv')
    if not os.path.exists(p):
        p = os.path.join(d, 'лог.csv')
    return list(csv.DictReader(open(p)))


def f(r, k):
    try:
        return float(r[k])
    except Exception:
        return float('nan')


def analyse(d):
    rows = load(d)
    t = [f(r, 't_ms') / 1000 for r in rows]
    e = [f(r, 'ошибка_град') for r in rows]
    th = [math.degrees(f(r, 'θ_enc')) for r in rows]
    hit = [r.get('есть_цель') == '1' for r in rows]
    SIGN = -1
    # угол цели в мире: он и показывает, идёт человек или стоит
    A = [th[i] + SIGN * e[i] if hit[i] else float('nan') for i in range(len(rows))]

    def rate(i, w=3):
        a, b = max(0, i - w), min(len(rows) - 1, i + w)
        if math.isnan(A[a]) or math.isnan(A[b]) or t[b] <= t[a]:
            return float('nan')
        return (A[b] - A[a]) / (t[b] - t[a])

    v = [abs(rate(i)) for i in range(len(rows))]
    # ОСТАНОВКА: скорость цели упала ниже 3 град/с и держится 2 с,
    # а до этого была выше 8 град/с
    stops = []
    for i in range(6, len(rows) - 12):
        if math.isnan(v[i]):
            continue
        before = [v[j] for j in range(i - 6, i) if not math.isnan(v[j])]
        after = [v[j] for j in range(i, i + 10) if not math.isnan(v[j])]
        if not before or len(after) < 8:
            continue
        if max(before) > 5 and max(after) < 3.5:
            if not stops or t[i] - t[stops[-1]] > 5:
                stops.append(i)

    print(f'--- {os.path.basename(d.rstrip("/"))}: остановок найдено {len(stops)}')
    if not stops:
        print('    переходов нет — сравнивать нечего')
        return
    for i in stops:
        seg = [j for j in range(i, len(rows)) if t[j] - t[i] <= 6 and hit[j]]
        if len(seg) < 5:
            continue
        peak = max(abs(e[j]) for j in seg)
        thv = [th[j] for j in seg if not math.isnan(th[j])]
        swing = max(thv) - min(thv) if thv else float('nan')
        # время успокоения: когда |ошибка| стабильно ниже 1°
        settle = float('nan')
        for k, j in enumerate(seg):
            if all(abs(e[m]) < 1.0 for m in seg[k:k + 4]):
                settle = t[j] - t[i]
                break
        # перемены знака ошибки после остановки = качание
        cross = sum(1 for k in range(1, len(seg))
                    if e[seg[k - 1]] and e[seg[k]]
                    and (e[seg[k - 1]] < 0) != (e[seg[k]] < 0))
        print(f'    t={t[i]:5.1f} с  пик ошибки {peak:5.1f}°  '
              f'размах вала {swing:5.1f}°  успокоение {settle:4.1f} с  '
              f'перемен знака {cross}')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for d in sys.argv[1:]:
        analyse(d)
