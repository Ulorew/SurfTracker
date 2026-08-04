"""Аугментации поверх готового окна (тикет, п.5).

Работают на BGR uint8 (как отдаёт cv2). Геометрию (отражение) применяем
последней и одновременно поправляем координаты боксов — единственная
аугментация здесь, которая их трогает.

Повороты и mosaic-аугментации НЕ реализуются здесь намеренно: небольшие
повороты (<=5°) и горизонтальный флип уже делает сам ultralytics при
обучении (train.py: degrees=3, fliplr=0.5, flipud=0) — это отдельный,
онлайн-слой поверх офлайн-аугментаций этого модуля, не дублирование бага.
Здесь флип нужен, чтобы разнообразие уже было запечено в сгенерированных
окнах (например, для контроля дистрибуции без опоры на train-time рандом).
"""

import random

import cv2
import numpy as np

import config


def _brightness(img: np.ndarray, rng: random.Random) -> np.ndarray:
    delta = rng.uniform(*config.BRIGHTNESS_DELTA_RANGE) * 255.0
    return np.clip(img.astype(np.float32) + delta, 0, 255)


def _contrast(img: np.ndarray, rng: random.Random) -> np.ndarray:
    factor = rng.uniform(*config.CONTRAST_FACTOR_RANGE)
    mean = float(img.mean())
    return np.clip((img - mean) * factor + mean, 0, 255)


def _gamma(img: np.ndarray, rng: random.Random) -> np.ndarray:
    gamma = rng.uniform(*config.GAMMA_RANGE)
    normalized = np.clip(img, 0, 255) / 255.0
    return np.clip(np.power(normalized, gamma) * 255.0, 0, 255)


def _white_balance(img: np.ndarray, rng: random.Random) -> np.ndarray:
    # BGR: независимый мягкий множитель на B и R, G — опорный канал.
    gain_b = rng.uniform(*config.WHITE_BALANCE_GAIN_RANGE)
    gain_r = rng.uniform(*config.WHITE_BALANCE_GAIN_RANGE)
    out = img.copy()
    out[..., 0] *= gain_b  # B
    out[..., 2] *= gain_r  # R
    return np.clip(out, 0, 255)


def _hue_shift(img_uint8: np.ndarray, rng: random.Random) -> np.ndarray:
    degrees = rng.uniform(*config.HUE_SHIFT_DEGREES_RANGE)
    shift_cv_units = degrees / 2.0  # OpenCV H в диапазоне [0,180) на 360°
    hsv = cv2.cvtColor(img_uint8, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[..., 0] = (hsv[..., 0] + shift_cv_units) % 180.0
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)


def _noise(img_uint8: np.ndarray, rng: random.Random) -> np.ndarray:
    std = rng.uniform(*config.NOISE_STD_RANGE)
    if std <= 0:
        return img_uint8
    noise = np.random.RandomState(rng.randint(0, 2**31 - 1)).normal(0, std, img_uint8.shape)
    return np.clip(img_uint8.astype(np.float32) + noise, 0, 255).astype(np.uint8)


def _blur(img_uint8: np.ndarray, rng: random.Random) -> np.ndarray:
    k = rng.choice(config.BLUR_KERNEL_CHOICES)
    if k <= 0:
        return img_uint8
    return cv2.GaussianBlur(img_uint8, (k, k), 0)


def _jpeg_recompress(img_uint8: np.ndarray, rng: random.Random) -> np.ndarray:
    quality = rng.randint(*config.JPEG_QUALITY_RANGE)
    ok, buf = cv2.imencode(".jpg", img_uint8, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return img_uint8
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def augment(image: np.ndarray, boxes_xywhn: "list[tuple[float, float, float, float]]", rng: random.Random):
    """boxes_xywhn: список (cx, cy, w, h), нормированные [0,1] (YOLO-формат).

    Возвращает (изображение, боксы) — боксы меняются только флипом.
    """
    img = image.astype(np.float32)
    img = _brightness(img, rng)
    img = _contrast(img, rng)
    img = _gamma(img, rng)
    img = _white_balance(img, rng)
    img = img.astype(np.uint8)

    img = _hue_shift(img, rng)
    img = _noise(img, rng)
    img = _blur(img, rng)
    img = _jpeg_recompress(img, rng)

    boxes = list(boxes_xywhn)
    if rng.random() < config.HFLIP_PROB:
        img = np.ascontiguousarray(img[:, ::-1])
        boxes = [(1.0 - cx, cy, w, h) for (cx, cy, w, h) in boxes]

    return img, boxes
