"""Убеждение трека ограничено полем зрения.

Повод — ютубный проход на 85 минутах: центр окна оказался ВНЕ кадра на 68%
тактов, и это состояние поглощающее. Замер по десяти видео: пока центр в
кадре, петля принимает кандидата на 55-100% тактов; после ухода за край —
на 0.0-1.4%, и назад практически не возвращается. Механика ухода: окно
уезжает наружу -> модель видит поля -> детекций нет -> промах -> в потере
центр заморожен снаружи навсегда.

Что здесь проверяется и почему именно так:
  * позиция проецируется на видимый конус, а скорость и ковариация — НЕТ
    (сужать гейт там, где фильтр знает меньше всего, — та же ошибка, что
    запрещена тикетом для затухания экстраполяции);
  * ловушка действительно размыкается: цель, ушедшая за край и вернувшаяся,
    захватывается снова;
  * у теста есть различающая сила — тот же прогон БЕЗ границ ловушку
    воспроизводит. Без этого контроля тест проходил бы и на выключенном
    механизме.
"""
import math
import types

import pytest

import tracking_config as base_cfg
from track_logic import STATUS_LOST, STATUS_TRACKING, TrackState

HALF_W = math.radians(20.0)      # полу-FOV по горизонтали
HALF_H = math.radians(12.0)
DT = 1.0 / 3.0
SIZE = math.radians(2.0)


def cfg(**over):
    d = {k: getattr(base_cfg, k) for k in dir(base_cfg) if k.isupper()}
    d.update(over)
    return types.SimpleNamespace(**d)


def state(bounded=True, **over):
    return TrackState(cfg(**over), 0.0, 0.0, SIZE,
                      min_window=math.radians(10.0), max_window=math.radians(40.0),
                      view_half_w=HALF_W if bounded else None,
                      view_half_h=HALF_H if bounded else None)


def det(cx, cy, size=SIZE, conf=0.9):
    return [cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2, conf]


V_TARGET = 0.15          # рад/с — цель успевает дойти до края за десяток тактов


def visible(dets, cx, cy, side):
    """Модель видит только то, что попало в ВЫРЕЗКУ. Это и есть механика
    ловушки: окно, уехавшее за кадр, не голодает «само по себе» — оно просто
    больше не показывает модели ту область, где цель есть. Тест, подающий
    детекции мимо окна, ловушку не воспроизводит вовсе (проверено: контроль
    падал)."""
    h = side / 2.0
    return [d for d in dets
            if cx - h <= (d[0] + d[2]) / 2 <= cx + h
            and cy - h <= (d[1] + d[3]) / 2 <= cy + h]


def drive(ts, ticks, target_cx, vx=0.0):
    """Такты с целью в точке target_cx (или уходящей со скоростью vx), но
    видимой только через окно. Возвращает (центры окна, принято тактов)."""
    centres, got = [], 0
    cx_t = target_cx
    for _ in range(ticks):
        cx, cy, side = ts.plan_window(DT)
        r = ts.step(DT, visible([det(cx_t, 0.0)], cx, cy, side))
        centres.append(cx)
        got += r.chosen is not None
        cx_t += vx * DT
    return centres, got


def run_away(ts, ticks=40):
    """Цель уезжает к краю кадра и выходит за него; дальше её в кадре нет."""
    drive(ts, 10, 0.0, vx=V_TARGET)              # ведём, пока цель в кадре
    centres, _ = drive(ts, ticks, 10.0)          # цель ушла: в кадре пусто
    return centres


class TestBeliefStaysInsideTheFrame:
    def test_prediction_never_leaves_the_cone(self):
        for cx in run_away(state()):
            assert -HALF_W - 1e-12 <= cx <= HALF_W + 1e-12

    def test_without_bounds_it_leaves_the_cone(self):
        """Различающая сила: без границ центр уходит ЗА полу-FOV, то есть
        тест выше проверяет механизм, а не малость скорости. Запас над
        границей берётся с большим отрывом, чем допуск теста выше (1e-12):
        правило регламента — допуск меньше охраняемого эффекта."""
        out = max(run_away(state(bounded=False)))
        assert out > HALF_W * 1.2, f"уход всего до {math.degrees(out):.1f}°"

    def test_vertical_bound_is_separate_from_horizontal(self):
        """Кадр не квадратный: одна общая граница молча растянула бы окно по
        вертикали на горизонтальный полу-FOV."""
        ts = state()
        ts.filter.cx, ts.filter.cy = 0.0, 10 * HALF_H
        ts.clamp_belief_to_view()
        assert ts.filter.cy == pytest.approx(HALF_H)
        assert HALF_H < HALF_W, "тест бессмысленен при равных полуразмерах"

    def test_point_inside_is_untouched(self):
        ts = state()
        ts.filter.cx, ts.filter.cy = 0.3 * HALF_W, -0.5 * HALF_H
        before = (ts.filter.cx, ts.filter.cy)
        ts.clamp_belief_to_view()
        assert (ts.filter.cx, ts.filter.cy) == before

    @pytest.mark.parametrize("inside", [True, False])
    def test_frozen_loss_point_is_clamped_too(self, inside):
        """В потере центр берётся из last_pred_*, а не из фильтра: не
        ограничить его — значит оставить ловушку ровно в том состоянии,
        ради которого всё это и делается.

        Оба режима проверяются явно, потому что предел у них РАЗНЫЙ, и
        перепутать их молча — значит вернуть окно наполовину в поля."""
        ts = state(VIEW_CLAMP_KEEPS_WINDOW_INSIDE=inside)
        ts.status = STATUS_LOST
        ts.last_pred_cx, ts.last_pred_cy = 5 * HALF_W, 0.0
        cx, _, side = ts.plan_window(DT)
        limit = HALF_W - side / 2 if inside else HALF_W
        assert cx == pytest.approx(limit)
        assert cx < 5 * HALF_W, "ограничение не сработало вовсе"

    def test_window_mode_keeps_the_whole_crop_inside_the_frame(self):
        """Смысл режима по умолчанию: вырезка не свисает в поля."""
        ts = state(VIEW_CLAMP_KEEPS_WINDOW_INSIDE=True)
        ts.status = STATUS_LOST
        ts.last_pred_cx, ts.last_pred_cy = 5 * HALF_W, 5 * HALF_H
        cx, cy, side = ts.plan_window(DT)
        assert cx + side / 2 <= HALF_W + 1e-12
        assert cy + side / 2 <= HALF_H + 1e-12 or side / 2 > HALF_H


class TestTrapIsOpened:
    """Главное: цель ушла за край и вернулась — трек обязан её подобрать."""

    def _leave_and_return(self, bounded):
        ts = state(bounded)
        run_away(ts, ticks=40)
        assert ts.status == STATUS_LOST, "сценарий не довёл трек до потери"
        _, got = drive(ts, 15, 0.3 * HALF_W)     # цель вернулась в кадр
        return got

    def test_target_returning_into_the_frame_is_reacquired(self):
        assert self._leave_and_return(bounded=True) > 0

    def test_without_bounds_it_is_never_reacquired(self):
        """Контроль ловушки: тот же сценарий без границ не подбирает цель ни
        разу. Если этот тест начнёт падать, значит ловушка размыкается чем-то
        другим и предыдущий тест перестал что-либо доказывать."""
        assert self._leave_and_return(bounded=False) == 0


class TestWhatMustNotBeTouched:
    def test_velocity_survives_the_clamp(self):
        """Проекция — это про позицию. Обнулять скорость на краю значит
        терять направление ухода, по которому цель вернётся."""
        ts = state()
        drive(ts, 10, 0.0, vx=V_TARGET)          # скорость набрана измерениями
        v_before = (ts.filter.vx, ts.filter.vy)
        assert abs(v_before[0]) > 0.01, "сценарий не разогнал фильтр"
        ts.filter.cx = 5 * HALF_W
        ts.clamp_belief_to_view()
        assert ts.filter.vx == pytest.approx(v_before[0])
        assert ts.filter.vy == pytest.approx(v_before[1])

    def test_position_covariance_is_not_shrunk(self):
        ts = state(FILTER_LEVEL=2)
        if not hasattr(ts.filter, "pos_covariance"):
            pytest.skip("уровень фильтра без ковариации")
        for _ in range(10):
            ts.plan_window(DT)
            ts.step(DT, [])
        p_far = ts.filter.pos_covariance()[0, 0]
        ts.filter.cx = 5 * HALF_W
        before = ts.filter.pos_covariance()[0, 0]
        ts.clamp_belief_to_view()
        assert ts.filter.pos_covariance()[0, 0] == pytest.approx(before)
        assert p_far > 0


def test_bounds_are_optional_and_default_to_no_clamping():
    """Стенд и тесты создают TrackState без границ кадра — там их взять
    неоткуда, и поведение обязано остаться прежним."""
    ts = TrackState(cfg(), 0.0, 0.0, SIZE, math.radians(10.0), math.radians(40.0))
    ts.filter.cx = 99.0
    ts.clamp_belief_to_view()
    assert ts.filter.cx == 99.0


class TestMarginsThemselves:
    """Прямые проверки _view_margins: сценарные тесты выше их не различают,
    потому что max(0, ...) съедает разницу, когда полокна больше полукадра.
    Эти три мутанта (умножение вместо деления по вертикали, значение по
    умолчанию флага, односторонние границы) пережили мутационный прогон —
    отсюда и раздел."""

    SMALL = math.radians(4.0)      # окно заведомо меньше полукадра по ОБЕИМ осям

    def test_vertical_margin_uses_half_the_side_not_a_multiple(self):
        ts = state()
        mw, mh = ts._view_margins(self.SMALL)
        assert mh == pytest.approx(HALF_H - self.SMALL / 2)
        assert mh > 0, "тест не различает деление и умножение при нулевом запасе"

    def test_horizontal_margin_matches_too(self):
        mw, _ = state()._view_margins(self.SMALL)
        assert mw == pytest.approx(HALF_W - self.SMALL / 2)

    def test_margin_never_goes_negative(self):
        """Окно шире кадра: центр обязан встать ровно в центр кадра, а не
        уехать на отрицательный запас."""
        mw, mh = state()._view_margins(10 * HALF_W)
        assert (mw, mh) == (0.0, 0.0)

    def test_flag_absent_means_window_mode(self):
        """Стенд и старые конфиги флага не знают. Умолчание обязано быть
        безопасным: вырезка в кадре, а не центр до края."""
        c = cfg()
        del c.VIEW_CLAMP_KEEPS_WINDOW_INSIDE
        ts = TrackState(c, 0.0, 0.0, SIZE, math.radians(10.0), math.radians(40.0),
                        view_half_w=HALF_W, view_half_h=HALF_H)
        assert ts._view_margins(self.SMALL)[0] == pytest.approx(HALF_W - self.SMALL / 2)

    def test_frame_mode_lets_the_centre_reach_the_edge(self):
        ts = state(VIEW_CLAMP_KEEPS_WINDOW_INSIDE=False)
        assert ts._view_margins(self.SMALL) == (HALF_W, HALF_H)

    @pytest.mark.parametrize("w,h", [(HALF_W, None), (None, HALF_H)])
    def test_one_sided_bounds_are_refused(self, w, h):
        """Одна граница из двух — молча неограниченная вторая ось. Лучше
        отказ при создании, чем трек, уезжающий по вертикали."""
        with pytest.raises(ValueError):
            TrackState(cfg(), 0.0, 0.0, SIZE, math.radians(10.0), math.radians(40.0),
                       view_half_w=w, view_half_h=h)
