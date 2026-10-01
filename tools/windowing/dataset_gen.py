#!/usr/bin/env python3
"""Размеченные кадры (X-AnyLabeling json+jpg) -> вырезанные окна YOLO-формата.

    python dataset_gen.py --frames-dir DIR --out-dir DIR
        [--split-file split.csv] [--incomplete-frames-file incomplete.txt]
        [--horizon-overrides horizon.csv] [--seed N] [--augment]

`--split-file` (CSV `stem,split`) и `--incomplete-frames-file` (список
stem'ов, по одному на строку) — списки, которых пока нет (появятся
позже). Параметры проведены через весь конвейер уже сейчас: без
--split-file все окна идут в один сплит config.DEFAULT_SPLIT_NAME, без
--incomplete-frames-file негативы генерируются на всех кадрах.

`--incomplete-frames-file` относится ТОЛЬКО к негативам: такой
кадр может содержать неразмеченный парус, значит область "без целей" на нём
ненадёжна. Позитивы с уже размеченных боксов этого кадра по-прежнему валидны
и генерируются как обычно.
"""

import argparse
import csv
import hashlib
import json
import os
import random
import re

import cv2

import config
from augment import augment as augment_fn
from crop import crop
from geometry import IntBox, resolve_placement
from negatives import sample_negatives
from sample_window import sample_window


def video_prefix(stem: str) -> str:
    return re.sub(r"_f\d+$", "", stem)


def target_size(b: IntBox) -> int:
    return max(b.w, b.h)


def load_frame_boxes(json_path: str):
    """-> (list[IntBox], img_w, img_h) из X-AnyLabeling json."""
    d = json.load(open(json_path))
    boxes = []
    for s in d["shapes"]:
        if s["shape_type"] != "rectangle":
            continue
        (x1, y1), (x2, y2) = s["points"][0], s["points"][2]
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        boxes.append(IntBox(round(x1), round(y1), round(x2), round(y2)))
    return boxes, d["imageWidth"], d["imageHeight"]


def split_target_ignore(boxes: "list[IntBox]"):
    """Боксы мельче config.MIN_TARGET_SIZE — ignore-зона, не цель:
    не позитивы, не подходящее место под
    негатив, не штрафуются как ложные при оценке."""
    targets = [b for b in boxes if target_size(b) >= config.MIN_TARGET_SIZE]
    ignore = [b for b in boxes if target_size(b) < config.MIN_TARGET_SIZE]
    return targets, ignore


def load_split_map(path):
    if not path:
        return {}
    out = {}
    with open(path) as f:
        for row in csv.reader(f):
            if not row or row[0].startswith("#"):
                continue
            out[row[0].strip()] = row[1].strip()
    return out


def load_incomplete_set(path):
    if not path:
        return set()
    with open(path) as f:
        return {line.strip() for line in f if line.strip()}


def load_horizon_overrides(path):
    if not path:
        return {}
    out = {}
    with open(path) as f:
        for row in csv.reader(f):
            if not row or row[0].startswith("#"):
                continue
            out[row[0].strip()] = float(row[1])
    return out


def clip_box_to_window(box_frame: IntBox, placement, window_size: int):
    """box_frame (координаты кадра) -> (cx,cy,w,h) нормированные в холсте окна.

    None, если после обрезки видимая часть меньше config.MIN_VISIBLE_BOX_PX
    по любой оси — это уже неразличимая полоска, а не цель (правило:
    "возвращать координаты всех рамок, попавших в окно").
    """
    ib = placement.src_box
    x0 = max(box_frame.x0, ib.x0)
    y0 = max(box_frame.y0, ib.y0)
    x1 = min(box_frame.x1, ib.x1)
    y1 = min(box_frame.y1, ib.y1)
    if x0 >= x1 or y0 >= y1:
        return None

    lx0 = (x0 - ib.x0) * placement.scale_x + placement.off_x
    lx1 = (x1 - ib.x0) * placement.scale_x + placement.off_x
    ly0 = (y0 - ib.y0) * placement.scale_y + placement.off_y
    ly1 = (y1 - ib.y0) * placement.scale_y + placement.off_y

    if (lx1 - lx0) < config.MIN_VISIBLE_BOX_PX or (ly1 - ly0) < config.MIN_VISIBLE_BOX_PX:
        return None

    lx0, lx1 = max(0.0, lx0), min(float(window_size), lx1)
    ly0, ly1 = max(0.0, ly0), min(float(window_size), ly1)

    cx = (lx0 + lx1) / 2 / window_size
    cy = (ly0 + ly1) / 2 / window_size
    w = (lx1 - lx0) / window_size
    h = (ly1 - ly0) / window_size
    return (cx, cy, w, h)


def stream_rng(seed: int, stem: str, stream: str) -> random.Random:
    """Независимый поток случайности на (кадр, назначение).

    Раньше позитивы, негативы и аугментации тянули из ОДНОГО rng, поэтому
    изменение числа негативов сдвигало розыгрыш позитивов: при одинаковом
    seed и разном --neg-ratio 324 из 327 позитивных меток отличались, то
    есть сравнения "по доле негативов" меняли не только негативы.
    Заодно поток больше не зависит от порядка обработки кадров.
    """
    h = hashlib.sha256(f"{seed}:{stem}:{stream}".encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


# Аугментация — свойство ОБУЧАЮЩЕЙ выборки, а не нарезчика: применять её к
# измерительному сплиту значит мерить не то, чем модель будет пользоваться,
# и сравнивать прогоны с разной случайной порчей вместо одной и той же
# картинки. Раньше --augment применялся к любому сплиту без разбора; на
# практике не выстрелило только потому, что все оконные датасеты состоят из
# одного train, а val (dataset_v6) режется другим инструментом из целых
# кадров.
AUGMENTED_SPLITS = ("train",)


def process_frame(stem, jpg_path, json_path, out_dir, split, seed,
                   horizon_overrides, generate_negatives, do_augment, report, neg_ratio=1.0):
    do_augment = do_augment and split in AUGMENTED_SPLITS
    report.setdefault("augmented_splits", set())
    if do_augment:
        report["augmented_splits"].add(split)
    rng_pos = stream_rng(seed, stem, "positives")
    rng_neg = stream_rng(seed, stem, "negatives")
    rng_aug = stream_rng(seed, stem, "augment")

    boxes, img_w, img_h = load_frame_boxes(json_path)
    targets, ignore = split_target_ignore(boxes)
    frame = cv2.imread(jpg_path)
    assert frame is not None, f"cannot read {jpg_path}"

    prefix = video_prefix(stem)
    horizon_frac = horizon_overrides.get(prefix, config.DEFAULT_HORIZON_Y_FRAC)
    horizon_y = horizon_frac * img_h

    images_dir = os.path.join(out_dir, "images", split)
    labels_dir = os.path.join(out_dir, "labels", split)
    meta_dir = os.path.join(out_dir, "meta", split)
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(labels_dir, exist_ok=True)
    os.makedirs(meta_dir, exist_ok=True)

    def write_window(square, tag, idx):
        placement = resolve_placement(square, img_w, img_h, config.WINDOW_SIZE)
        img = crop(frame, square)
        yolo_boxes = []
        for b in targets:  # ignore-боксы никогда не попадают в разметку окна
            local = clip_box_to_window(b, placement, config.WINDOW_SIZE)
            if local is not None:
                yolo_boxes.append(local)
        if do_augment:
            img, yolo_boxes = augment_fn(img, yolo_boxes, rng_aug)
        name = f"{stem}_{tag}{idx:02d}"
        cv2.imwrite(os.path.join(images_dir, name + ".jpg"), img)
        with open(os.path.join(labels_dir, name + ".txt"), "w") as f:
            for cx, cy, w, h in yolo_boxes:
                f.write(f"0 {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")
        # Метаданные вставки окна в исходный кадр — нужны eval_640.py, чтобы
        # определить положение ложного срабатывания относительно горизонта:
        # у самого окна после вырезки нет понятия "кадр".
        ib = placement.src_box
        meta = {
            "source_frame": stem,
            "video_prefix": prefix,
            "frame_w": img_w,
            "frame_h": img_h,
            "horizon_y": horizon_y,
            "src_box": [ib.x0, ib.y0, ib.x1, ib.y1],
            "scale_x": placement.scale_x,
            "scale_y": placement.scale_y,
            "off_x": placement.off_x,
            "off_y": placement.off_y,
        }
        with open(os.path.join(meta_dir, name + ".meta.json"), "w") as f:
            json.dump(meta, f)
        if tag == "pos":
            for cx, cy, w, h in yolo_boxes:
                report["size_samples"].append(max(w, h) * config.WINDOW_SIZE)

    n_pos = 0
    for b in targets:
        windows = sample_window(b, rng_pos)
        report["boxes_total"] += 1
        if not windows:
            report["boxes_zero_windows"] += 1
        report["windows_per_box_sum"] += len(windows)
        for sq in windows:
            write_window(sq, "pos", n_pos)
            n_pos += 1

    n_neg = 0
    if generate_negatives:
        base = n_pos if n_pos > 0 else config.EMPTY_FRAME_NEGATIVE_COUNT
        neg_count = round(base * neg_ratio)
        # и target, и ignore блокируют место под негатив —
        # ignore-зона не подтверждённый фон, туда негатив ставить нельзя.
        neg_squares = sample_negatives(img_w, img_h, targets + ignore, neg_count, rng_neg,
                                       horizon_y=horizon_y)
        for i, sq in enumerate(neg_squares):
            write_window(sq, "neg", i)
        n_neg = len(neg_squares)

    report["frames"] += 1
    report["positives"] += n_pos
    report["negatives"] += n_neg
    report["ignore_boxes_total"] += len(ignore)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--split-file", default=None)
    ap.add_argument("--incomplete-frames-file", default=None)
    ap.add_argument("--horizon-overrides", default=None)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--augment", action="store_true")
    ap.add_argument("--neg-ratio", type=float, default=1.0,
                     help="множитель числа негативов относительно позитивов (и относительно "
                          "config.EMPTY_FRAME_NEGATIVE_COUNT на пустых кадрах)")
    ap.add_argument("--size-bins-floor", type=float, default=None,
                     help="переопределить нижнюю границу нижней корзины config.SIZE_BINS "
                          "(по умолчанию — как в config.py)")
    args = ap.parse_args()

    if args.size_bins_floor is not None:
        lo, hi, frac = config.SIZE_BINS[0]
        config.SIZE_BINS[0] = (args.size_bins_floor, hi, frac)

    split_map = load_split_map(args.split_file)
    incomplete = load_incomplete_set(args.incomplete_frames_file)
    horizon_overrides = load_horizon_overrides(args.horizon_overrides)

    jsons = sorted(f for f in os.listdir(args.frames_dir) if f.endswith(".json"))
    report = {"frames": 0, "positives": 0, "negatives": 0, "size_samples": [],
              "boxes_total": 0, "boxes_zero_windows": 0, "windows_per_box_sum": 0,
              "ignore_boxes_total": 0}

    # Отпечаток версии разметки — хеш
    # содержимого всех json, реально вошедших в нарезку. Меняется, если хоть
    # один бокс поправили руками, даже если состав файлов тот же.
    labeling_hasher = hashlib.sha256()
    for jf in jsons:
        labeling_hasher.update(jf.encode())
        labeling_hasher.update(open(os.path.join(args.frames_dir, jf), "rb").read())
    report["labeling_version"] = labeling_hasher.hexdigest()[:16]

    for jf in jsons:
        stem = jf[:-5]
        jpg_path = os.path.join(args.frames_dir, stem + ".jpg")
        if not os.path.exists(jpg_path):
            continue
        split = split_map.get(stem, config.DEFAULT_SPLIT_NAME)
        generate_negatives = stem not in incomplete
        process_frame(stem, jpg_path, os.path.join(args.frames_dir, jf), args.out_dir,
                      split, args.seed, horizon_overrides, generate_negatives, args.augment, report,
                      neg_ratio=args.neg_ratio)

    # список, а не множество — для json; пусто == аугментации не было нигде
    report["augmented_splits"] = sorted(report.get("augmented_splits", set()))

    sizes = report.pop("size_samples")
    hist = {}
    for lo, hi, frac in config.SIZE_BINS:
        count = sum(1 for s in sizes if lo <= s < hi)
        hist[f"{lo}-{hi}"] = {"target_frac": frac, "count": count}
    report["size_distribution"] = hist
    report["size_samples_total"] = len(sizes)

    boxes_total = report["boxes_total"]
    report["avg_windows_per_box"] = (report["windows_per_box_sum"] / boxes_total) if boxes_total else None
    report.pop("windows_per_box_sum")

    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "report.json"), "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
