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
    # Затухание экстраполяции: ловится ТОЛЬКО на сценарии, где цель теряется
    # НА ХОДУ. Пока оба сценария потери держали цель неподвижной перед
    # пропаданием, скорость альфа-беты была нулевой, и подмена τ (хоть 0.0,
    # хоть 100.0) не меняла ни одного такта.
    'tau':    ('EXTRAPOLATION_TAU_SEC', 0.2),
    # Механизм А: ловятся только когда он включён — на выключенном стенд
    # обязан их НЕ поймать, и это тоже проверяется (контроль ниже).
    'lam':    ('SIZE_LAMBDA', 2.0),
    'veto':   ('SIZE_VETO_RATIO', 1.2),
    # ШТРАФ ЕСТЬ ВООБЩЕ? Порча 'lam' (0.5 -> 2.0) отвечала только на вопрос
    # «тот ли коэффициент», и отвечала им же на переносе, где штрафа нет
    # вовсе. Ноль — единственная порча, которую невозможно пройти без
    # работающего штрафного слагаемого: контроль требует совпасть с λ=0.5, а
    # эта порча требует РАЗОЙТИСЬ с λ=0.0, и вместе они зажимают механизм.
    'lam0':   ('SIZE_LAMBDA', 0.0),
    # То же для вето: 'veto' лишь ужимает полосу, а эта порча снимает её
    # целиком — и ловится только сценарием, где вето кого-то отвергает.
    'veto0':  ('SIZE_VETO_RATIO', 1e9),
    # Уровень 2: ловятся только при включённом Калмане (а chi2 — при гейте).
    'chi2':   ('KALMAN_GATE_CHI2', 3.0),
    # Каждая из трёх правок гейта — своей порчей. Одной общей мало: правки
    # независимы, и «поймана» на общей не сказало бы, какая именно перенесена.
    'gpred':  ('MAHA_R_FROM_PREDICTED_SIZE', False),
    'grad':   ('KALMAN_GATE_ALSO_RADIUS', False),
    'gwarm':  ('KALMAN_GATE_MIN_UPDATES', 0),
    'rpos':   ('KALMAN_R_POS_SIZE_FRAC', 0.10),
    'rlogh':  ('KALMAN_R_LOGH', 0.05),
    'accel':  ('KALMAN_SIGMA_ACCEL_MPS2', 5.0 * (2.0 / 50.0) * 2600.0),
}


# Пол окна живёт не в конфиге, а в аргументе конструктора TrackState (обе
# стороны читают его из строки FRAME), поэтому порча идёт особым путём.
MIN_WINDOW_MUTATION = 900


def apply_mutation(name):
    # Составная порча: снять правило «только сужать» И прогрев разом. Нужна
    # ровно для одного — показать, что прогрев не мёртв, а подчинён.
    if name == 'grad_gwarm':
        tcfg.KALMAN_GATE_ALSO_RADIUS = False
        tcfg.KALMAN_GATE_MIN_UPDATES = 0
        sys.stderr.write('ПОРЧА: сужение снято И прогрев снят\n')
        return
    if name == 'minwin':
        sys.stderr.write(f'ПОРЧА: min_window = {MIN_WINDOW_MUTATION}\n')
        return
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
    if len(sys.argv) > 3:
        tcfg.VIEW_CLAMP_KEEPS_WINDOW_INSIDE = (sys.argv[3] == 'true')
    if len(sys.argv) > 4:
        tcfg.ENABLE_SIZE_SCORING = (sys.argv[4] == 'true')
    if len(sys.argv) > 5 and sys.argv[5] == 'true':
        # Уровень 2. Метрические константы офлайна приводятся к тем же
        # абсолютным единицам, в которых считает перенос: делители равны 1,
        # числители — уже пересчитанные величины. Иначе сравнивались бы два
        # разных фильтра (см. комментарий в PortDrive).
        PX_PER_RAD = 2600.0
        tcfg.FILTER_LEVEL = 2
        tcfg.KALMAN_SIGMA_ACCEL_MPS2 = (2.0 / 50.0) * PX_PER_RAD
        tcfg.KALMAN_REF_DISTANCE_M = 1.0
        tcfg.KALMAN_MAX_SPEED_MPS = (20.0 / 20.0) * PX_PER_RAD
        tcfg.KALMAN_MIN_DISTANCE_M = 1.0
        tcfg.KALMAN_SEED_SIZE_FALLBACK = 0.02 * PX_PER_RAD
        # СКОРОСТЬ log h БЕЗРАЗМЕРНА и с переводом в пиксели не
        # масштабируется, а офлайн выводит её как FRAC * v_max. Раз v_max
        # переведён в пиксели (2600), то FRAC приходится делить на тот же
        # множитель, иначе питон получает 780 1/с вместо 0.3 и следует за
        # размером мгновенно: сторона окна расходилась с переносом на 76 тактах
        # при полном совпадении выбора и состояния.
        tcfg.KALMAN_LOGH_RADIAL_FRAC = 0.30 / PX_PER_RAD
        tcfg.KALMAN_R_POS_SIZE_FRAC = 0.30
        tcfg.KALMAN_R_LOGH = 0.25
        tcfg.KALMAN_GATE_CHI2 = 9.21
        # Три правки гейта от 18.08 — явно, а не умолчанием: стенд обязан
        # сличать ту конфигурацию, которую называет, даже если конфиг сменят.
        tcfg.MAHA_R_FROM_PREDICTED_SIZE = True
        tcfg.KALMAN_GATE_ALSO_RADIUS = True
        tcfg.KALMAN_GATE_MIN_UPDATES = 5
        tcfg.KALMAN_ANISOTROPIC_Q = False
    if len(sys.argv) > 6:
        tcfg.ENABLE_MAHALANOBIS_GATE = (sys.argv[6] == 'true')
    # Порча — ПОСЛЕДНЕЙ. Пока она стояла до настройки уровня 2, блок констант
    # Калмана её молча затирал, и стенд «не ловил» подмены, которых сам же и
    # лишился.
    mut = os.environ.get('PORT_CHECK_MUTATE')
    if mut:
        apply_mutation(mut)
    frame, scens = read_scenarios(sys.argv[1])
    W, H, dt, min_win = frame
    if mut == 'minwin':
        min_win = MIN_WINDOW_MUTATION
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
