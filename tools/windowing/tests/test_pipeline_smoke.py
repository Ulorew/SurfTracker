import glob
import os
import random

import cv2
import pytest

import config
import dataset_gen

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
FRAMES_DIR = os.path.join(
    PROJECT_ROOT, "Data", "frames", "_archive", "labeled_json_backup_20260802",
)
IMAGES_DIR = os.path.join(PROJECT_ROOT, "Data", "frames", "labeled")


def _pick_ten_frames_with_boxes():
    """10 реальных кадров, у которых точно есть хотя бы один бокс — иначе
    сквозной прогон в основном тестирует пустые кадры."""
    picked = []
    for jf in sorted(glob.glob(os.path.join(FRAMES_DIR, "*.json"))):
        stem = os.path.splitext(os.path.basename(jf))[0]
        jpg = os.path.join(IMAGES_DIR, stem + ".jpg")
        if not os.path.exists(jpg):
            continue
        boxes, _w, _h = dataset_gen.load_frame_boxes(jf)
        if boxes:
            picked.append((stem, jpg, jf))
        if len(picked) >= 10:
            break
    return picked


@pytest.mark.skipif(
    not os.path.isdir(FRAMES_DIR) or not os.path.isdir(IMAGES_DIR),
    reason="нет реальных кадров проекта рядом",
)
def test_end_to_end_on_ten_real_frames(tmp_path):
    frames = _pick_ten_frames_with_boxes()
    assert len(frames) >= 5, "мало реальных кадров с боксами для сквозного теста"

    rng = random.Random(0)
    report = {"frames": 0, "positives": 0, "negatives": 0, "size_samples": [],
              "boxes_total": 0, "boxes_zero_windows": 0, "windows_per_box_sum": 0,
              "ignore_boxes_total": 0}
    out_dir = str(tmp_path / "windows")

    for stem, jpg_path, json_path in frames:
        dataset_gen.process_frame(
            stem, jpg_path, json_path, out_dir, config.DEFAULT_SPLIT_NAME, rng,
            horizon_overrides={}, generate_negatives=True, do_augment=True, report=report,
        )

    assert report["frames"] == len(frames)
    assert report["positives"] > 0
    assert report["negatives"] > 0

    images_dir = os.path.join(out_dir, "images", config.DEFAULT_SPLIT_NAME)
    labels_dir = os.path.join(out_dir, "labels", config.DEFAULT_SPLIT_NAME)
    meta_dir = os.path.join(out_dir, "meta", config.DEFAULT_SPLIT_NAME)

    jpgs = sorted(f for f in os.listdir(images_dir) if f.endswith(".jpg"))
    assert len(jpgs) == report["positives"] + report["negatives"]

    for name in jpgs:
        img = cv2.imread(os.path.join(images_dir, name))
        assert img.shape == (config.WINDOW_SIZE, config.WINDOW_SIZE, 3)

        stem_ = os.path.splitext(name)[0]
        label_path = os.path.join(labels_dir, stem_ + ".txt")
        assert os.path.exists(label_path)
        for line in open(label_path):
            if not line.strip():
                continue
            cls, cx, cy, w, h = line.split()
            assert cls == "0"
            for v in (cx, cy, w, h):
                assert 0.0 <= float(v) <= 1.0

        assert os.path.exists(os.path.join(meta_dir, stem_ + ".meta.json"))
