"""Позитивные окна для обучения — от размеченной рамки (тикет, п.2-3).

Матчасть (почему бокс может "не закрывать" корзину):
    crop() рисует окно стороной S = k * B (B — натуральный размер цели, px)
    в холст WINDOW_SIZE^2. Если S > WINDOW_SIZE, окно уменьшается, и видимый
    размер цели в готовой картинке V = B * WINDOW_SIZE / S = WINDOW_SIZE / k
    — не зависит от B напрямую, только от k. Если S <= WINDOW_SIZE, ресайза
    нет (запрет на увеличение, см. crop.py) — видимый размер остаётся V = B,
    и k тут ни при чём (влияет только на контекст вокруг цели).

    Два независимых пути закрыть корзину [lo, hi):
      - НАТУРА: сам бокс B уже внутри [lo, hi), и есть k из [K_MIN, K_MAX],
        при котором S=k*B ещё <= WINDOW_SIZE (т.е. K_MIN*B <= WINDOW_SIZE,
        B <= WINDOW_SIZE/K_MIN = 256 px) — берём максимум контекста, не
        переходя в ресайз;
      - РЕСАЙЗ: случайная цель V внутри корзины, k=WINDOW_SIZE/V из
        [K_MIN, K_MAX], и получившийся S=k*B в самом деле больше WINDOW_SIZE
        (иначе это уже предыдущий случай, для которого B в корзину не попал).
      Если ни один путь не сработал — бокс корзину не закрывает.

    При WINDOW_SIZE=640 и K_MIN=2.5 предельный видимый размер, достижимый
    ресайзом, — 640/2.5=256 px: корзина 320-480 при этих числах не
    закрывается вообще ни одним путём (см. предупреждение у K_MIN в config.py).
"""

import random

import config
from geometry import IntBox, Square, box_center


def _make_window(cx: float, cy: float, side: float, rng: random.Random) -> Square:
    jitter = config.CENTER_JITTER_FRAC * side
    jx = rng.uniform(-jitter, jitter)
    jy = rng.uniform(-jitter, jitter)
    return Square(cx=cx + jx, cy=cy + jy, side=side)


def _reachable_sides(b_size: float, rng: random.Random) -> "list[float]":
    """Стороны S окон, закрывающих хотя бы одну корзину, для бокса b_size."""
    bins = list(config.SIZE_BINS)
    rng.shuffle(bins)  # иначе первые корзины в списке систематически
                        # выигрывали бы при обрезке по cap

    sides = []
    for lo, hi, _frac in bins:
        # путь 1: натура (без ресайза) — бокс уже в этой корзине
        if lo <= b_size < hi and config.K_MIN * b_size <= config.WINDOW_SIZE:
            k = min(config.K_MAX, config.WINDOW_SIZE / b_size)
            sides.append(k * b_size)
            continue

        # путь 2: ресайз — целимся в случайную точку корзины
        v = rng.uniform(lo, hi)
        k = config.WINDOW_SIZE / v
        if not (config.K_MIN <= k <= config.K_MAX):
            continue
        s = k * b_size
        if s <= config.WINDOW_SIZE:
            continue  # не настоящий ресайз — тот же случай, что путь 1, но не подошёл
        sides.append(s)

    return sides


def sample_window(box: IntBox, rng: random.Random) -> "list[Square]":
    """Возвращает окна (Square) под все корзины, которые этот бокс закрывает.

    Не более config.MAX_WINDOWS_PER_BOX окон с бокса; для мелких боксов
    (сторона < config.SMALL_BOX_THRESHOLD_PX) лимит подняли до
    config.SMALL_BOX_MAX_WINDOWS — им, как правило, доступна только одна
    корзина (собственный натуральный размер), это компенсируется повторной
    выборкой того же окна с другим джиттером.
    """
    b_size = max(box.w, box.h)
    cx, cy = box_center(box)
    is_small = b_size < config.SMALL_BOX_THRESHOLD_PX
    cap = config.SMALL_BOX_MAX_WINDOWS if is_small else config.MAX_WINDOWS_PER_BOX

    reachable = _reachable_sides(b_size, rng)
    if not reachable:
        return []

    windows = [_make_window(cx, cy, s, rng) for s in reachable[:cap]]

    if is_small:
        i = 0
        while len(windows) < cap:
            s = reachable[i % len(reachable)]
            windows.append(_make_window(cx, cy, s, rng))
            i += 1

    return windows
