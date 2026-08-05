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


def _natural_side(b_size: float, lo: float, hi: float) -> "float | None":
    """Путь 1 (натура, без ресайза): бокс уже в корзине и помещается в холст
    при k >= K_MIN. -> сторона окна или None."""
    if lo <= b_size < hi and config.K_MIN * b_size <= config.WINDOW_SIZE:
        return min(config.K_MAX, config.WINDOW_SIZE / b_size) * b_size
    return None


def _resize_visible_range(b_size: float, lo: float, hi: float) -> "tuple[float, float] | None":
    """Путь 2 (ресайз): диапазон ВИДИМЫХ размеров v, при которых окно
    попадает в корзину [lo,hi) и k = WINDOW_SIZE/v остаётся в [K_MIN, K_MAX],
    а вырезка действительно уменьшается (s > WINDOW_SIZE, т.е. v < b_size).

    Диапазон считается ТОЧНО, а не проверяется случайной пробой. Раньше здесь
    бросали v равномерно по всей корзине и отбрасывали неудачные броски —
    из-за этого корзина считалась достижимой лишь с некоторой вероятностью, и
    верхняя корзина систематически недобирала окна: при боксе 400px и корзине
    200-320 годится только v <= 256, то есть примерно половина бросков.
    Хуже того, ответ расходился с reachable_bin_indices, который те же
    корзины считает достижимыми детерминированно.
    """
    v_lo = max(lo, config.WINDOW_SIZE / config.K_MAX)
    v_hi = min(hi, config.WINDOW_SIZE / config.K_MIN, b_size)
    return (v_lo, v_hi) if v_lo < v_hi else None


def _reachable_sides(b_size: float, rng: random.Random) -> "list[tuple[float, float]]":
    """-> [(сторона S, целевая доля корзины), ...] для корзин, которые бокс
    b_size вообще способен закрыть.

    Доля возвращается наружу, потому что раньше она здесь и терялась: тройка
    распаковывалась как `lo, hi, _frac`, и целевое распределение SIZE_BINS не
    влияло на нарезку позитивов ВООБЩЕ. Фактическое распределение получалось
    из одной геометрии — 47% в нижней корзине при заданных 15%, и 12% в
    корзине 120-200 при заданных 35%.
    """
    bins = list(config.SIZE_BINS)
    rng.shuffle(bins)  # порядок не должен систематически влиять на отбор

    sides = []
    for lo, hi, frac in bins:
        nat = _natural_side(b_size, lo, hi)
        if nat is not None:
            sides.append((nat, frac))
            continue
        rng_v = _resize_visible_range(b_size, lo, hi)
        if rng_v is None:
            continue
        v = rng.uniform(*rng_v)
        sides.append(((config.WINDOW_SIZE / v) * b_size, frac))

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

    # Взвешенный выбор корзины по целевым долям (тикет "ночь", п.1.1) вместо
    # прежнего "берём все достижимые подряд". Доли нормируются по тем
    # корзинам, которые ЭТОТ бокс реально может закрыть: недостижимая корзина
    # не должна забирать себе вес, иначе бокс просто недоберёт окон.
    weights = [max(f, 0.0) for _, f in reachable]
    if sum(weights) <= 0:
        weights = [1.0] * len(reachable)

    windows = []
    for _ in range(cap):
        side = rng.choices([s for s, _ in reachable], weights=weights, k=1)[0]
        windows.append(_make_window(cx, cy, side, rng))
        if not is_small and len(windows) >= len(reachable):
            # у крупного бокса корзин много: не размножаем одну и ту же сверх
            # числа доступных, иначе доли вырождаются в шум одного розыгрыша
            break

    return windows


def reachable_bin_indices(b_size: float) -> "list[int]":
    """Индексы корзин config.SIZE_BINS, которые бокс b_size СПОСОБЕН закрыть.

    Детерминированно, без rng — нужно для обратного порядка розыгрыша
    (сначала корзина по целевой доле, потом бокс из способных её закрыть).
    Прямой порядок "бокс -> корзина" целевые доли починить не может: бокс
    можно только УМЕНЬШИТЬ, поэтому мелкая цель достижима лишь в своей
    корзине, и доля крупных корзин определяется составом разметки, а не
    заданием.
    """
    out = []
    for i, (lo, hi, _frac) in enumerate(config.SIZE_BINS):
        if _natural_side(b_size, lo, hi) is not None \
                or _resize_visible_range(b_size, lo, hi) is not None:
            out.append(i)
    return out


def window_for_bin(box: IntBox, bin_idx: int, rng: random.Random) -> "Square | None":
    """Одно окно, целящееся именно в корзину bin_idx (None, если недостижима)."""
    b_size = max(box.w, box.h)
    cx, cy = box_center(box)
    lo, hi, _frac = config.SIZE_BINS[bin_idx]

    nat = _natural_side(b_size, lo, hi)
    if nat is not None:
        return _make_window(cx, cy, nat, rng)

    rng_v = _resize_visible_range(b_size, lo, hi)
    if rng_v is None:
        return None
    v = rng.uniform(*rng_v)
    return _make_window(cx, cy, (config.WINDOW_SIZE / v) * b_size, rng)
