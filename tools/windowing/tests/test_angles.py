"""Перевод пиксели <-> углы.

Главный тест — тождество пиксель -> угол -> пиксель по ВСЕМУ кадру, включая
углы: именно там малоугловое приближение расходится с честным atan, и именно
там его подмена прошла бы незамеченной на центральных кадрах.
"""
import math

import pytest

import angles
from angles import (Intrinsics, angle_size_to_px, angle_to_px, from_fov,
                    intrinsics_for, px_size_to_angle, px_to_angle)

INTR = from_fov(1920, 1080, 75.0, "тест")


@pytest.mark.parametrize("u,v", [
    (0, 0), (1919, 0), (0, 1079), (1919, 1079),      # все четыре угла кадра
    (960, 540),                                       # центр
    (0, 540), (1919, 540), (960, 0), (960, 1079),     # середины сторон
    (1, 1), (1918, 1078), (480, 270), (1440, 810),
])
def test_pixel_angle_pixel_identity(u, v):
    th, ph = px_to_angle(u, v, INTR)
    u2, v2 = angle_to_px(th, ph, INTR)
    assert u2 == pytest.approx(u, abs=1e-6)
    assert v2 == pytest.approx(v, abs=1e-6)


def test_identity_holds_on_dense_grid_including_edges():
    worst = 0.0
    for u in range(0, 1920, 37):
        for v in range(0, 1080, 41):
            th, ph = px_to_angle(u, v, INTR)
            u2, v2 = angle_to_px(th, ph, INTR)
            worst = max(worst, abs(u2 - u), abs(v2 - v))
    assert worst < 1e-6, f"худшее расхождение по сетке {worst}"


def test_size_roundtrip():
    for s in (5.0, 30.0, 120.0, 640.0, 1080.0):
        assert angle_size_to_px(px_size_to_angle(s, INTR), INTR) == pytest.approx(s, abs=1e-6)


class TestNotSmallAngleApproximation:
    """Честный atan, а не theta ~ (u-cx)/f. Приближение систематически
    сжимает края кадра, и на 1920px это уже проценты."""

    def test_center_is_zero_angle(self):
        assert px_to_angle(INTR.cx, INTR.cy, INTR) == (0.0, 0.0)

    def test_edge_differs_from_linear_approximation(self):
        u = 1919.0
        th, _ = px_to_angle(u, INTR.cy, INTR)
        linear = (u - INTR.cx) / INTR.fx
        assert abs(th - linear) > 0.01, "на краю кадра atan обязан заметно отличаться от линейного"

    def test_half_fov_at_frame_edge(self):
        """У края кадра угол обязан равняться половине заявленного FOV."""
        th, _ = px_to_angle(1920.0, INTR.cy, INTR)
        assert math.degrees(th) == pytest.approx(75.0 / 2, abs=0.05)


class TestIntrinsicsTable:
    def test_every_clip_folder_resolves(self):
        import os
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "..", "..", "Data", "frames", "val_manual")
        if not os.path.isdir(root):
            pytest.skip("нет папки клипов")
        for name in sorted(os.listdir(root)):
            if not os.path.isdir(os.path.join(root, name)):
                continue
            intr = intrinsics_for(name)
            assert intr.fx > 0
            assert intr is not angles.DEFAULT_INTRINSICS, (
                f"клип {name} не найден в таблице и молча получил дефолт")

    def test_source_is_recorded_for_every_entry(self):
        for key, intr in angles.CLIP_INTRINSICS.items():
            assert intr.source, f"у {key} не указан источник f_x"

    def test_frame_size_override_moves_center_and_scales_focal(self):
        """Раньше тест требовал, чтобы f_x НЕ менялся при смене размера кадра,
        и тем самым закреплял дефект: на 4K это давало FOV 113.8° вместо 75°.
        Правильно — масштабировать f_x вместе с шириной, сохраняя поле зрения."""
        base = intrinsics_for("YT_Primbee_Speed_Windsurfing_8bYtDBZkrpM")
        moved = intrinsics_for("YT_Primbee_Speed_Windsurfing_8bYtDBZkrpM", 1600, 1012)
        assert moved.fx == pytest.approx(base.fx * 1600 / 1920)
        assert moved.cx == pytest.approx(800.0)
        assert moved.cy == pytest.approx(506.0)

    def test_partial_frame_size_is_refused(self):
        """Половина размера кадра — не размер кадра: центр по одной оси
        остался бы от другого разрешения, и это проявилось бы только
        смещением углов."""
        with pytest.raises(ValueError):
            intrinsics_for("VID_20230624_145515", 1920, None)
        with pytest.raises(ValueError):
            intrinsics_for("VID_20230624_145515", None, 1080)

    def test_fov_to_focal_matches_formula(self):
        intr = from_fov(1920, 1080, 90.0, "тест")
        assert intr.fx == pytest.approx(960.0)  # tan(45°)=1

    def test_principal_point_is_the_frame_centre(self):
        """Отдельно от fx: главная точка — середина кадра. Ошибка здесь не
        видна в тождестве пиксель->угол->пиксель (она сокращается), зато
        сдвигает ВСЕ абсолютные углы."""
        intr = from_fov(1600, 900, 70.0, "тест")
        assert intr.cx == pytest.approx(800.0)
        assert intr.cy == pytest.approx(450.0)

    def test_table_resolution_matches_the_actual_frames(self):
        """Разрешение в таблице обязано совпадать с реальными кадрами клипа:
        опечатка в нём сдвигает главную точку и все углы, а по картинке это
        никак не заметно."""
        import glob
        import os

        import cv2
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "..", "..", "Data", "frames", "val_manual")
        if not os.path.isdir(root):
            pytest.skip("нет папки клипов")
        seen = set()
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            jpgs = sorted(glob.glob(os.path.join(d, "*.jpg")))
            if not jpgs:
                continue
            h, w = cv2.imread(jpgs[0]).shape[:2]
            intr = intrinsics_for(name)
            assert intr.cx == pytest.approx(w / 2.0), f"{name}: ширина в таблице не та"
            assert intr.cy == pytest.approx(h / 2.0), f"{name}: высота в таблице не та"
            seen.add(name)
        assert seen, "тест бесполезен, если ни один клип не проверен"


class TestFocalScalesWithResolution:
    """f_x измеряется в ПИКСЕЛЯХ и обязан расти вместе с шириной кадра.

    Раньше при передаче размера кадра пересчитывался только центр, а f_x
    оставался табличным. На 4K-видео (3840 против табличных 1920) это давало
    подразумеваемое поле зрения 113.8° вместо 75° — то есть все углы вдвое
    меньше настоящих. Дефект нашла валидация, не тесты.
    """

    def test_double_width_doubles_focal(self):
        base = intrinsics_for("YT_best_windsurf_racing_bp6nX64OeI0", 1920, 1080)
        big = intrinsics_for("YT_best_windsurf_racing_bp6nX64OeI0", 3840, 2160)
        assert big.fx == pytest.approx(2 * base.fx, rel=1e-9)
        assert big.cx == pytest.approx(1920.0)

    def test_field_of_view_is_preserved_across_resolutions(self):
        """Смысл масштабирования: поле зрения — свойство объектива и от
        разрешения не зависит."""
        for w, h in ((1920, 1080), (3840, 2160), (2560, 1440)):
            intr = intrinsics_for("YT_best_windsurf_racing_bp6nX64OeI0", w, h)
            fov = 2 * math.degrees(math.atan((w / 2) / intr.fx))
            assert fov == pytest.approx(75.0, abs=0.1), f"{w}x{h}: FOV {fov:.1f}"

    def test_angle_of_the_same_direction_is_resolution_independent(self):
        """Прямая проверка следствия: точка на одной и той же ДОЛЕ кадра
        обязана давать один и тот же угол при любом разрешении."""
        a = px_to_angle(1920 * 0.75, 540, intrinsics_for("VID_20230624_145515", 1920, 1080))
        b = px_to_angle(3840 * 0.75, 1080, intrinsics_for("VID_20230624_145515", 3840, 2160))
        assert a[0] == pytest.approx(b[0], rel=1e-9)
