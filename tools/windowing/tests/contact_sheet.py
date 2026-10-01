#!/usr/bin/env python3
"""Простыня из случайных окон — посмотреть глазами.

Не pytest-тест: глаз в цикл CI не воткнёшь, но перед тем как доверять
конвейеру, стоит один раз реально посмотреть на выборку окон с рамками.

    python contact_sheet.py WINDOWS_DIR --split train --n 36 --out sheet.jpg

WINDOWS_DIR — то, что пишет dataset_gen.py (--out-dir): images/<split>,
labels/<split> внутри.
"""

import argparse
import os
import random

import cv2
import numpy as np


def load_yolo_boxes(label_path, w, h):
    boxes = []
    if os.path.exists(label_path):
        for line in open(label_path):
            if not line.strip():
                continue
            _, cx, cy, bw, bh = map(float, line.split())
            x0, y0 = (cx - bw / 2) * w, (cy - bh / 2) * h
            x1, y1 = (cx + bw / 2) * w, (cy + bh / 2) * h
            boxes.append((x0, y0, x1, y1))
    return boxes


def build_contact_sheet(windows_dir, split, n, seed=0, thumb=200, cols=6):
    images_dir = os.path.join(windows_dir, "images", split)
    labels_dir = os.path.join(windows_dir, "labels", split)

    names = sorted(f for f in os.listdir(images_dir) if f.endswith(".jpg"))
    random.Random(seed).shuffle(names)
    names = names[:n]

    rows = (len(names) + cols - 1) // cols
    sheet = np.full((rows * thumb, cols * thumb, 3), 40, dtype=np.uint8)

    for i, name in enumerate(names):
        img = cv2.imread(os.path.join(images_dir, name))
        h, w = img.shape[:2]
        stem = os.path.splitext(name)[0]
        boxes = load_yolo_boxes(os.path.join(labels_dir, stem + ".txt"), w, h)

        is_negative = "_neg" in name
        border = (0, 0, 255) if is_negative else (0, 200, 0)
        for x0, y0, x1, y1 in boxes:
            cv2.rectangle(img, (int(x0), int(y0)), (int(x1), int(y1)), (0, 255, 255), 2)

        thumb_img = cv2.resize(img, (thumb - 4, thumb - 4), interpolation=cv2.INTER_AREA)
        thumb_img = cv2.copyMakeBorder(thumb_img, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=border)

        r, c = divmod(i, cols)
        sheet[r * thumb:(r + 1) * thumb, c * thumb:(c + 1) * thumb] = thumb_img

    return sheet


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("windows_dir")
    ap.add_argument("--split", default="train")
    ap.add_argument("--n", type=int, default=36)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="contact_sheet.jpg")
    args = ap.parse_args()

    sheet = build_contact_sheet(args.windows_dir, args.split, args.n, args.seed)
    cv2.imwrite(args.out, sheet)
    print(f"{args.out} (жёлтые рамки — цели; красная рамка миниатюры — негатив, зелёная — позитив)")
