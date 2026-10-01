"""track_logic.py — выбор цели, размер-фильтр,
расширение окна на пропуск, состояние трека (miss/lost/reacquire).

Единицы. После перехода на углы петля работает в радианах, но САМА
геометрия здесь от единиц не зависит — тесты написаны в условных единицах,
где min_window=640 играет роль прежнего пола "столько пикселей crop() всё
равно вырежет", а max_window — прежней короткой стороны кадра. Числа взяты
теми же, что и раньше, чтобы отличия от прежнего поведения были видны, а не
спрятаны за сменой масштаба. Перевод пиксели<->углы проверяется отдельно:
test_angles.py (сам перевод) и test_track_angular_boundary.py (границы, где
петля встречается с пикселями).
"""
import types

import pytest

from track_logic import (
    STATUS_LOST, STATUS_TRACKING, TrackState, expand_window_side,
    select_target, update_size_filter,
)


def make_cfg(**overrides):
    cfg = types.SimpleNamespace(
        TRACK_WINDOW_K=3.5,
        TARGET_SELECT_MAX_DIST_FRAC=0.30,
        REACQUIRE_MAX_DIST_FRAC=0.30,
        MISS_TO_LOST_N=5,
        WINDOW_EXPAND_PER_MISS=1.15,
        SIZE_FILTER_GROW_RATE=0.5,
        SIZE_FILTER_SHRINK_RATE=0.1,
        FILTER_LEVEL=0,
        ALPHA_BETA_ALPHA=0.6,
        ALPHA_BETA_BETA=0.3,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class TestWindowGeometryIsWhatModelActuallySees:
    """Сторона окна обязана совпадать с тем, что реально увидит модель.
    Ниже min_window окно — фикция (crop() всё равно вырежет не меньше), выше
    max_window вырезка перестаёт быть квадратной. Оба предела петля получает
    снаружи уже в своих единицах."""

    def test_small_target_window_floored_to_detect_min(self):
        cfg = make_cfg(TRACK_WINDOW_K=3.5)
        ts = TrackState(cfg, 500, 500, 20, 640, 1080)  # 3.5*20 = 70 номинально
        assert ts.current_window_side() == pytest.approx(640)

    def test_large_target_window_capped_to_short_frame_side(self):
        """Потолок — КОРОТКАЯ сторона кадра: иначе src_box неквадратный и
        картинка приходит в сеть анизотропно сплющенной."""
        cfg = make_cfg(TRACK_WINDOW_K=3.5)
        ts = TrackState(cfg, 960, 540, 400, 640, 1080)  # 3.5*400 = 1400 > 1080
        assert ts.current_window_side() == pytest.approx(1080)


    def test_selection_threshold_uses_real_window_not_nominal(self):
        """Порог приёма считается от реальной стороны (640), а не от
        номинальных 70 — иначе он втрое строже задуманного."""
        cfg = make_cfg(TARGET_SELECT_MAX_DIST_FRAC=0.30)
        ts = TrackState(cfg, 500, 500, 20, 640, 1080)
        # 150px от предсказания: внутри 0.3*640=192, но вне 0.3*70=21
        det = (640, 490, 660, 510, 0.5)
        r = ts.step(dt=0.33, detections=[det])
        assert r.chosen is not None


class TestExtrapolationIsApplied:
    """Шаг 5 петли: окно строится вокруг ПРЕДСКАЗАНИЯ на следующий
    такт, а не вокруг позиции с прошлого обновления."""

    def _moving(self, cfg):
        ts = TrackState(cfg, 100, 500, 200, 640, 1080)
        ts.step(dt=1.0, detections=[(200 - 100, 400, 200 + 100, 600, 0.9)])  # центр 200
        return ts

    def test_plan_window_leads_the_target(self):
        cfg = make_cfg(FILTER_LEVEL=0)
        ts = self._moving(cfg)          # vx = 100 px/сек
        cx, cy, side = ts.plan_window(dt=1.0)
        assert cx == pytest.approx(300.0), "окно должно вести цель, а не стоять на прошлой позиции"

    def test_step_uses_same_point_as_plan_window(self):
        cfg = make_cfg(FILTER_LEVEL=0)
        ts = self._moving(cfg)
        cx, cy, _ = ts.plan_window(dt=1.0)
        r = ts.step(dt=1.0, detections=[])
        assert (r.predicted_cx, r.predicted_cy) == pytest.approx((cx, cy))

    def test_fast_target_stays_selectable_thanks_to_extrapolation(self):
        """Цель, уезжающая на 100px за такт, при узком пороге приёма: с
        экстраполяцией она ровно в центре окна, без неё — за порогом, и
        захват уехал бы на соседа."""
        cfg = make_cfg(FILTER_LEVEL=0)
        ts = self._moving(cfg)          # разгон при штатном пороге
        cfg.TARGET_SELECT_MAX_DIST_FRAC = 0.10   # теперь сужаем: 0.1*700 = 70px
        r = ts.step(dt=1.0, detections=[(300 - 100, 400, 300 + 100, 600, 0.9)])
        assert r.chosen is not None, "цель на 100px впереди отвергнута — экстраполяция не применена"
        assert r.chosen_dist == pytest.approx(0.0, abs=1e-6)


class TestSelectTarget:
    def test_closest_within_threshold_wins(self):
        near = (95, 95, 105, 105, 0.9)
        far = (140, 140, 160, 160, 0.9)
        chosen, d = select_target(100, 100, [far, near], window_side=100, max_dist_frac=0.3)
        assert chosen == near

    def test_outside_threshold_rejected_even_if_only_candidate(self):
        far = (500, 500, 520, 520, 0.99)
        chosen, d = select_target(100, 100, [far], window_side=100, max_dist_frac=0.3)
        assert chosen is None

    def test_no_detections_is_miss(self):
        chosen, d = select_target(100, 100, [], window_side=100, max_dist_frac=0.3)
        assert chosen is None

    def test_ignores_confidence_closest_wins_even_if_less_confident(self):
        """Правило: НЕ выбирать по уверенности — ближе, но менее уверенная
        детекция должна победить более уверенную, но дальнюю (другой сёрфер)."""
        close_low_conf = (98, 100, 102, 104, 0.20)
        far_high_conf = (130, 100, 134, 104, 0.95)
        chosen, d = select_target(100, 100, [far_high_conf, close_low_conf],
                                   window_side=200, max_dist_frac=0.5)
        assert chosen == close_low_conf


class TestUpdateSizeFilter:
    def test_grows_using_grow_rate(self):
        out = update_size_filter(filtered_size=100, measured_size=200,
                                  grow_rate=0.5, shrink_rate=0.1)
        assert out == pytest.approx(100 + 0.5 * 100)

    def test_shrinks_using_shrink_rate_slower(self):
        out = update_size_filter(filtered_size=100, measured_size=50,
                                  grow_rate=0.5, shrink_rate=0.1)
        assert out == pytest.approx(100 + 0.1 * (-50))

    def test_asymmetry_shrink_slower_than_grow_for_same_magnitude_change(self):
        grow = update_size_filter(100, 150, grow_rate=0.5, shrink_rate=0.1)
        shrink = update_size_filter(100, 50, grow_rate=0.5, shrink_rate=0.1)
        assert (grow - 100) > (100 - shrink)  # выросло сильнее, чем упало


class TestExpandWindowSide:
    def test_zero_misses_unchanged(self):
        assert expand_window_side(100, 0, 1.15) == 100

    def test_compounds_per_miss(self):
        assert expand_window_side(100, 3, 1.15) == pytest.approx(100 * 1.15 ** 3)


class TestTrackStateHappyPath:
    def test_stays_tracking_with_detections_near_prediction(self):
        cfg = make_cfg()
        ts = TrackState(cfg, init_cx=100, init_cy=100, init_size=20, min_window=640, max_window=800)
        for _ in range(5):
            det = (95, 95, 105, 105, 0.8)
            r = ts.step(dt=0.33, detections=[det])
            assert r.status == STATUS_TRACKING
            assert r.chosen is not None
            assert r.miss_count == 0

    def test_window_side_is_k_times_size_when_above_detect_floor(self):
        cfg = make_cfg(TRACK_WINDOW_K=3.5)
        ts = TrackState(cfg, init_cx=500, init_cy=400, init_size=200,
                         min_window=640, max_window=1080)
        assert ts.current_window_side() == pytest.approx(3.5 * 200)  # 700 > пола 640


class TestTrackStateMissesAndLoss:
    def test_miss_extrapolates_and_expands_window(self):
        cfg = make_cfg(WINDOW_EXPAND_PER_MISS=1.2)
        ts = TrackState(cfg, init_cx=100, init_cy=100, init_size=20, min_window=640, max_window=800)
        side0 = ts.current_window_side()
        r = ts.step(dt=0.33, detections=[])  # промах
        assert r.status == STATUS_TRACKING
        assert r.chosen is None
        assert r.miss_count == 1
        assert ts.current_window_side() == pytest.approx(side0 * 1.2)

    def test_n_consecutive_misses_transitions_to_lost_exactly_on_nth(self):
        cfg = make_cfg(MISS_TO_LOST_N=5)
        ts = TrackState(cfg, init_cx=100, init_cy=100, init_size=20, min_window=640, max_window=800)
        for i in range(1, 5):
            r = ts.step(dt=0.33, detections=[])
            assert r.status == STATUS_TRACKING, f"не должен был потеряться на промахе {i}"
            assert r.lost_transition is False
        r5 = ts.step(dt=0.33, detections=[])
        assert r5.status == STATUS_LOST
        assert r5.lost_transition is True

    def test_lost_window_frozen_at_last_known_position(self):
        cfg = make_cfg(MISS_TO_LOST_N=2)
        ts = TrackState(cfg, init_cx=100, init_cy=100, init_size=20, min_window=640, max_window=800)
        ts.step(dt=0.33, detections=[])
        ts.step(dt=0.33, detections=[])  # теперь LOST
        r1 = ts.step(dt=0.33, detections=[])
        r2 = ts.step(dt=0.33, detections=[])
        assert r1.predicted_cx == r2.predicted_cx == 100
        assert r1.predicted_cy == r2.predicted_cy == 100

    def test_lost_window_keeps_growing_up_to_frame_short_side(self):
        """Потолок — КОРОТКАЯ сторона кадра, а не длинная: окно должно
        остаться квадратным, иначе вырезка неквадратная и картинка приходит
        в сеть сплющенной."""
        cfg = make_cfg(MISS_TO_LOST_N=1, WINDOW_EXPAND_PER_MISS=1.5)
        ts = TrackState(cfg, init_cx=100, init_cy=100, init_size=20, min_window=640, max_window=200)
        ts.step(dt=0.33, detections=[])  # -> LOST сразу (N=1)
        prev = ts.current_window_side()
        for _ in range(30):  # много промахов, должно упереться в потолок
            ts.step(dt=0.33, detections=[])
            cur = ts.current_window_side()
            assert cur >= prev
            prev = cur
        assert ts.current_window_side() == pytest.approx(min(300, 200))

    def test_very_long_loss_does_not_overflow(self):
        """Затяжная потеря (сотни тактов) не должна переполнять возведение
        в степень до того, как сработает ограничение кадром."""
        cfg = make_cfg(MISS_TO_LOST_N=1, WINDOW_EXPAND_PER_MISS=1.15)
        ts = TrackState(cfg, init_cx=100, init_cy=100, init_size=20, min_window=640, max_window=1080)
        for _ in range(2000):
            ts.step(dt=0.33, detections=[])
        assert ts.current_window_side() == pytest.approx(1080)

    def test_far_detection_during_tracking_is_rejected_not_a_swap(self):
        """Чужой сёрфер рядом не должен подменить цель — просто промах."""
        cfg = make_cfg(TARGET_SELECT_MAX_DIST_FRAC=0.3, TRACK_WINDOW_K=3.5)
        ts = TrackState(cfg, init_cx=100, init_cy=100, init_size=20, min_window=640, max_window=800)
        other_surfer = (400, 400, 420, 420, 0.9)  # далеко за порогом
        r = ts.step(dt=0.33, detections=[other_surfer])
        assert r.chosen is None
        assert r.miss_count == 1


class TestReacquisition:
    def test_detection_near_last_known_during_lost_reacquires(self):
        cfg = make_cfg(MISS_TO_LOST_N=1)
        ts = TrackState(cfg, init_cx=100, init_cy=100, init_size=20, min_window=640, max_window=800)
        ts.step(dt=0.33, detections=[])  # -> LOST
        assert ts.status == STATUS_LOST
        det_near = (95, 95, 105, 105, 0.7)
        r = ts.step(dt=0.33, detections=[det_near])
        assert r.status == STATUS_TRACKING
        assert r.reacquired is True
        assert ts.miss_count == 0

    def test_reacquire_resets_velocity(self):
        cfg = make_cfg(MISS_TO_LOST_N=1, FILTER_LEVEL=0)
        ts = TrackState(cfg, init_cx=0, init_cy=0, init_size=20, min_window=640, max_window=800)
        ts.step(dt=1.0, detections=[])  # miss -> LOST (заморожено на 0,0)
        ts.step(dt=1.0, detections=[(95, -5, 105, 5, 0.7)])  # реакквизиция на (100,0)
        assert ts.filter.vx == 0.0, "скорость после реакквизиции должна быть с нуля, не унаследована"

    def test_expansion_in_lost_actually_admits_new_detections(self):
        """Механизм расширения окна в LOST обязан работать: детекция, недостижимая сразу после
        потери, должна становиться достижимой по мере роста окна. Раньше
        радиус приёма (0.3*S) всегда лежал внутри полуширины (0.5*S)
        НЕрасширенного окна, и рост окна не мог принять ничего нового."""
        cfg = make_cfg(MISS_TO_LOST_N=1, WINDOW_EXPAND_PER_MISS=1.15,
                        REACQUIRE_MAX_DIST_FRAC=0.30)
        ts = TrackState(cfg, 500, 500, 20, 640, 1080)
        ts.step(dt=0.33, detections=[])  # -> LOST, окно 640*1.15
        far = (500 + 300 - 10, 490, 500 + 300 + 10, 510, 0.5)  # 300px от точки заморозки

        r_early = ts.step(dt=0.33, detections=[far])
        assert r_early.reacquired is False, "сразу после потери 300px ещё вне радиуса"

        for _ in range(12):
            r = ts.step(dt=0.33, detections=[far])
            if r.reacquired:
                break
        assert r.reacquired is True, "по мере роста окна детекция обязана стать достижимой"

    def test_lost_freezes_at_last_prediction_not_last_detection(self):
        """Окно замораживается на месте последнего ПРЕДСКАЗАНИЯ.
        За N промахов до потери фильтр экстраполирует цель — выбрасывать это
        движение значит искать там, где цели заведомо уже нет."""
        cfg = make_cfg(MISS_TO_LOST_N=3, FILTER_LEVEL=0)
        ts = TrackState(cfg, 100, 500, 200, 640, 1080)
        ts.step(dt=1.0, detections=[(100, 400, 300, 600, 0.9)])  # центр 200, vx=100
        for _ in range(3):
            ts.step(dt=1.0, detections=[])                       # промахи -> LOST
        cx, cy, _ = ts.plan_window(dt=1.0)
        assert cx > 400, f"заморозились на устаревшей детекции (cx={cx}), а не на предсказании"

    def test_detection_far_from_last_known_during_lost_not_reacquired(self):
        cfg = make_cfg(MISS_TO_LOST_N=1, WINDOW_EXPAND_PER_MISS=1.0)
        ts = TrackState(cfg, init_cx=100, init_cy=100, init_size=20, min_window=640, max_window=2000)
        ts.step(dt=0.33, detections=[])  # -> LOST, last_known_window_side = 3.5*20=70
        far = (900, 900, 920, 920, 0.9)  # далеко от (100,100), порог 0.3*70=21
        r = ts.step(dt=0.33, detections=[far])
        assert r.status == STATUS_LOST
        assert r.reacquired is False
