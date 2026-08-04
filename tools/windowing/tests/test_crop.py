import cv2
import numpy as np
import pytest

import config
from crop import crop
from geometry import Square


def make_frame(w=1000, h=800):
    """Синтетический кадр: горизонтальный градиент + уникальный маркер 10x10
    из чистого цвета (255,0,0) в известном месте — удобно проверять паддинг
    "натуральным разрешением, без блюра" по конкретным пикселям.
    """
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    grad = np.linspace(0, 255, w, dtype=np.uint8)
    frame[:, :] = grad[np.newaxis, :, np.newaxis]
    frame[100:110, 100:110] = (255, 0, 0)  # маркер (BGR)
    return frame


def test_output_always_window_size():
    frame = make_frame()
    for side in (10, 100, 640, 1000, 3000):
        out = crop(frame, Square(cx=500, cy=400, side=side))
        assert out.shape == (config.WINDOW_SIZE, config.WINDOW_SIZE, 3)
        assert out.dtype == np.uint8


def test_downscale_matches_inter_area_directly():
    """Сторона > 640 -> ресайз, и именно INTER_AREA (сверяем побитово с
    прямым вызовом cv2.resize на том же под-изображении)."""
    frame = make_frame(2000, 1600)
    square = Square(cx=1000, cy=800, side=1200)
    out = crop(frame, square)

    x0, y0 = 1000 - 600, 800 - 600
    expected_sub = frame[y0:y0 + 1200, x0:x0 + 1200]
    expected = cv2.resize(expected_sub, (config.WINDOW_SIZE, config.WINDOW_SIZE),
                           interpolation=cv2.INTER_AREA)
    assert np.array_equal(out, expected)


def test_no_upscale_keeps_native_resolution():
    """Сторона < 640 -> НЕ увеличивать: маркер должен остаться резким
    (точное совпадение пикселей с исходником), а не быть растянутым."""
    frame = make_frame()
    side = 100
    square = Square(cx=105, cy=105, side=side)  # маркер (100,100)-(110,110) внутри
    out = crop(frame, square)

    # Маркер должен присутствовать в выходе БЕЗ искажения размера: ищем
    # непрерывный блок точного цвета маркера и меряем его сторону.
    mask = np.all(out == (255, 0, 0), axis=-1)
    ys, xs = np.where(mask)
    assert ys.size > 0, "маркер потерян при паддинге"
    marker_h = ys.max() - ys.min() + 1
    marker_w = xs.max() - xs.min() + 1
    assert marker_h == 10 and marker_w == 10, (
        f"маркер исказился при 'без увеличения': {marker_w}x{marker_h}, ожидалось 10x10"
    )


def test_undersized_square_expands_to_real_content_not_padding():
    """Паддинга больше нет (тикет "пробные прогоны", правка 1): сторона < 640
    -> область захвата расширяется до WINDOW_SIZE реальных пикселей кадра,
    а не докрашивается заливкой."""
    frame = make_frame(2000, 1600)
    out = crop(frame, Square(cx=1000, cy=800, side=100))
    x0, y0 = 1000 - config.WINDOW_SIZE // 2, 800 - config.WINDOW_SIZE // 2
    expected = frame[y0:y0 + config.WINDOW_SIZE, x0:x0 + config.WINDOW_SIZE]
    assert np.array_equal(out, expected), "ожидали прямую вырезку WINDOW_SIZE^2 реальных пикселей"


def test_side_exactly_window_size_no_resize_artifact():
    frame = make_frame(2000, 1600)
    square = Square(cx=1000, cy=800, side=config.WINDOW_SIZE)
    out = crop(frame, square)
    x0, y0 = 1000 - config.WINDOW_SIZE // 2, 800 - config.WINDOW_SIZE // 2
    expected = frame[y0:y0 + config.WINDOW_SIZE, x0:x0 + config.WINDOW_SIZE]
    assert np.array_equal(out, expected)


@pytest.mark.parametrize("cx,cy,side", [
    (0, 0, 200),        # угол кадра
    (999, 0, 200),      # правый верхний угол (frame w=1000)
    (0, 799, 200),      # левый нижний
    (999, 799, 200),    # правый нижний
    (-50, -50, 200),    # центр вообще снаружи кадра
])
def test_edge_clamp_stays_in_bounds_and_full_size(cx, cy, side):
    """Выход квадрата за границы кадра -> прижать к краю (сдвиг, не паддинг):
    результат всегда WINDOW_SIZE^2, содержимое — реальный прижатый регион кадра."""
    frame = make_frame(1000, 800)
    out = crop(frame, Square(cx=cx, cy=cy, side=side))
    assert out.shape == (config.WINDOW_SIZE, config.WINDOW_SIZE, 3)


def test_square_way_outside_frame_still_clamps_to_real_corner():
    """"Прижать к краю" — это сдвиг внутрь кадра, а не "пусто, если далеко":
    даже квадрат с центром за много кадров от границы должен вернуть
    реальное содержимое кадрового угла, без паддинга."""
    frame = make_frame(1000, 800)
    frame[:] = (0, 0, 0)
    frame[0:5, 0:5] = (255, 255, 255)  # маркер у самого угла (0,0)
    out = crop(frame, Square(cx=-10000, cy=-10000, side=100))
    # без паддинга холст = прямая вырезка WINDOW_SIZE^2 от (0,0) кадра —
    # маркер должен оказаться прямо в углу выхода, без смещения по центру.
    assert tuple(int(v) for v in out[0, 0]) == (255, 255, 255), (
        "квадрат должен был прижаться к углу (0,0) кадра, а не потеряться где-то ещё"
    )


def test_crop_side_larger_than_frame_does_not_crash():
    frame = make_frame(300, 200)
    out = crop(frame, Square(cx=150, cy=100, side=5000))
    assert out.shape == (config.WINDOW_SIZE, config.WINDOW_SIZE, 3)
