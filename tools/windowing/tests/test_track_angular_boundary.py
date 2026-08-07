"""Граница "петля в углах <-> кадр в пикселях" (тикет "ночь", п.2.0).

Сам перевод проверяет test_angles.py, геометрию петли — test_track_logic.py.
Здесь проверяется ровно стык: track_run переводит детекции в углы на входе,
угловое окно — в пиксельную вырезку на выходе, и НИЧЕГО пиксельного внутрь
петли не протекает. Именно на этом стыке подмена единиц (передать в петлю
пиксели, оставив угловые пределы) не роняет ни один прежний тест, но делает
окно бессмысленным — поэтому тесты ниже смотрят на числа, которые при такой
подмене обязаны разъехаться.
"""
import math

import pytest

import angles as ang
import tracking_config as tcfg
from geometry import Square, resolve_placement
from track_logic import TrackState
from track_run import angular_window_to_square, det_to_angles

FRAME_W, FRAME_H = 1920, 1080
INTR = ang.from_fov(FRAME_W, FRAME_H, 75.0, "тест")
MIN_WIN = ang.px_size_to_angle(tcfg.DETECT_MIN_WINDOW_PX, INTR)
MAX_WIN = ang.px_size_to_angle(min(FRAME_W, FRAME_H), INTR)


def px_det(cx, cy, size, conf=0.5):
    return (cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2, conf)


def make_state(cx_px, cy_px, size_px):
    th, ph = ang.px_to_angle(cx_px, cy_px, INTR)
    return TrackState(tcfg, th, ph, ang.px_size_to_angle(size_px, INTR), MIN_WIN, MAX_WIN)


class TestDetToAngles:
    def test_center_and_size_survive_the_trip(self):
        det = px_det(960, 540, 100)
        th0, ph0, th1, ph1, conf, idx = det_to_angles(det, INTR, 7)
        assert conf == 0.5 and idx == 7
        # рамка в центре кадра: угловой размер — ровно 2*atan(s/2f)
        assert (th1 - th0) == pytest.approx(ang.px_size_to_angle(100, INTR))
        assert (th0 + th1) / 2 == pytest.approx(0.0, abs=1e-12)

    def test_index_recovers_the_exact_original_box(self):
        """Обратно в пиксели идём ПО ИНДЕКСУ, а не обратным преобразованием:
        иначе на рамку накапливается ошибка round-trip, и в лог/метрику
        попадает не та рамка, которую выдала модель."""
        dets = [px_det(100, 200, 30, 0.11), px_det(1800, 900, 50, 0.22),
                px_det(960, 540, 40, 0.33)]
        ang_dets = [det_to_angles(d, INTR, k) for k, d in enumerate(dets)]
        # перемешиваем: индекс обязан вести к своей рамке, а не к позиции в
        # угловом списке (в петле порядок кандидатов меняет сортировка счёта)
        for a in reversed(ang_dets):
            recovered = dets[a[5]]
            assert recovered[4] == a[4], "индекс привёл к чужой рамке"
            assert ang.px_to_angle(recovered[0], recovered[1], INTR) == (a[0], a[1])

    def test_ordering_by_distance_is_preserved_near_center(self):
        """Углы монотонны по пикселям: ближний в пикселях остаётся ближним в
        углах — иначе выбор цели менял бы решения от одной смены единиц."""
        pred_u, pred_v = 960.0, 540.0
        near, far = px_det(1000, 540, 30), px_det(1200, 540, 30)
        th_p, ph_p = ang.px_to_angle(pred_u, pred_v, INTR)
        d = []
        for det in (near, far):
            a = det_to_angles(det, INTR, 0)
            cx, cy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
            d.append(math.hypot(cx - th_p, cy - ph_p))
        assert d[0] < d[1]


class TestAngularWindowToSquare:
    def test_square_stays_square_and_isotropic_in_the_frame(self):
        sq = angular_window_to_square(0.0, 0.0, MAX_WIN, INTR)
        pl = resolve_placement(Square(sq.cx, sq.cy, sq.side), FRAME_W, FRAME_H, 640)
        assert pl.scale_x == pytest.approx(pl.scale_y), "анизотропное сжатие окна"

    def test_pixel_square_covers_the_requested_angular_window(self):
        """Недобор недопустим: цель, которую петля считает попавшей в окно,
        обязана попасть и в вырезку. Проверяем по краям кадра, где tan
        расходится сильнее всего."""
        side_ang = ang.px_size_to_angle(400, INTR)
        for u, v in ((960, 540), (300, 200), (1700, 950), (100, 1000)):
            th, ph = ang.px_to_angle(u, v, INTR)
            sq = angular_window_to_square(th, ph, side_ang, INTR)
            for dth, dph in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
                cu, cv = ang.angle_to_px(th + dth * side_ang / 2,
                                          ph + dph * side_ang / 2, INTR)
                assert cu >= sq.cx - sq.side / 2 - 1e-6
                assert cu <= sq.cx + sq.side / 2 + 1e-6
                assert cv >= sq.cy - sq.side / 2 - 1e-6
                assert cv <= sq.cy + sq.side / 2 + 1e-6

    def test_extreme_angles_do_not_blow_up(self):
        """Замороженное в LOST окно может оказаться у самого края поля зрения;
        tan вблизи pi/2 уходит в бесконечность, и без ограничения сюда
        приходил бы inf вместо стороны."""
        sq = angular_window_to_square(math.pi / 2 - 1e-9, 0.0, MAX_WIN, INTR)
        assert math.isfinite(sq.cx) and math.isfinite(sq.side)


class TestNoPixelsLeakIntoTheLoop:
    def test_small_target_gives_window_of_detect_min_pixels(self):
        """20-пиксельная цель: 3.5*20 ниже пола, окно обязано выйти ровно в
        DETECT_MIN_WINDOW_PX пикселей после обратного перевода.

        Это же и проверка единиц: передай в петлю пиксели вместо углов, и
        размер 20 будет прочитан как 20 радиан — окно мгновенно упрётся в
        потолок вместо 640.
        """
        ts = make_state(960, 540, 20)
        cx, cy, side_ang = ts.plan_window(dt=0.0)
        sq = angular_window_to_square(cx, cy, side_ang, INTR)
        assert sq.side == pytest.approx(tcfg.DETECT_MIN_WINDOW_PX, rel=1e-6)

    def test_large_target_window_capped_to_short_frame_side(self):
        ts = make_state(960, 540, 400)  # 3.5*400 = 1400 пикселей > 1080
        cx, cy, side_ang = ts.plan_window(dt=0.0)
        sq = angular_window_to_square(cx, cy, side_ang, INTR)
        assert sq.side == pytest.approx(min(FRAME_W, FRAME_H), rel=1e-6)

    def test_medium_target_window_is_k_times_size_but_in_ANGLE(self):
        """k теперь множит УГЛОВОЙ размер, а не пиксельный, и это не одно и
        то же: k*2*atan(s/2f) > 2*atan(k*s/2f), потому что atan вогнут.

        Для цели 200px это +2.5% к стороне окна (717 против 700) — реальное,
        хоть и небольшое, отличие поведения от прогонов до перехода в углы.
        Тест фиксирует именно его, чтобы разница была документирована, а не
        обнаружилась потом как расхождение метрик."""
        ts = make_state(960, 540, 200)
        cx, cy, side_ang = ts.plan_window(dt=0.0)
        sq = angular_window_to_square(cx, cy, side_ang, INTR)
        expected = ang.angle_size_to_px(tcfg.TRACK_WINDOW_K * ang.px_size_to_angle(200, INTR), INTR)
        assert sq.side == pytest.approx(expected, rel=1e-6)
        assert 700 < sq.side < 700 * 1.05, "расхождение с пиксельным k больше 5% — это уже не мелочь"

    def test_full_tick_picks_the_pixel_detection_nearest_to_prediction(self):
        """Полный такт как в track_run: детекции в пикселях -> углы -> step ->
        обратно в пиксели по индексу."""
        ts = make_state(960, 540, 100)
        dets = [px_det(1200, 540, 100), px_det(1000, 540, 100)]
        ang_dets = [det_to_angles(d, INTR, k) for k, d in enumerate(dets)]
        r = ts.step(dt=0.33, detections=ang_dets)
        assert r.chosen is not None
        assert dets[r.chosen[5]] == dets[1], "выбран не ближайший к предсказанию"

    def test_state_is_stored_in_radians_not_pixels(self):
        """Прямая проверка утверждения тикета: внутреннее состояние — углы."""
        ts = make_state(960, 540, 100)
        assert abs(ts.filter.cx) < math.pi
        assert ts.filtered_size == pytest.approx(ang.px_size_to_angle(100, INTR))
        assert ts.filtered_size < 0.1, "размер цели хранится в пикселях, а не в радианах"


class TestPixelCeilingIsRealAtFrameEdges:
    """Заявленный `max_window` — угловой, а вырезка меряется в пикселях.

    tan растянут вне оптической оси, поэтому один и тот же угол у края кадра
    занимает заметно больше пикселей, чем в центре. Замер по матрице клипов:
    21.6% тактов получали сторону БОЛЬШЕ короткой стороны кадра, 16.6%
    вырезок приходили в модель анизотропно сжатыми — при том, что комментарий
    самого кода обещает обратное.

    Прежние тесты этого не ловили, потому что все щупали ровно центр кадра
    (960, 540), где растяжения нет.
    """

    W, H = 1920, 1080

    @staticmethod
    def _intr():
        import angles as ang
        return ang.intrinsics_for("YT_best_windsurf_racing",
                                   TestPixelCeilingIsRealAtFrameEdges.W,
                                   TestPixelCeilingIsRealAtFrameEdges.H)

    @pytest.mark.parametrize("u", [960, 1440, 1700, 1850, 1919])
    @pytest.mark.parametrize("v", [540, 900, 1079])
    def test_side_never_exceeds_short_frame_side(self, u, v):
        import angles as ang
        from track_run import angular_window_to_square
        intr = self._intr()
        th, ph = ang.px_to_angle(u, v, intr)
        side_ang = ang.px_size_to_angle(min(self.W, self.H), intr)
        sq = angular_window_to_square(th, ph, side_ang, intr, self.W, self.H)
        assert sq.side <= min(self.W, self.H) + 1e-9

    def test_without_the_ceiling_it_does_exceed(self):
        """Различающая сила: без передачи размеров кадра та же точка даёт
        сторону заметно больше кадра — значит тест выше сторожит потолок, а
        не малость угла."""
        import angles as ang
        from track_run import angular_window_to_square
        intr = self._intr()
        th, ph = ang.px_to_angle(1850, 540, intr)
        side_ang = ang.px_size_to_angle(min(self.W, self.H), intr)
        sq = angular_window_to_square(th, ph, side_ang, intr)
        assert sq.side > min(self.W, self.H) * 1.2

    def test_crop_of_a_capped_square_stays_square(self):
        """Смысл потолка: вырезка перестаёт быть прямоугольной. При стороне
        больше короткой оси clamp_box_to_frame обрезает по каждой оси
        независимо, и crop() сжимает холст анизотропно."""
        from geometry import resolve_placement, Square
        sq = Square(cx=1850, cy=540, side=min(self.W, self.H))
        pl = resolve_placement(sq, self.W, self.H, 640)
        assert pl.src_box.w == pl.src_box.h
        assert pl.scale_x == pytest.approx(pl.scale_y)
