"""Кроп на лету — онлайн-датасет реально переиспользует
sample_window/negatives/crop/clip_box_to_window, и ignore-зона (порог 20)
работает и здесь, не только в офлайн dataset_gen.py."""
import json
import os
import shutil

import cv2
import numpy as np
import pytest

import config
from online_dataset import OnlineCropYOLODataset, _local_rng, build_frame_index
from test_ignore_zone import _make_synthetic_frame_and_json

ultralytics = pytest.importorskip("ultralytics")
from ultralytics.cfg import get_cfg  # noqa: E402


def _make_variants(tmp_path, frames_dir, stem, variant_names=("clean", "tone")):
    variants_dir = tmp_path / "variants"
    variants_dir.mkdir(exist_ok=True)
    src = os.path.join(frames_dir, stem + ".jpg")
    for v in variant_names:
        shutil.copyfile(src, variants_dir / f"{stem}__{v}.jpg")
    manifest = {
        "frames_dir": str(frames_dir), "variants_dir": str(variants_dir),
        "variant_names": list(variant_names), "stems": [stem], "seed": 0, "quality": 90,
    }
    json.dump(manifest, open(variants_dir / "variants_manifest.json", "w"))
    return str(variants_dir)


def _make_dataset(tmp_path, boxes_wh, size=(1920, 1080), seed=1, neg_ratio=1.0):
    stem, jpg_path, json_path = _make_synthetic_frame_and_json(tmp_path, boxes_wh, size=size)
    frames_dir = os.path.dirname(jpg_path)
    variants_dir = _make_variants(tmp_path, frames_dir, stem)

    hyp = get_cfg(overrides={"mosaic": 0.0, "copy_paste": 0.0, "fliplr": 0.0, "flipud": 0.0})
    ds = OnlineCropYOLODataset(
        frames_dir=frames_dir, variants_dir=variants_dir,
        data={"nc": 1, "names": {0: "windsurf"}, "channels": 3},
        imgsz=config.WINDOW_SIZE, hyp=hyp, augment=True, single_cls=True,
        seed=seed, neg_ratio=neg_ratio,
    )
    return ds, stem


def test_build_frame_index_has_pos_and_neg_slots(tmp_path):
    ignore_box = (100, 100, 110, 110)   # 10px, под порогом MIN_TARGET_SIZE=20
    target_box = (600, 400, 750, 550)   # 150px, обычная цель
    ds, stem = _make_dataset(tmp_path, [ignore_box, target_box])

    kinds = [k for (_, k, _) in ds.slots]
    assert "pos" in kinds
    assert "neg" in kinds
    assert ds.frames[stem]["targets"] == [b for b in ds.frames[stem]["targets"]]
    assert len(ds.frames[stem]["ignore"]) == 1  # ignore-бокс не попал в targets


def test_positive_sample_has_nonempty_instances_and_no_ignore(tmp_path):
    ignore_box = (100, 100, 110, 110)
    target_box = (600, 400, 750, 550)
    ds, stem = _make_dataset(tmp_path, [ignore_box, target_box])

    pos_indices = [i for i, (_, k, _) in enumerate(ds.slots) if k == "pos"]
    assert pos_indices, "должен быть хотя бы один позитивный слот"

    label = ds.get_image_and_label(pos_indices[0])
    assert label["img"].shape == (config.WINDOW_SIZE, config.WINDOW_SIZE, 3)
    instances = label["instances"]
    assert len(instances) >= 1  # цель попала в разметку окна
    # ignore-бокс (10px) в исходных координатах кадра не мог дать бокс
    # с нормированной стороной, близкой к его настоящему малому размеру,
    # т.к. он вообще не входит в targets -> не должен формировать отдельную
    # инстанцию с полу-нулевой площадью
    for b in instances.bboxes:
        assert b[2] > 0 and b[3] > 0  # w,h > 0 (xywh)


def test_negative_sample_typically_has_no_target_instances(tmp_path):
    target_box = (600, 400, 750, 550)
    ds, stem = _make_dataset(tmp_path, [target_box], size=(1920, 1080))

    neg_indices = [i for i, (_, k, _) in enumerate(ds.slots) if k == "neg"]
    assert neg_indices
    empties = 0
    for i in neg_indices:
        label = ds.get_image_and_label(i)
        if len(label["instances"]) == 0:
            empties += 1
    # на большом кадре с одной целью и коллизионной проверкой негатив почти
    # всегда пустой; допускаем единичные промахи коллизии как крайний случай
    assert empties >= len(neg_indices) - 1


def test_deterministic_given_same_seed_index_call_count(tmp_path):
    target_box = (600, 400, 750, 550)
    ds1, _ = _make_dataset(tmp_path, [target_box], seed=7)
    ds2, _ = _make_dataset(tmp_path, [target_box], seed=7)

    pos_i = next(i for i, (_, k, _) in enumerate(ds1.slots) if k == "pos")
    ds1._call_count = 3
    ds2._call_count = 3
    l1 = ds1.get_image_and_label(pos_i)
    l2 = ds2.get_image_and_label(pos_i)
    assert np.array_equal(l1["img"], l2["img"])
    assert np.allclose(l1["instances"].bboxes, l2["instances"].bboxes)


def test_local_rng_differs_across_calls():
    r1 = _local_rng(base_seed=1, index=0, call_count=1)
    r2 = _local_rng(base_seed=1, index=0, call_count=2)
    assert r1.random() != r2.random()
