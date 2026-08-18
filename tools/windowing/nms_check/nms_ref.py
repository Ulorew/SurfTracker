#!/usr/bin/env python3
"""Независимый эталон отбора кандидатов + проверка свойств.

Эталон написан по ПРАВИЛАМ, а не переписан с Java: порог, буфер на max*8
слотов с вытеснением слабейшего, устойчивая сортировка по убыванию
уверенности, жадное подавление по IoU, размер = наибольшая сторона.

Отдельно считаются СВОЙСТВА выхода — они не зависят ни от какой реализации и
ловят то, что эталон и перенос могли бы нарушить вместе: уверенности не
возрастают, попарный IoU не выше порога, размер равен большей стороне, все
уверенности не ниже порога, число не больше max.
"""
import sys, os, struct
from decimal import Decimal, ROUND_HALF_UP

# ПОРОГ В ТОЧНОСТИ float32. Java сравнивает float с float, а питон читает те
# же числа как double: 0.08 в float32 равно 0.07999999821, и наивное сравнение
# с двойным 0.08 отвергало кандидата, который на телефоне проходит. Ровно
# пороговый случай at_thresh это и поймал.
LOW_CONF = struct.unpack('f', struct.pack('f', 0.08))[0]
IOU = 0.45

# Порчи ЭТАЛОНА: доказывают, что набор случаев доходит до каждого правила.
# Без них «совпало» означало бы лишь, что два кода одинаково молчат.
MUT = os.environ.get('NMS_CHECK_MUTATE', '')


def f32(x):
    return struct.unpack('f', struct.pack('f', x))[0]


def fmt6(x):
    """Печать как в Java: HALF_UP, а не половинное-к-чётному.

    Без этого сличение спотыкалось на точных половинах: 301.1640625 питон
    печатал как 301.164062, Java — как 301.164063. Расхождение форматирования
    не имеет отношения к правилам отбора и маскировало бы настоящие.
    """
    return str(Decimal(repr(f32(x))).quantize(Decimal('0.000001'),
                                              rounding=ROUND_HALF_UP))


def iou(a, b):
    ax0, ax1 = a[0] - a[2] / 2, a[0] + a[2] / 2
    ay0, ay1 = a[1] - a[3] / 2, a[1] + a[3] / 2
    bx0, bx1 = b[0] - b[2] / 2, b[0] + b[2] / 2
    by0, by1 = b[1] - b[3] / 2, b[1] + b[3] / 2
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    uni = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return 0.0 if uni <= 0 else inter / uni


def nms(cols, max_det):
    low = 0.0 if MUT == 'thresh' else LOW_CONF
    thr = 0.95 if MUT == 'iou' else IOU
    cap = max_det * (1 if MUT == 'capacity' else 8)

    slots = []
    for c in cols:
        if c[4] < low:
            continue
        if len(slots) < cap:
            slots.append(list(c))
            continue
        if MUT == 'truncate':          # обрыв прохода вместо вытеснения
            break
        weak = min(range(len(slots)), key=lambda i: (slots[i][4], i))
        if slots[weak][4] >= c[4]:
            continue
        slots[weak] = list(c)

    order = sorted(range(len(slots)),
                   key=lambda i: (slots[i][4] if MUT != 'sortdir' else -slots[i][4]),
                   reverse=True)

    out, dead = [], set()
    for pos, i in enumerate(order):
        if i in dead or len(out) >= max_det:
            continue
        b = slots[i]
        size = max(b[2], b[3]) if MUT != 'size' else min(b[2], b[3])
        out.append((b[0], b[1], size, b[2], b[3], b[4]))
        for j in order[pos + 1:]:
            if j not in dead and iou(b, slots[j]) > thr:
                dead.add(j)
    return out


def read_cases(path):
    with open(path) as f:
        lines = [l.rstrip('\n') for l in f]
    i, cases = 0, []
    while i < len(lines):
        if not lines[i].strip():
            i += 1; continue
        _, name, n, mx = lines[i].split()
        n, mx = int(n), int(mx)
        rows = [[float(x) for x in lines[i + 1 + r].split()] if n else []
                for r in range(5)]
        cases.append((name, [[rows[r][a] for r in range(5)] for a in range(n)], mx))
        i += 6
    return cases


def properties(name, out, max_det, bad):
    if len(out) > max_det:
        bad.append(f'{name}: выдано {len(out)} при пределе {max_det}')
    for k in range(1, len(out)):
        if out[k][5] > out[k - 1][5] + 1e-9:
            bad.append(f'{name}: уверенность возрастает на {k}')
            break
    for d in out:
        if d[5] < LOW_CONF - 1e-9:
            bad.append(f'{name}: уверенность {d[5]:.4f} ниже порога'); break
        if abs(d[2] - max(d[3], d[4])) > 1e-6:
            bad.append(f'{name}: размер {d[2]:.4f} != max({d[3]:.4f},{d[4]:.4f})'); break
    for x in range(len(out)):
        for y in range(x + 1, len(out)):
            a = (out[x][0], out[x][1], out[x][3], out[x][4])
            b = (out[y][0], out[y][1], out[y][3], out[y][4])
            if iou(a, b) > IOU + 1e-9:
                bad.append(f'{name}: выжившие {x} и {y} перекрыты, IoU {iou(a,b):.3f}')
                return


def main():
    cases = read_cases(sys.argv[1])
    with open(sys.argv[2], 'w') as w:
        w.write('case,idx,cx,cy,size,bw,bh,conf\n')
        bad = []
        for name, cols, mx in cases:
            out = nms(cols, mx)
            if not MUT:
                properties(name, out, mx, bad)
            if not out:
                w.write(f'{name},-1,,,,,,\n')
            for k, d in enumerate(out):
                w.write(f'{name},{k},' + ','.join(fmt6(v) for v in d) + '\n')
    if bad and not MUT:
        print('СВОЙСТВА НАРУШЕНЫ эталоном (чинить эталон, не перенос):')
        print('\n'.join(bad[:10]))
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
