"""Перевод пиксели <-> углы (тикет "ночь", п.2.0).

Внутреннее состояние петли слежения целиком в угловых единицах: позиция,
скорость, размер цели, сторона окна, все гейты. Пиксели остаются только на
входе (детекции) и выходе (вырезка окна, оверлей).

Зачем: на телефоне угол цели в кадре складывается с углом стенда, и петля
обязана жить в тех же единицах. На офлайн-видео угол камеры — константа,
поэтому переход ничего не ломает, но делать его дешевле до Калмана, чем
после (иначе ковариацию пришлось бы переопределять дважды).

Перевод честный, через atan/tan, а НЕ через малоугловое приближение
theta ~ (u-cx)/fx: на краю кадра 1920px при f_x~1250 разница достигает
нескольких процентов, и она систематическая — приближение сжимает края.
"""

import math
from typing import NamedTuple


class Intrinsics(NamedTuple):
    fx: float
    fy: float
    cx: float
    cy: float
    source: str          # откуда взято f_x: EXIF / метаданные / прикидка
    note: str = ""


def from_fov(width: int, height: int, fov_h_deg: float, source: str, note: str = "") -> Intrinsics:
    """f_x из горизонтального поля зрения: f = (W/2) / tan(FOV/2).

    Пиксели считаем квадратными (f_y = f_x) — для всех наших источников это
    так; иначе понадобился бы отдельный вертикальный FOV.
    """
    fx = (width / 2.0) / math.tan(math.radians(fov_h_deg) / 2.0)
    return Intrinsics(fx=fx, fy=fx, cx=width / 2.0, cy=height / 2.0,
                      source=source, note=note)


def px_to_angle(u: float, v: float, intr: Intrinsics) -> "tuple[float, float]":
    """Пиксель -> (theta, phi) в радианах."""
    return (math.atan((u - intr.cx) / intr.fx),
            math.atan((v - intr.cy) / intr.fy))


def angle_to_px(theta: float, phi: float, intr: Intrinsics) -> "tuple[float, float]":
    """(theta, phi) -> пиксель. Точная обратная к px_to_angle."""
    return (intr.cx + intr.fx * math.tan(theta),
            intr.cy + intr.fy * math.tan(phi))


def px_size_to_angle(size_px: float, intr: Intrinsics) -> float:
    """Угловой размер цели: 2*atan(s / (2*f_x)).

    Это угол, стягиваемый объектом в ЦЕНТРЕ кадра. У края тот же объект в
    пикселях стягивает меньший угол, но поправку сознательно не вводим:
    размер нужен для масштаба окна и для механизма А (отношение размеров),
    а там важна не абсолютная точность, а согласованность между тактами.
    """
    return 2.0 * math.atan(size_px / (2.0 * intr.fx))


def angle_size_to_px(size_ang: float, intr: Intrinsics) -> float:
    """Обратная к px_size_to_angle."""
    return 2.0 * intr.fx * math.tan(size_ang / 2.0)


# --- таблица клипов -------------------------------------------------------
# Метаданных объектива нет ни у одного из четырёх исходных видео (проверено
# ffprobe: ни EXIF, ни tags с фокусным). Поэтому ВСЕ значения — прикидка по
# типичному полю зрения, и это помечено в source. Для логики петли это
# приемлемо: она работает на РАЗНОСТЯХ углов, и общий масштабный множитель
# сокращается в сравнениях "скорость против допуска" и "размер против
# размера". Ошибка масштаба сместит абсолютные числа в отчёте, но не
# упорядочивание конфигураций.
PHONE_FOV_H_DEG = 67.0    # типичная основная камера смартфона
DRONE_FOV_H_DEG = 75.0    # типичный дрон (DJI-класс), горизонтальное поле

CLIP_INTRINSICS = {
    # телефонные съёмки
    "VID_20230624_145515": from_fov(1920, 1080, PHONE_FOV_H_DEG, "прикидка",
                                     "смартфон, типичный FOV 67°"),
    "VID_20250822_163723": from_fov(1920, 1080, PHONE_FOV_H_DEG, "прикидка",
                                     "смартфон, типичный FOV 67°"),
    # ютубные, съёмка с дрона
    "YT_Primbee_Speed_Windsurfing_8bYtDBZkrpM": from_fov(1920, 1012, DRONE_FOV_H_DEG, "прикидка",
                                                          "дрон, типичный FOV 75°; кадр 1920x1012"),
    "YT_best_windsurf_racing_bp6nX64OeI0": from_fov(1920, 1080, DRONE_FOV_H_DEG, "прикидка",
                                                     "компиляция, преимущественно дрон, FOV 75°"),
}

DEFAULT_INTRINSICS = from_fov(1920, 1080, DRONE_FOV_H_DEG, "прикидка (по умолчанию)",
                               "клип не найден в таблице")


def intrinsics_for(clip_or_stem: str, frame_w: int = None, frame_h: int = None) -> Intrinsics:
    """Подбор по префиксу имени клипа/кадра. Если размер кадра передан и
    отличается от табличного — центр пересчитывается, f_x остаётся (он
    свойство объектива, а не обрезки)."""
    intr = DEFAULT_INTRINSICS
    for key, value in CLIP_INTRINSICS.items():
        if clip_or_stem.startswith(key):
            intr = value
            break
    if (frame_w is None) != (frame_h is None):
        # половина размера кадра — не размер кадра: молча оставить старый
        # центр по одной оси значит выдать intrinsics, которых нет ни у
        # одной камеры, и обнаружится это только смещением углов
        raise ValueError("frame_w и frame_h задаются только вместе")
    if frame_w and frame_h:
        intr = intr._replace(cx=frame_w / 2.0, cy=frame_h / 2.0)
    return intr
