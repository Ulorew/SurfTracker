#!/usr/bin/env python3
"""Сличение двух прогонов по ВЫБОРУ, а не по числам фильтра.

Порядок важности столбцов задан не вкусом: индекс выбранной детекции решает,
на кого поедет камера; состояние решает, ищем мы или ведём; сторона окна
решает, что вообще увидит модель. Координаты предсказания сравниваются
последними и с допуском — расхождение в них при одинаковом выборе на всех
тактах поведения не меняет.
"""
import sys, csv

TOL_PX = 0.001       # допуск, пиксели. НЕ 1.0: контроль без порчи даёт ровный
                     # ноль во всех клетках матрицы, а обнуление блоков Q двигает
                     # предсказание на 0.0154 px — допуск в тысячу раз выше уровня
                     # шума выбрасывал три порчи Калмана и всё прочее того же масштаба.


def load(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def main():
    a = load(sys.argv[1])   # питон, эталон
    b = load(sys.argv[2])   # телефонный перенос
    quiet = '--quiet' in sys.argv
    names = '--names' in sys.argv   # только имена разошедшихся сценариев

    if len(a) != len(b):
        print(f'РАЗНОЕ ЧИСЛО ТАКТОВ: питон {len(a)}, телефон {len(b)}')
        return 2

    n = len(a)
    bad_choice = bad_status = bad_miss = 0
    bad_side = bad_pred = 0
    first = []
    per_scen = {}

    for ra, rb in zip(a, b):
        assert ra['scenario'] == rb['scenario'] and ra['tick'] == rb['tick'], \
            'строки разъехались — сравнение бессмысленно'
        s = ra['scenario']
        per_scen.setdefault(s, {'n': 0, 'ch': 0, 'st': 0})
        per_scen[s]['n'] += 1
        why = []
        if ra['chosen'] != rb['chosen']:
            bad_choice += 1; per_scen[s]['ch'] += 1
            why.append(f"выбор {ra['chosen']}!={rb['chosen']}")
        if ra['status'] != rb['status']:
            bad_status += 1; per_scen[s]['st'] += 1
            why.append(f"состояние {ra['status']}!={rb['status']}")
        if ra['miss'] != rb['miss']:
            bad_miss += 1; per_scen[s]['ot'] = per_scen[s].get('ot', 0) + 1; why.append(f"промахов {ra['miss']}!={rb['miss']}")
        if abs(float(ra['side']) - float(rb['side'])) > TOL_PX:
            bad_side += 1; per_scen[s]['ot'] = per_scen[s].get('ot', 0) + 1
            why.append(f"окно {float(ra['side']):.1f}!={float(rb['side']):.1f}")
        dx = abs(float(ra['pred_cx']) - float(rb['pred_cx']))
        dy = abs(float(ra['pred_cy']) - float(rb['pred_cy']))
        if max(dx, dy) > TOL_PX:
            bad_pred += 1; per_scen[s]['ot'] = per_scen[s].get('ot', 0) + 1
            why.append(f"центр {dx:.1f}/{dy:.1f} px")
        if why and len(first) < 12:
            first.append(f"  {s} такт {ra['tick']}: " + '; '.join(why))

    if names:
        bad = [s for s, d in per_scen.items() if d['ch'] or d['st'] or d.get('ot')]
        print(','.join(bad) if bad else '-')
        return 0 if not bad else 1

    if not quiet:
        print(f'тактов сличено: {n}')
        print(f'  выбор детекции : {bad_choice} расхождений')
        print(f'  состояние      : {bad_status}')
        print(f'  счётчик промахов: {bad_miss}')
        print(f'  сторона окна   : {bad_side}')
        print(f'  центр плана    : {bad_pred}  (допуск {TOL_PX} px)')
        for s, d in per_scen.items():
            mark = 'ok' if d['ch'] == 0 and d['st'] == 0 else 'РАСХОЖДЕНИЕ'
            print(f'    {s:<12} {d["n"]:>3} тактов  выбор {d["ch"]}  состояние {d["st"]}  {mark}')
        if first:
            print('первые расхождения:')
            print('\n'.join(first))

    critical = bad_choice + bad_status + bad_miss + bad_side
    return 0 if critical == 0 and bad_pred == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
