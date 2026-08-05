"""Формы счёта кандидата (тикет "счёт кандидата", п.4).

Проверяется, что каждая форма делает то, чем отличается от соседней:
  maha        — размер участвует в счёте наравне с положением, через
                ковариацию, а не через подобранный вес;
  maha_aniso  — Q вытянута ВДОЛЬ вектора скорости в K раз (мутация "изотропная
                Q при K=3" обязана ронять test_anisotropy_is_along_velocity);
  maha_vdir   — добавляется штраф за разворот, которого в форме 2 нет и быть
                не может: гауссова Q симметрична.

Уверенность детектора не входит ни в одну форму — отдельный тест.
"""
import math
import types

import numpy as np
import pytest

import tracking_config as base_cfg
from track_kalman import IDX_DPH, IDX_DTH, IDX_PH, IDX_TH, KalmanAngularFilter
from track_score import (ALL_FORMS, FORM_DISTANCE, FORM_MAHA, FORM_MAHA_ANISO,
                         FORM_MAHA_POS, FORM_MAHA_VDIR, candidate_score)

SIZE = math.radians(2.0)
DT = 1.0 / 3.0


def cfg(**over):
    d = {k: getattr(base_cfg, k) for k in dir(base_cfg) if k.isupper()}
    d.update(over)
    return types.SimpleNamespace(**d)


def det(x, y, size=SIZE, conf=0.9):
    return (x - size / 2, y - size / 2, x + size / 2, y + size / 2, conf)


def moving_filter(c=None, vx=0.15):
    f = KalmanAngularFilter(c or cfg())
    f.seed(0.0, 0.0, SIZE)
    for i in range(1, 9):
        f.update(vx * DT * i, 0.0, DT, SIZE)
    return f


class TestSizeIsPartOfTheScore:
    def test_wrong_sized_candidate_loses_to_a_slightly_farther_right_sized_one(self):
        """Ради чего форма 1 и берётся: цель правильного размера чуть дальше
        обязана победить крупного соседа чуть ближе."""
        f = moving_filter()
        c = cfg(SCORE_FORM=FORM_MAHA)
        px, py = f.predict(DT)
        right = det(px + 0.3 * SIZE, py, size=SIZE)
        wrong = det(px + 0.15 * SIZE, py, size=4 * SIZE)
        s_right = candidate_score(right, f, px, py, SIZE, DT, c)
        s_wrong = candidate_score(wrong, f, px, py, SIZE, DT, c)
        assert s_right < s_wrong

    def test_position_only_control_prefers_the_closer_wrong_size(self):
        """Контроль: двухкоординатная форма на том же наборе выбирает
        крупного соседа. Без этого утверждение "размер решает" непроверяемо."""
        f = moving_filter()
        c = cfg(SCORE_FORM=FORM_MAHA_POS)
        px, py = f.predict(DT)
        right = det(px + 0.3 * SIZE, py, size=SIZE)
        wrong = det(px + 0.15 * SIZE, py, size=4 * SIZE)
        assert candidate_score(wrong, f, px, py, SIZE, DT, c) < \
            candidate_score(right, f, px, py, SIZE, DT, c)

    def test_alpha_beta_branch_uses_log_size_term(self):
        """В ветке без ковариации размер входит слагаемым |log(s/s_pred)| —
        так предписано тикетом. Проверяем, что оно там ЕСТЬ."""
        from track_filters import AlphaBetaFilter
        f = AlphaBetaFilter(0.6, 0.3, cfg())
        f.seed(0.0, 0.0)
        c = cfg(SCORE_FORM=FORM_MAHA)
        same = candidate_score(det(0.0, 0.0, size=SIZE), f, 0.0, 0.0, SIZE, DT, c)
        big = candidate_score(det(0.0, 0.0, size=3 * SIZE), f, 0.0, 0.0, SIZE, DT, c)
        assert big > same


class TestAnisotropy:
    def test_anisotropy_is_along_velocity(self):
        """K=3: дисперсия манёвра вдоль скорости втрое больше поперечной.

        Мутация "изотропная Q при включённой анизотропии" роняет этот тест.
        """
        c = cfg(KALMAN_ANISOTROPIC_Q=True, KALMAN_ANISO_K=3.0)
        f = moving_filter(c)                    # скорость вдоль theta
        S = f.accel_covariance()
        assert S[0, 0] == pytest.approx(3.0 * S[1, 1], rel=1e-9)

    def test_isotropic_when_flag_is_off(self):
        f = moving_filter(cfg(KALMAN_ANISOTROPIC_Q=False))
        S = f.accel_covariance()
        assert S[0, 0] == pytest.approx(S[1, 1])
        assert S[0, 1] == pytest.approx(0.0)

    def test_trace_is_preserved(self):
        """След сохраняется: форма 2 обязана отличаться от формы 1 ТОЛЬКО
        анизотропией, а не общим уровнем шума — иначе сравнение форм меряет
        не то, что заявлено."""
        iso = moving_filter(cfg(KALMAN_ANISOTROPIC_Q=False)).accel_covariance()
        ani = moving_filter(cfg(KALMAN_ANISOTROPIC_Q=True, KALMAN_ANISO_K=3.0)).accel_covariance()
        assert np.trace(ani) == pytest.approx(np.trace(iso), rel=1e-9)

    def test_anisotropy_follows_a_turned_velocity(self):
        """Ось вытягивания привязана к вектору скорости, а не к осям кадра."""
        c = cfg(KALMAN_ANISOTROPIC_Q=True, KALMAN_ANISO_K=3.0)
        f = KalmanAngularFilter(c)
        f.seed(0.0, 0.0, SIZE)
        f.x[IDX_DTH], f.x[IDX_DPH] = 0.0, 0.2      # скорость вдоль phi
        S = f.accel_covariance()
        assert S[1, 1] == pytest.approx(3.0 * S[0, 0], rel=1e-9)

    def test_zero_velocity_falls_back_to_isotropic(self):
        """Направления нет — анизотропии тоже. Иначе ось вытягивания
        определялась бы шумом округления."""
        c = cfg(KALMAN_ANISOTROPIC_Q=True, KALMAN_ANISO_K=3.0)
        f = KalmanAngularFilter(c)
        f.seed(0.0, 0.0, SIZE)
        S = f.accel_covariance()
        assert S[0, 0] == pytest.approx(S[1, 1])

    def test_isotropic_Q_matches_the_old_block_diagonal_form(self):
        """Переписав Q через полную 2x2 ковариацию, нельзя было менять
        изотропный случай: он обязан совпасть с прежней поблочной формулой."""
        f = moving_filter(cfg(KALMAN_ANISOTROPIC_Q=False))
        Q = f._Q(DT)
        q = f.sigma_alpha ** 2
        blk = np.array([[DT ** 4 / 4, DT ** 3 / 2], [DT ** 3 / 2, DT ** 2]]) * q
        assert Q[IDX_TH, IDX_TH] == pytest.approx(blk[0, 0])
        assert Q[IDX_TH, IDX_DTH] == pytest.approx(blk[0, 1])
        assert Q[IDX_DTH, IDX_DTH] == pytest.approx(blk[1, 1])
        assert Q[IDX_TH, IDX_PH] == pytest.approx(0.0)   # оси не связаны

    def test_anisotropic_Q_couples_the_two_angles(self):
        """При косой скорости оси theta и phi становятся связаны — поблочная
        запись эту связь потеряла бы."""
        c = cfg(KALMAN_ANISOTROPIC_Q=True, KALMAN_ANISO_K=3.0)
        f = KalmanAngularFilter(c)
        f.seed(0.0, 0.0, SIZE)
        f.x[IDX_DTH], f.x[IDX_DPH] = 0.15, 0.15
        Q = f._Q(DT)
        assert abs(Q[IDX_TH, IDX_PH]) > 0


class TestDirectionTerm:
    def test_reversal_is_penalised(self):
        f = moving_filter()
        c = cfg(SCORE_FORM=FORM_MAHA_VDIR)
        px, py = f.predict(DT)
        d = 3 * SIZE
        fwd = det(f.cx + d, f.cy)
        back = det(f.cx - d, f.cy)
        s_f = candidate_score(fwd, f, px, py, SIZE, DT, c, velocity_ready=True)
        s_b = candidate_score(back, f, px, py, SIZE, DT, c, velocity_ready=True)
        assert s_b > s_f

    def test_form_2_cannot_tell_forward_from_backward(self):
        """Содержательная суть сравнения форм 2 и 3: гауссова Q симметрична,
        поэтому анизотропия одинаково "разрешает" уход вперёд и назад.
        Кандидаты, симметричные относительно текущей позиции, обязаны получить
        РАВНЫЙ счёт — а форма 3 их различает."""
        c2 = cfg(SCORE_FORM=FORM_MAHA_ANISO, KALMAN_ANISOTROPIC_Q=True)
        f = moving_filter(c2)
        px, py = f.cx, f.cy      # симметрия относительно ПОЗИЦИИ, не предсказания
        d = 3 * SIZE
        s_f = candidate_score(det(f.cx + d, f.cy), f, px, py, SIZE, 0.0, c2)
        s_b = candidate_score(det(f.cx - d, f.cy), f, px, py, SIZE, 0.0, c2)
        assert s_f == pytest.approx(s_b, rel=1e-9)

    def test_penalty_is_silent_below_measurement_noise(self):
        f = moving_filter()
        c = cfg(SCORE_FORM=FORM_MAHA_VDIR)
        tiny = 0.1 * base_cfg.VDIR_MIN_MOVE_SIZE_FRAC * SIZE
        plain = cfg(SCORE_FORM=FORM_MAHA)
        px, py = f.predict(DT)
        d = det(f.cx - tiny, f.cy)
        assert candidate_score(d, f, px, py, SIZE, DT, c, velocity_ready=True) == \
            pytest.approx(candidate_score(d, f, px, py, SIZE, DT, plain))

    def test_penalty_needs_a_velocity_estimate(self):
        f = moving_filter()
        c = cfg(SCORE_FORM=FORM_MAHA_VDIR)
        px, py = f.predict(DT)
        back = det(f.cx - 3 * SIZE, f.cy)
        plain = cfg(SCORE_FORM=FORM_MAHA)
        assert candidate_score(back, f, px, py, SIZE, DT, c, velocity_ready=False) == \
            pytest.approx(candidate_score(back, f, px, py, SIZE, DT, plain))


class TestConfidenceIsNotUsed:
    @pytest.mark.parametrize("form", ALL_FORMS)
    def test_confidence_does_not_change_any_score(self, form):
        """Прямое требование тикета: уверенность в счёт выбора НЕ входит ни в
        одной форме (она только в порогах теневых)."""
        f = moving_filter()
        c = cfg(SCORE_FORM=form)
        px, py = f.predict(DT)
        lo = det(px + SIZE, py, conf=0.1)
        hi = det(px + SIZE, py, conf=0.99)
        assert candidate_score(lo, f, px, py, SIZE, DT, c) == \
            pytest.approx(candidate_score(hi, f, px, py, SIZE, DT, c))


def test_unknown_form_is_an_error_not_a_silent_fallback():
    """Опечатка в имени формы должна ронять прогон, а не молча возвращать
    прежнее правило: иначе вся таблица отбора окажется одной и той же формой."""
    f = moving_filter()
    with pytest.raises(ValueError):
        candidate_score(det(0, 0), f, 0, 0, SIZE, DT, cfg(SCORE_FORM="махаланобис"))


def test_distance_form_is_plain_euclid():
    f = moving_filter()
    c = cfg(SCORE_FORM=FORM_DISTANCE)
    assert candidate_score(det(0.3, 0.4), f, 0.0, 0.0, SIZE, DT, c) == pytest.approx(0.5)
