#!/usr/bin/env python3
"""Офлайн-заготовка для кропа на лету (тикет "патч v2", п.5, вариант D).

Для каждого исходного кадра готовим несколько ПОЛНОКАДРОВЫХ версий:
"чистая" (оригинал, symlink — без затрат диска) + 2-3 версии с запечёнными
фильтрами из augment.py, сгруппированными по смыслу (не все 7 фильтров сразу
в одну версию — так теряется контроль, какая версия что даёт):

    tone     — brightness, contrast, gamma, white_balance, hue_shift
    degrade  — noise, затем финальный jpeg на диске (естественно деградирует)
    blur     — gaussian blur

Кроп и флип остаются онлайн (см. online_dataset.py) — здесь фильтры
применяются к кадру ЦЕЛИКОМ один раз, не к каждому окну.

    python online_variants.py --frames-dir DIR --out-dir DIR [--seed 0]
        [--quality 90] [--dry-run]

--dry-run печатает объём диска (кол-во кадров x версий, средний/итоговый
размер по выборке) и не пишет файлы — ровно то, что тикет просит "посчитать
и доложить до генерации".
"""

import argparse
import json
import os
import random

import cv2
import numpy as np

from augment import _blur, _brightness, _contrast, _gamma, _hue_shift, _noise, _white_balance

VARIANT_NAMES = ["clean", "tone", "degrade", "blur"]


def _tone(img: np.ndarray, rng: random.Random) -> np.ndarray:
    f = img.astype(np.float32)
    f = _brightness(f, rng)
    f = _contrast(f, rng)
    f = _gamma(f, rng)
    f = _white_balance(f, rng)
    f = f.astype(np.uint8)
    return _hue_shift(f, rng)


def _degrade(img: np.ndarray, rng: random.Random) -> np.ndarray:
    return _noise(img, rng)


def build_variant(name: str, img: np.ndarray, rng: random.Random) -> "np.ndarray | None":
    """None для 'clean' — вызывающий код должен использовать symlink на оригинал."""
    if name == "clean":
        return None
    if name == "tone":
        return _tone(img, rng)
    if name == "degrade":
        return _degrade(img, rng)
    if name == "blur":
        return _blur(img, rng)
    raise ValueError(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames-dir", required=True, help="папка с исходными кадрами (*.jpg/*.json)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--quality", type=int, default=90)
    ap.add_argument("--sample", type=int, default=None,
                     help="ограничить числом кадров (для --dry-run на выборке, не на всех 598)")
    ap.add_argument("--dry-run", action="store_true", help="только посчитать объём, не писать файлы")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    stems = sorted(f[:-4] for f in os.listdir(args.frames_dir) if f.endswith(".jpg"))
    if args.sample:
        stems = random.Random(0).sample(stems, min(args.sample, len(stems)))

    if not args.dry_run:
        os.makedirs(args.out_dir, exist_ok=True)

    total_bytes = 0
    per_variant_bytes = {v: 0 for v in VARIANT_NAMES}
    n_done = 0

    for stem in stems:
        src_jpg = os.path.join(args.frames_dir, stem + ".jpg")
        if not os.path.exists(src_jpg):
            continue
        clean_size = os.path.getsize(src_jpg)
        per_variant_bytes["clean"] += clean_size  # symlink почти бесплатен на диске, но считаем честно как ссылку на этот объём
        total_bytes += 0  # clean не добавляет нового объёма (symlink)

        img = None
        for variant in VARIANT_NAMES[1:]:
            if args.dry_run:
                if img is None:
                    img = cv2.imread(src_jpg)
                out_img = build_variant(variant, img, rng)
                ok, buf = cv2.imencode(".jpg", out_img, [cv2.IMWRITE_JPEG_QUALITY, args.quality])
                size = len(buf)
            else:
                if img is None:
                    img = cv2.imread(src_jpg)
                out_img = build_variant(variant, img, rng)
                out_path = os.path.join(args.out_dir, f"{stem}__{variant}.jpg")
                cv2.imwrite(out_path, out_img, [cv2.IMWRITE_JPEG_QUALITY, args.quality])
                size = os.path.getsize(out_path)
            per_variant_bytes[variant] += size
            total_bytes += size

        if not args.dry_run:
            clean_path = os.path.join(args.out_dir, f"{stem}__clean.jpg")
            if os.path.lexists(clean_path):
                os.remove(clean_path)
            os.symlink(os.path.abspath(src_jpg), clean_path)

        n_done += 1

    report = {
        "n_frames": n_done,
        "quality": args.quality,
        "variant_names": VARIANT_NAMES,
        "new_bytes_total": total_bytes,
        "new_mb_total": round(total_bytes / 1024 / 1024, 1),
        "per_variant_avg_kb": {v: round((per_variant_bytes[v] / n_done) / 1024, 1) if n_done else 0
                                for v in VARIANT_NAMES if v != "clean"},
        "dry_run": args.dry_run,
        "sampled": args.sample is not None,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))

    if not args.dry_run:
        variants_manifest = {
            "frames_dir": os.path.abspath(args.frames_dir),
            "variants_dir": os.path.abspath(args.out_dir),
            "variant_names": VARIANT_NAMES,
            "stems": stems,
            "seed": args.seed,
            "quality": args.quality,
        }
        with open(os.path.join(args.out_dir, "variants_manifest.json"), "w") as f:
            json.dump(variants_manifest, f, indent=2)


if __name__ == "__main__":
    main()
