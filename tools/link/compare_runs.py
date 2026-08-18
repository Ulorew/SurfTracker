#!/usr/bin/env python3
"""Сличение двух прогонов по одной мерке. Готовится ЗАРАНЕЕ, до прогонов.

Так сделано намеренно: мерку, придуманную после того, как числа увидены,
слишком легко подобрать под желаемый вывод. Сегодня это уже случилось —
«синхронизация подтверждена» сравнивала ходьбу без неё со стоянием с ней, то
есть разные условия, и вывод был неверен.

  compare_runs.py <папка1> <папка2>
"""
import csv, math, sys, json, os


def load(d):
    p = os.path.join(d, 'log.csv')
    if not os.path.exists(p):
        p = os.path.join(d, 'лог.csv')
    rows = list(csv.DictReader(open(p)))
    j = os.path.join(d, 'run.json')
    if not os.path.exists(j):
        j = os.path.join(d, 'прогон.json')
    return rows, json.load(open(j))


def f(r, k):
    try:
        return float(r[k])
    except Exception:
        return float('nan')


def stats(d):
    rows, cfg = load(d)
    t = [f(r, 't_ms') / 1000 for r in rows]
    e = [f(r, 'ошибка_град') for r in rows]
    th = [math.degrees(f(r, 'θ_enc')) for r in rows]
    hit = [r.get('есть_цель') == '1' for r in rows]
    idx = [i for i in range(len(rows)) if hit[i] and not math.isnan(e[i])]
    if not idx:
        return None
    # УСТАНОВИВШИЙСЯ режим: первые 10 с — наведение, оно про другое
    st = [i for i in idx if t[i] > 10.0]
    if len(st) < 20:
        st = idx
    a = sorted(abs(e[i]) for i in st)
    thv = [th[i] for i in st if not math.isnan(th[i])]
    cross = 0
    for k in range(1, len(st)):
        i, j = st[k - 1], st[k]
        if e[i] and e[j] and (e[i] < 0) != (e[j] < 0):
            cross += 1
    dur = t[st[-1]] - t[st[0]]
    return {
        'имя': os.path.basename(d.rstrip('/')),
        'sync': cfg.get('синхронизация'),
        'тактов': len(st),
        'длительность_с': round(dur, 1),
        '|ошибка| медиана': round(a[len(a) // 2], 2),
        '|ошибка| p90': round(a[int(0.9 * (len(a) - 1))], 2),
        '|ошибка| макс': round(a[-1], 2),
        'размах вала': round(max(thv) - min(thv), 1) if thv else None,
        'переходов через ноль в мин': round(cross / max(dur, 1e-9) * 60, 1),
    }


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    a, b = stats(sys.argv[1]), stats(sys.argv[2])
    keys = list(a.keys())
    w = max(len(k) for k in keys)
    print(f"{'':<{w}}  {'прогон 1':>22}  {'прогон 2':>22}")
    for k in keys:
        print(f'{k:<{w}}  {str(a[k]):>22}  {str(b[k]):>22}')
    print()
    print('Сравнивать можно ТОЛЬКО прогоны одного режима: стоя со стоя,')
    print('ходьбу с ходьбой. Разные условия уже давали неверный вывод.')
