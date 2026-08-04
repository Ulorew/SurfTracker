import glob
import json
import os
import random

import pytest

import config
from geometry import IntBox
from sample_window import sample_window

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
BACKUP_DIR = os.path.join(
    PROJECT_ROOT, "Data", "frames", "_archive", "labeled_json_backup_20260802",
)


def _real_native_sizes():
    """Реальные размеры целей проекта — представительнее синтетики: у
    равномерного диапазона своя механика, которая ничего не говорит о том,
    воспроизводит ли код целевое распределение НА РЕАЛЬНЫХ данных.
    """
    sizes = []
    for path in glob.glob(os.path.join(BACKUP_DIR, "*.json")):
        d = json.load(open(path))
        for s in d["shapes"]:
            if s["shape_type"] != "rectangle":
                continue
            (x1, y1), (x2, y2) = s["points"][0], s["points"][2]
            sizes.append(max(abs(x2 - x1), abs(y2 - y1)))
    return sizes


def visible_size(native: float, side: float) -> float:
    return native * min(1.0, config.WINDOW_SIZE / side)


@pytest.mark.skipif(not os.path.isdir(BACKUP_DIR), reason="нет бэкапа реальной разметки")
def test_distribution_covers_reachable_bins_on_real_sizes():
    native_sizes = _real_native_sizes()
    assert len(native_sizes) > 100

    rng = random.Random(0)
    visible = []
    for native in native_sizes:
        box = IntBox(0, 0, round(native), round(native))
        for sq in sample_window(box, rng):
            visible.append(visible_size(native, sq.side))

    assert len(visible) > len(native_sizes), "мало окон на реальных боксах — подозрительно"

    total = len(visible)
    counts = {}
    for lo, hi, _frac in config.SIZE_BINS:
        counts[(lo, hi)] = sum(1 for v in visible if lo <= v < hi)

    # Все корзины (320-480 убрана из SIZE_BINS как структурно недостижимая,
    # см. config.py) достижимы натурой и/или ресайзом — должны получить
    # заметную долю, а не единицы случайных попаданий.
    for lo, hi, _frac in config.SIZE_BINS:
        frac = counts[(lo, hi)] / total
        assert frac > 0.03, f"корзина {lo}-{hi} почти не закрывается ({frac:.3f}) на реальных размерах"
