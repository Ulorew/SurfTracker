#!/usr/bin/env python3
"""Разбор толчков: восстановление после машинного возмущения.

Написан ДО прогона. Мерка та же, что для переходов, но возмущение одинаково по
построению, а режим синхронизации чередуется ВНУТРИ прогона — значит сравнение
не зависит ни от скорости человека, ни от освещения, ни от нагрева.

  kicks.py <папка>
"""
import csv, math, sys, os, json


def main(d):
    p = os.path.join(d, 'log.csv')
    rows = list(csv.DictReader(open(p)))
    cfg = json.load(open(os.path.join(d, 'run.json')))
    # Сколько градусов даёт САМ толчок: больше этого вал уехать не мог, значит
    # разбег в 30-40° — это движение цели, а не восстановление петли.
    kick_deg = math.degrees(cfg.get('толчок', 0.35)) * cfg.get('толчок_мс', 500) / 1000.0
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
            # ГОДЕН ли толчок: цель должна СТОЯТЬ до него. Иначе меряется
            # движение человека, а не восстановление петли. На прогоне
            # 260818_1026 суфлёр объявил конец за 15 с до настоящего, человек
            # ушёл, и три последних толчка дали размах вала 30-40° вместо 4-9°
            # — а сводка посчитала их наравне с годными.
            pre = [j for j in range(max(0, i - 30), i) if not inK[j]]
            A = []
            for j in pre:
                if not math.isnan(e[j]) and not math.isnan(th[j]):
                    A.append((t[j], th[j] - e[j]))
            moving = False
            if len(A) >= 4:
                sp = max(abs((A[k][1] - A[k - 1][1]) / max(A[k][0] - A[k - 1][0], 1e-6))
                         for k in range(1, len(A)))
                moving = sp > 12
            thv0 = [th[j] for j in seg if not math.isnan(th[j])]
            swing = (max(thv0) - min(thv0)) if thv0 else float('nan')
            # Размах больше 2.5 толчков — цель ушла, петля гналась за ней.
            if not math.isnan(swing) and kick_deg > 0 and swing > 2.5 * kick_deg:
                moving = True
            events.append({'t': t[i], 'sync': sync[i], 'пик': peak, 'годен': not moving,
                           'размах': max(thv) - min(thv) if thv else float('nan'),
                           'успокоение': settle, 'перемен': cross})

    if not events:
        print('Толчков в логе нет.')
        return
    print(f"{'t,с':>6} {'режим':>10} {'пик,°':>7} {'размах,°':>9} {'успок,с':>8} "
          f"{'перемен':>8} {'годен':>7}")
    for x in events:
        print(f"{x['t']:>6.1f} {'с sync' if x['sync'] else 'без sync':>10} "
              f"{x['пик']:>7.1f} {x['размах']:>9.1f} {x['успокоение']:>8.1f} {x['перемен']:>8} "
              f"{'да' if x['годен'] else 'НЕТ':>7}")
    events = [x for x in events if x['годен']]

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
    print()
    # РАЗБРОС ВНУТРИ РЕЖИМА — против разницы между режимами. Без этого
    # сравнение медиан выдаёт победителя там, где его нет.
    for flag, name in ((True, 'с синхронизацией'), (False, 'без синхронизации')):
        g = [x['пик'] for x in events if x['sync'] == flag]
        if len(g) >= 2:
            print(f'{name:>20}: пики {min(g):.1f}..{max(g):.1f}° (разброс {max(g)-min(g):.1f}°)')
    a = [x['пик'] for x in events if x['sync']]
    b = [x['пик'] for x in events if not x['sync']]
    if a and b:
        diff = abs(med(a) - med(b))
        spread = max([max(a) - min(a) if len(a) > 1 else 0,
                      max(b) - min(b) if len(b) > 1 else 0])
        print(f'\nразница медиан {diff:.1f}° против разброса внутри режима {spread:.1f}°')
        if diff < spread:
            print('ВЫВОД НЕ СЛЕДУЕТ: разброс внутри режима больше разницы между ними.')
            print(f'Толчков годных: с sync {len(a)}, без sync {len(b)} — нужно больше.')
        else:
            print('Разница превышает разброс — сравнение осмысленно.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '.')


# --- Разбор с проверкой значимости --------------------------------------
# Добавлено после прогона 260818_1033: сравнение медиан «на глаз» ничего не
# решает, когда разброс велик, а правило «разница меньше разброса — вывода
# нет» слишком грубое: при 14 наблюдениях на режим оно не заключит НИЧЕГО
# никогда. Перестановочный тест отвечает на нужный вопрос прямо: какова
# вероятность получить такую разницу случайно.
