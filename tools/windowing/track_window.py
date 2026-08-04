"""Окно для боевого режима — от предсказанного положения (тикет, п.2).

Пока используется только как интерфейс на будущее (трекер — вне рамок этого
тикета): единственное окно, k фиксирован, никакого джиттера и подбора корзины
— в отличие от sample_window это не про обучающее разнообразие, а про то,
чтобы дать трекеру стабильный, предсказуемый контекст вокруг предсказания.
"""

from geometry import IntBox, Square, box_center
import config


def track_window(predicted_box: IntBox) -> Square:
    """Единственное окно вокруг предсказанного бокса, фиксированный k."""
    b_size = max(predicted_box.w, predicted_box.h)
    cx, cy = box_center(predicted_box)
    side = config.TRACK_WINDOW_K * b_size
    return Square(cx=cx, cy=cy, side=side)
