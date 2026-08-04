"""track_window.py — интерфейс на будущее для трекера (см. докстринг модуля).
Пока нигде не используется, но перед началом работы над трекингом стоило
зафиксировать контракт тестом: единственное окно, фиксированный k, без
джитера (в отличие от sample_window.py — это стабильный контекст для
предсказания, а не разнообразие для обучения)."""
import config
from geometry import IntBox, Square
from track_window import track_window


def test_single_square_no_jitter_deterministic():
    box = IntBox(100, 100, 150, 150)
    a = track_window(box)
    b = track_window(box)
    assert a == b, "без джиттера одинаковый вход должен давать одинаковый выход"


def test_centered_on_box_center():
    box = IntBox(100, 100, 150, 150)  # центр (125, 125)
    square = track_window(box)
    assert square.cx == 125.0 and square.cy == 125.0


def test_side_scales_with_config_k_from_natural_size():
    box = IntBox(0, 0, 40, 60)  # natural size = max(40,60) = 60
    square = track_window(box)
    assert square.side == config.TRACK_WINDOW_K * 60


def test_returns_square_type():
    square = track_window(IntBox(0, 0, 10, 10))
    assert isinstance(square, Square)


def test_non_square_box_uses_max_dimension():
    wide = track_window(IntBox(0, 0, 100, 20))
    tall = track_window(IntBox(0, 0, 20, 100))
    assert wide.side == tall.side == config.TRACK_WINDOW_K * 100
