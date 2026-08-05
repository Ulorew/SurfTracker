"""Формы счёта кандидата (тикет "счёт кандидата, теневые треки, синтетический
стенд", п.4).

Меньший счёт лучше. Уверенность детектора в счёт НЕ входит ни в одной форме —
она участвует только в порогах рождения и питания теневых треков.

Формы:
  distance    — прежнее правило: евклидово расстояние до предсказания.
                Оставлено как база сравнения, а не как кандидат.
  maha        — форма 1: махаланобис d^2 по (theta, phi, log h). Свободных
                параметров нет вовсе: ковариация целиком из R и Q, то есть из
                физики. Размер — третья координата, а не слагаемое с весом.
  maha_aniso  — форма 2: то же, но Q вытянута вдоль вектора скорости в K раз
                (см. KalmanAngularFilter.accel_covariance).
  maha_vdir   — форма 3: форма 1 плюс АДДИТИВНЫЙ штраф за разворот. Это
                контроль против формы 2: гауссова Q симметрична и отличить
                разворот от ускорения не может в принципе, а направленный член
                может. Если форма 3 обходит форму 2 — анизотропия как модель
                механизма В неверна.

Ветка alpha-beta. Там нет ни ковариации, ни log h в состоянии, поэтому
"махаланобис" вырождается в нормировку на ШУМ ИЗМЕРЕНИЯ: положение делится на
(0.3*размер), логарифм отношения размеров — на KALMAN_R_LOGH. Это честная
нижняя граница той же формулы (P = 0), и она даёт ровно то, что предписывает
тикет для этой ветки: |log(s_cand/s_pred)| вместо третьей координаты.
"""

import math

from track_filters import dist

FORM_DISTANCE = "distance"
FORM_MAHA = "maha"
FORM_MAHA_ANISO = "maha_aniso"
FORM_MAHA_VDIR = "maha_vdir"
# КОНТРОЛЬНАЯ форма, не кандидат: махаланобис только по (theta, phi), без
# размера. Существует ровно затем, чтобы показать, что третья координата
# что-то решает — на разноразмерном пересечении она обязана проигрывать
# форме 1. Без такого контроля утверждение "log h помогает" непроверяемо.
FORM_MAHA_POS = "maha_pos"
ALL_FORMS = (FORM_DISTANCE, FORM_MAHA, FORM_MAHA_ANISO, FORM_MAHA_VDIR)
MAHA_FORMS = (FORM_MAHA, FORM_MAHA_ANISO, FORM_MAHA_VDIR, FORM_MAHA_POS)


def _det_center(det):
    return ((det[0] + det[2]) / 2.0, (det[1] + det[3]) / 2.0)


def _det_size(det):
    return max(det[2] - det[0], det[3] - det[1])


def direction_penalty(det, prev_cx, prev_cy, vel, pred_size, dt, cfg):
    """Штраф за РАЗВОРОТ, в тех же безразмерных единицах, что и d^2.

    (1 - cos)/2 из [0,1] умножается на VDIR_MAHA_LAMBDA: полный разворот
    стоит столько же, сколько уход на VDIR_MAHA_LAMBDA единиц d^2. Ноль, пока
    движение меньше шума измерения — иначе штраф раздаётся по знаку шума.
    """
    if vel is None:
        return 0.0
    vmag = math.hypot(vel[0], vel[1])
    dcx, dcy = _det_center(det)
    ux, uy = dcx - prev_cx, dcy - prev_cy
    umag = math.hypot(ux, uy)
    min_move = cfg.VDIR_MIN_MOVE_SIZE_FRAC * pred_size
    if umag <= min_move or vmag * dt <= min_move:
        return 0.0
    cos = (ux * vel[0] + uy * vel[1]) / (umag * vmag)
    return cfg.VDIR_MAHA_LAMBDA * (1.0 - cos) / 2.0


def _fallback_maha(det, pred_cx, pred_cy, pred_size, cfg):
    """Ветка без ковариации (alpha-beta): нормировка на шум измерения."""
    dcx, dcy = _det_center(det)
    sigma_pos = max(cfg.KALMAN_R_POS_SIZE_FRAC * pred_size, 1e-12)
    d2 = (dist(dcx, dcy, pred_cx, pred_cy) / sigma_pos) ** 2
    s_cand = max(_det_size(det), 1e-12)
    s_pred = max(pred_size, 1e-12)
    d2 += (abs(math.log(s_cand / s_pred)) / cfg.KALMAN_R_LOGH) ** 2
    return d2


def candidate_score(det, filt, pred_cx, pred_cy, pred_size, dt, cfg,
                     velocity_ready=False):
    """-> счёт кандидата (меньше лучше) по форме cfg.SCORE_FORM."""
    form = getattr(cfg, "SCORE_FORM", FORM_DISTANCE)
    if form == FORM_DISTANCE:
        dcx, dcy = _det_center(det)
        return dist(dcx, dcy, pred_cx, pred_cy)

    if form not in MAHA_FORMS:
        raise ValueError(f"неизвестная форма счёта: {form!r} (см. track_score.ALL_FORMS)")

    dcx, dcy = _det_center(det)
    if form == FORM_MAHA_POS:
        if hasattr(filt, "gate_distance2"):
            g = filt.gate_distance2(dcx, dcy, _det_size(det), dt)
            if g is not None:
                return g
        sigma = max(cfg.KALMAN_R_POS_SIZE_FRAC * pred_size, 1e-12)
        return (dist(dcx, dcy, pred_cx, pred_cy) / sigma) ** 2

    if hasattr(filt, "score_distance2"):
        # анизотропия — свойство фильтра (она в Q), форма её только включает
        score = filt.score_distance2(dcx, dcy, _det_size(det), dt)
    else:
        score = _fallback_maha(det, pred_cx, pred_cy, pred_size, cfg)

    if form == FORM_MAHA_VDIR:
        vel = (filt.vx, filt.vy) if velocity_ready else None
        score += direction_penalty(det, filt.cx, filt.cy, vel, pred_size, dt, cfg)

    return score
