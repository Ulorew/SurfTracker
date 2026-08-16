#!/usr/bin/env python3
"""Гоняет НАСТОЯЩИЙ TrackState по тем же сценариям и печатает те же столбцы.

Единицы. TrackState живёт в углах, телефон — в пикселях. Перевод здесь
линейный: вычесть центр кадра. Он ТОЧЕН, потому что вся логика уровня 1
масштабно-инвариантна — приём меряется долей стороны окна, окно кратно
размеру цели, экстраполяция линейна. Ни одна константа не задана в
абсолютных углах. Единственное, что задано абсолютно, — пол окна 640, и он
передаётся в тех же единицах, что и координаты.

Конфигурация: строго уровень 1 (альфа-бета, выбор по расстоянию), всё, что
не перенесено на телефон, выключено явно. Сличать телефон с включённым
Калманом значило бы мерить не перенос, а разницу поколений.

Оговорка про теневые треки: здесь они выключены НЕ потому, что «ещё не
перенесены». Они выключены в самом проде (tracking-v2, коммит 3304aed) —
решение владельца после пересчёта честной линейкой. В перенос они не идут
вовсе, поэтому эта строка не временная.
"""
import sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import tracking_config as tcfg
import track_logic as tl


def set_level1():
    tcfg.ENABLE_SIZE_SCORING = False
    tcfg.ENABLE_OCCLUSION_HOLD = False
    tcfg.ENABLE_VELOCITY_GATE = False
    tcfg.ENABLE_VELOCITY_DIRECTION = False
    tcfg.ENABLE_SHADOW_TRACKS = False
    tcfg.ENABLE_MAHALANOBIS_GATE = False
    tcfg.SCORE_FORM = 'distance'
    tcfg.FILTER_LEVEL = 1
    tcfg.VIEW_CLAMP_KEEPS_WINDOW_INSIDE = True
    tcfg.TRACK_WINDOW_K = 3.5
    tcfg.SIZE_FILTER_GROW_RATE = 0.5
    tcfg.SIZE_FILTER_SHRINK_RATE = 0.1
    tcfg.TARGET_SELECT_MAX_DIST_FRAC = 0.30
    tcfg.REACQUIRE_MAX_DIST_FRAC = 0.30
    tcfg.EXTRAPOLATION_TAU_SEC = 1.5
    tcfg.WINDOW_EXPAND_PER_MISS = 1.15
    tcfg.MISS_TO_LOST_N = 5
    tcfg.ALPHA_BETA_ALPHA = 0.6
    tcfg.ALPHA_BETA_BETA = 0.3


# Порча эталона — проверка РАЗЛИЧАЮЩЕЙ СИЛЫ самого стенда.
#
# Стенд, который не ловит подмену константы, объявил бы согласие и при
# настоящем расхождении. Ровно это уже случилось: прошлая проверка переноса
# была слепа (сравнивала числа фильтра без состояния и без выбора) и
# отрапортовала «сошёлся». Поэтому каждая порча обязана быть поймана, и
# запуск без этой проверки считается несостоявшимся.
MUTATIONS = {
    'frac':   ('TARGET_SELECT_MAX_DIST_FRAC', 0.45),
    'k':      ('TRACK_WINDOW_K', 3.0),
    'expand': ('WINDOW_EXPAND_PER_MISS', 1.30),
    'miss':   ('MISS_TO_LOST_N', 3),
    'shrink': ('SIZE_FILTER_SHRINK_RATE', 0.5),
    'alpha':  ('ALPHA_BETA_ALPHA', 0.9),
    'reacq':  ('REACQUIRE_MAX_DIST_FRAC', 0.15),
}


def apply_mutation(name):
    attr, val = MUTATIONS[name]
    setattr(tcfg, attr, val)
    sys.stderr.write(f'ПОРЧА: {attr} = {val}\n')


def read_scenarios(path):
    frame = None
    scens = []
    with open(path) as f:
        lines = [l.rstrip('\n') for l in f if l.strip()]
    i = 0
    while i < len(lines):
        p = lines[i].split()
        if p[0] == 'FRAME':
            frame = (int(p[1]), int(p[2]), float(p[3]), int(p[4]))
            i += 1
        elif p[0] == 'SCENARIO':
            name, n = p[1], int(p[2])
            ticks = []
            for k in range(n):
                q = lines[i + 1 + k].split()
                m = int(q[0])
                ticks.append([(float(q[1 + j * 3]), float(q[2 + j * 3]),
                               float(q[3 + j * 3])) for j in range(m)])
            scens.append((name, ticks))
            i += 1 + n
        else:
            raise ValueError('неожиданная строка: ' + lines[i])
    return frame, scens


def main():
    set_level1()
    mut = os.environ.get('PORT_CHECK_MUTATE')
    if mut:
        apply_mutation(mut)
    if len(sys.argv) > 3:
        tcfg.VIEW_CLAMP_KEEPS_WINDOW_INSIDE = (sys.argv[3] == 'true')
    frame, scens = read_scenarios(sys.argv[1])
    W, H, dt, min_win = frame
    hw, hh = W / 2.0, H / 2.0
    max_win = min(W, H)

    out = open(sys.argv[2], 'w')
    out.write('scenario,tick,chosen,status,miss,side,pred_cx,pred_cy\n')

    for name, ticks in scens:
        st = None
        for ti, dets in enumerate(ticks):
            # детекции -> угловой формат (x0,y0,x1,y1,conf), центр кадра в нуле
            ang = []
            for (cx, cy, s) in dets:
                ang.append((cx - hw - s / 2, cy - hh - s / 2,
                            cx - hw + s / 2, cy - hh + s / 2, 0.9))
            if st is None:
                if not dets:
                    out.write(f'{name},{ti},-1,-,0,0.0000,0.0000,0.0000\n')
                    continue
                cx, cy, s = dets[0]
                st = tl.TrackState(tcfg, cx - hw, cy - hh, s,
                                   min_window=min_win, max_window=max_win,
                                   view_half_w=hw, view_half_h=hh)
                out.write(f'{name},{ti},0,T,0,{st.current_window_side():.4f},'
                          f'{st.filter.cx + hw:.4f},{st.filter.cy + hh:.4f}\n')
                continue
            r = st.step(dt, ang)
            ch = -1
            if r.chosen is not None:
                ch = next(i for i, a in enumerate(ang) if a is r.chosen)
            sts = 'T' if r.status == tl.STATUS_TRACKING else 'L'
            out.write(f'{name},{ti},{ch},{sts},{r.miss_count},'
                      f'{r.window_side:.4f},{r.predicted_cx + hw:.4f},'
                      f'{r.predicted_cy + hh:.4f}\n')
    out.close()


if __name__ == '__main__':
    main()
