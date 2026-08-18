#!/usr/bin/env python3
"""Разбор толчков: восстановление после машинного возмущения.

Написан ДО прогона. Мерка та же, что для переходов, но возмущение одинаково по
построению, а режим синхронизации чередуется ВНУТРИ прогона — значит сравнение
не зависит ни от скорости человека, ни от освещения, ни от нагрева.

  kicks.py <папка>
"""
import csv, math, sys, os


def main(d):
    p = os.path.join(d, 'log.csv')
    rows = list(csv.DictReader(open(p)))
    if 'толчок_фаза' not in rows[0]:
        print('В логе нет колонок толчка — прогон снят старой сборкой.')
        return

    def f(r, k):
        try:
            return float(r[k])
        except Exception:
            return float('nan')

    t = [f(r, 't_ms') / 1000 for r in rows]
    e = [f(r, 'ошибка_град') for r in rows]
    th = [math.degrees(f(r, 'θ_enc')) for r in rows]
    inK = [r['в_толчке'] == '1' for r in rows]
    sync = [r['sync_такта'] == '1' for r in rows]
    ph = [int(f(r, 'толчок_фаза')) if r['толчок_фаза'] else -1 for r in rows]

    # ВОССТАНОВЛЕНИЕ: от конца толчка до следующего толчка
    events = []
    for i in range(1, len(rows)):
        if inK[i - 1] and not inK[i]:
            seg = [j for j in range(i, len(rows)) if not inK[j] and t[j] - t[i] < 8]
            if len(seg) < 8:
                continue
            peak = max(abs(e[j]) for j in seg if not math.isnan(e[j]))
            thv = [th[j] for j in seg if not math.isnan(th[j])]
            settle = float('nan')
            for k, j in enumerate(seg):
                if all(abs(e[m]) < 1.0 for m in seg[k:k + 4] if not math.isnan(e[m])):
                    settle = t[j] - t[i]
                    break
            cross = sum(1 for k in range(1, len(seg))
                        if e[seg[k - 1]] and e[seg[k]]
                        and (e[seg[k - 1]] < 0) != (e[seg[k]] < 0))
            events.append({'t': t[i], 'sync': sync[i], 'пик': peak,
                           'размах': max(thv) - min(thv) if thv else float('nan'),
                           'успокоение': settle, 'перемен': cross})

    if not events:
        print('Толчков в логе нет.')
        return
    print(f"{'t,с':>6} {'режим':>10} {'пик,°':>7} {'размах,°':>9} {'успок,с':>8} {'перемен':>8}")
    for x in events:
        print(f"{x['t']:>6.1f} {'с sync' if x['sync'] else 'без sync':>10} "
              f"{x['пик']:>7.1f} {x['размах']:>9.1f} {x['успокоение']:>8.1f} {x['перемен']:>8}")

    def med(v):
        v = sorted(z for z in v if not math.isnan(z))
        return v[len(v) // 2] if v else float('nan')

    print()
    for flag, name in ((True, 'с синхронизацией'), (False, 'без синхронизации')):
        g = [x for x in events if x['sync'] == flag]
        if not g:
            continue
        print(f'{name:>20}: толчков {len(g)}, пик {med([x["пик"] for x in g]):.1f}°, '
              f'успокоение {med([x["успокоение"] for x in g]):.1f} с, '
              f'перемен знака {med([x["перемен"] for x in g]):.0f}')
    print('\nВозмущение одинаково по построению, режим чередуется внутри прогона.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '.')
