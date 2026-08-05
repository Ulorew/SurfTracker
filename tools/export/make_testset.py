#!/usr/bin/env python3
"""Фиксированный набор из 20 кадров для сверки ноутбук <-> экспорт <-> телефон
(тикет "экспорт на телефон", п.2-3).

Кадры — не случайные картинки, а ровно то, что модель видит в проде: окна
640x640, вырезанные тем же crop(), что и в петле слежения, вокруг размеченных
целей. Сверять на полных кадрах 1920x1080 было бы сверкой другого режима:
на телефоне в сеть пойдёт окно, а не кадр.

PNG, а не JPEG: сверка с телефоном должна упираться в арифметику инференса,
а не в то, что JPEG-декодеры на двух устройствах дали разные пиксели.

    python make_testset.py --out ../../export/testset
"""
import argparse
import json
import os
import random
import sys

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "windowing"))

import config  # noqa: E402
from crop import crop  # noqa: E402
from geometry import Square, resolve_placement  # noqa: E402

FRAMES_ROOT = os.path.join(HERE, "..", "..", "Data", "frames", "val_manual")


def labeled_boxes(json_path):
    """Прямоугольники разметки крупнее порога ignore-зоны."""
    d = json.load(open(json_path))
    out = []
    for s in d.get("shapes", []):
        if s.get("shape_type") != "rectangle":
            continue
        (x1, y1), (x2, y2) = s["points"][0], s["points"][2]
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        if max(x2 - x1, y2 - y1) < config.MIN_TARGET_SIZE:
            continue
        out.append((x1, y1, x2, y2))
    return out


def clip_to_window(box, placement, side):
    x0 = max(box[0], placement.src_box.x0)
    y0 = max(box[1], placement.src_box.y0)
    x1 = min(box[2], placement.src_box.x1)
    y1 = min(box[3], placement.src_box.y1)
    if x0 >= x1 or y0 >= y1:
        return None
    lx0 = (x0 - placement.src_box.x0) * placement.scale_x + placement.off_x
    lx1 = (x1 - placement.src_box.x0) * placement.scale_x + placement.off_x
    ly0 = (y0 - placement.src_box.y0) * placement.scale_y + placement.off_y
    ly1 = (y1 - placement.src_box.y0) * placement.scale_y + placement.off_y
    if min(lx1 - lx0, ly1 - ly0) < config.MIN_VISIBLE_BOX_PX:
        return None
    return [round(v, 2) for v in (lx0, ly0, lx1, ly1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--k", type=float, default=3.5, help="окно = k * размер цели")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    os.makedirs(args.out, exist_ok=True)

    # собираем кандидатов из всех проездов, по одному окну на размеченный кадр,
    # чтобы набор не оказался двадцатью соседними тактами одного проезда
    candidates = []
    for clip in sorted(os.listdir(FRAMES_ROOT)):
        d = os.path.join(FRAMES_ROOT, clip)
        if not os.path.isdir(d):
            continue
        for jf in sorted(f for f in os.listdir(d) if f.endswith(".json")):
            jpg = os.path.join(d, jf[:-5] + ".jpg")
            if not os.path.exists(jpg):
                continue
            boxes = labeled_boxes(os.path.join(d, jf))
            if boxes:
                candidates.append((clip, jpg, boxes))
    assert candidates, f"нет размеченных кадров в {FRAMES_ROOT}"

    by_clip = {}
    for c in candidates:
        by_clip.setdefault(c[0], []).append(c)
    # равномерно по проездам: набор должен покрывать все источники, иначе
    # сверка молча проверит один тип съёмки
    picked, i = [], 0
    while len(picked) < args.count:
        clips = sorted(by_clip)
        clip = clips[i % len(clips)]
        pool = by_clip[clip]
        if pool:
            picked.append(pool.pop(rng.randrange(len(pool))))
        i += 1
        if all(not v for v in by_clip.values()):
            break

    items = []
    for n, (clip, jpg, boxes) in enumerate(picked):
        frame = cv2.imread(jpg)
        b = max(boxes, key=lambda t: max(t[2] - t[0], t[3] - t[1]))
        size = max(b[2] - b[0], b[3] - b[1])
        side = max(args.k * size, config.WINDOW_SIZE)
        sq = Square(cx=(b[0] + b[2]) / 2, cy=(b[1] + b[3]) / 2, side=side)
        pl = resolve_placement(sq, frame.shape[1], frame.shape[0], config.WINDOW_SIZE)
        win = crop(frame, sq)
        assert win.shape == (config.WINDOW_SIZE, config.WINDOW_SIZE, 3), win.shape
        name = f"{n:02d}_{clip[:28]}.png"
        cv2.imwrite(os.path.join(args.out, name), win)
        gt = [g for g in (clip_to_window(x, pl, config.WINDOW_SIZE) for x in boxes) if g]
        items.append({"name": name, "source": os.path.relpath(jpg, HERE),
                       "clip": clip, "window_side_px": round(side, 1),
                       "gt_xyxy": gt})

    meta = {"count": len(items), "imgsz": config.WINDOW_SIZE,
            "формат": "PNG BGR, как отдаёт cv2.imread; препроцессинг — забота раннера",
            "items": items}
    with open(os.path.join(args.out, "testset.json"), "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"кадров: {len(items)} -> {args.out}")
    print(f"  с разметкой в окне: {sum(1 for i in items if i['gt_xyxy'])}, "
          f"рамок всего: {sum(len(i['gt_xyxy']) for i in items)}")
    print(f"  проездов покрыто: {len({i['clip'] for i in items})}")


if __name__ == "__main__":
    main()
