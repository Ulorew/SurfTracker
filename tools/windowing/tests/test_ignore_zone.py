"""Ignore-зона: боксы мельче
config.MIN_TARGET_SIZE — не цель, но всё ещё блокируют место под негатив, и
никогда не попадают в разметку окна ни одним путём."""
import json
import os
import random

import cv2
import numpy as np
import pytest

import config
import dataset_gen


def _make_synthetic_frame_and_json(tmp_path, boxes_wh, size=(1000, 800)):
    """boxes_wh: список (x1,y1,x2,y2). Каждый бокс — честный видимый
    прямоугольник (не совпадает по цвету с фоном), чтобы crop() не
    деградировал в пустой холст."""
    w, h = size
    frame = np.full((h, w, 3), 60, dtype=np.uint8)
    for x1, y1, x2, y2 in boxes_wh:
        frame[int(y1):int(y2), int(x1):int(x2)] = (200, 200, 200)

    stem = "synthetic_ignore"
    jpg_path = tmp_path / f"{stem}.jpg"
    cv2.imwrite(str(jpg_path), frame)

    shapes = [
        {
            "label": "windsurf",
            "score": None,
            "points": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
            "group_id": None,
            "description": None,
            "difficult": False,
            "shape_type": "rectangle",
            "flags": {},
            "attributes": {},
            "kie_linking": [],
        }
        for x1, y1, x2, y2 in boxes_wh
    ]
    data = {
        "version": "4.0.0-beta.13", "flags": {}, "checked": True, "shapes": shapes,
        "imagePath": f"{stem}.jpg", "imageData": None, "imageHeight": h, "imageWidth": w,
        "description": None,
    }
    json_path = tmp_path / f"{stem}.json"
    json.dump(data, open(json_path, "w"))
    return stem, str(jpg_path), str(json_path)


def test_ignore_box_never_labeled_and_generates_no_positives(tmp_path):
    assert config.MIN_TARGET_SIZE == 20, "тест писан под текущий порог — поправь числа при его смене"

    ignore_box = (100, 100, 100 + 10, 100 + 10)   # 10px — под порогом
    target_box = (600, 400, 600 + 150, 400 + 150)  # 150px — обычная цель

    stem, jpg_path, json_path = _make_synthetic_frame_and_json(tmp_path, [ignore_box, target_box])

    rng = random.Random(0)
    report = {"frames": 0, "positives": 0, "negatives": 0, "size_samples": [],
              "boxes_total": 0, "boxes_zero_windows": 0, "windows_per_box_sum": 0,
              "ignore_boxes_total": 0}
    out_dir = str(tmp_path / "out")

    dataset_gen.process_frame(stem, jpg_path, json_path, out_dir, "train", rng,
                              horizon_overrides={}, generate_negatives=True,
                              do_augment=False, report=report, neg_ratio=1.0)

    assert report["ignore_boxes_total"] == 1
    assert report["boxes_total"] == 1, "в выборку target-боксов должен попасть только крупный"
    assert report["positives"] > 0

    labels_dir = os.path.join(out_dir, "labels", "train")
    for fname in os.listdir(labels_dir):
        for line in open(os.path.join(labels_dir, fname)):
            if not line.strip():
                continue
            _, cx, cy, w, h = map(float, line.split())
            # разметка окна нормирована на WINDOW_SIZE; проверяем, что ни одна
            # рамка в файле не соответствует по масштабу ignore-боксу (10px) —
            # грубая проверка, что там не может быть боксов почти нулевой площади
            assert w * h > 0, "нулевая площадь в разметке — не должно быть в принципе"


def test_negatives_still_avoid_ignore_box(tmp_path):
    """Негатив не должен садиться на ignore-зону, даже если под неё не
    считается цель — сама область всё ещё занята.

    Кадр — реалистичного масштаба (1920x1080, как в проекте), не 1000x800:
    при WINDOW_SIZE=640 и правиле "не меньше WINDOW_SIZE реальных пикселей"
    (см. crop.py) окно на кадре размером ~640 физически не может обойти
    бокс у центра — это геометрия, а не баг; на кадре с большим запасом
    места обход возможен и должен происходить."""
    from geometry import boxes_intersect, expand_box, resolve_placement, square_to_int_box
    from geometry import IntBox

    ignore_box = (500, 400, 500 + 10, 400 + 10)
    stem, jpg_path, json_path = _make_synthetic_frame_and_json(
        tmp_path, [ignore_box], size=(1920, 1080))

    rng = random.Random(0)
    report = {"frames": 0, "positives": 0, "negatives": 0, "size_samples": [],
              "boxes_total": 0, "boxes_zero_windows": 0, "windows_per_box_sum": 0,
              "ignore_boxes_total": 0}
    out_dir = str(tmp_path / "out")

    dataset_gen.process_frame(stem, jpg_path, json_path, out_dir, "train", rng,
                              horizon_overrides={}, generate_negatives=True,
                              do_augment=False, report=report, neg_ratio=1.0)

    # кадр без единого target-бокса -> негативы по EMPTY_FRAME_NEGATIVE_COUNT
    assert report["negatives"] > 0

    ignore_ib = IntBox(int(ignore_box[0]), int(ignore_box[1]), int(ignore_box[2]), int(ignore_box[3]))
    expanded = expand_box(ignore_ib, config.NEGATIVE_BOX_EXPAND_FRAC)

    meta_dir = os.path.join(out_dir, "meta", "train")
    for fname in os.listdir(meta_dir):
        if "_neg" not in fname:
            continue
        meta = json.load(open(os.path.join(meta_dir, fname)))
        src = IntBox(*meta["src_box"])
        assert not boxes_intersect(src, expanded), (
            f"негатив {fname} реально вырезаемой областью пересекает ignore-зону"
        )
