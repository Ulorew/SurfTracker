#!/usr/bin/env python3
"""Равномерная нарезка: ровно N кадров из видео, без фильтров дублей/смаза.

Использование:
    python tools/extract_uniform.py VIDEO OUTDIR --count 100 [--stem NAME]

Кадры именуются <stem>_f<frame_index:06d>.jpg, строки манифеста пишутся в stdout
в формате image,video,frame_index,timestamp_sec (склеивает вызывающий).
Если OpenCV не может декодировать (например AV1) — падает обратно на ffmpeg
с фильтром select по списку индексов.
"""
import argparse
import os
import subprocess
import sys

import cv2


def uniform_indices(total: int, count: int) -> list[int]:
    if total <= count:
        return list(range(total))
    return sorted({round(i * total / count) for i in range(count)} - {total})


def extract_opencv(path: str, outdir: str, stem: str, indices: list[int], fps: float) -> list[tuple]:
    cap = cv2.VideoCapture(path)
    want = set(indices)
    last = max(want)
    rows = []
    idx = 0
    while idx <= last:
        if idx in want:
            ok, frame = cap.read()
            if not ok:
                break
            name = f"{stem}_f{idx:06d}.jpg"
            cv2.imwrite(os.path.join(outdir, name), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            rows.append((name, os.path.basename(path), idx, idx / fps))
        else:
            if not cap.grab():
                break
        idx += 1
    cap.release()
    return rows


def extract_ffmpeg(path: str, outdir: str, stem: str, indices: list[int], fps: float) -> list[tuple]:
    expr = "+".join(f"eq(n\\,{i})" for i in indices)
    tmp_pattern = os.path.join(outdir, f"__tmp_{stem}_%05d.jpg")
    # check=False: ffmpeg 6.1 может упасть (SIGABRT) на EOF уже после записи всех кадров
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-c:v", "libdav1d", "-i", path,
         "-vf", f"select={expr}", "-vsync", "vfr", "-q:v", "2", tmp_pattern],
        check=False,
    )
    rows = []
    for ordinal, idx in enumerate(indices, start=1):
        tmp = os.path.join(outdir, f"__tmp_{stem}_{ordinal:05d}.jpg")
        if not os.path.exists(tmp):
            break
        name = f"{stem}_f{idx:06d}.jpg"
        os.replace(tmp, os.path.join(outdir, name))
        rows.append((name, os.path.basename(path), idx, idx / fps))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("outdir")
    ap.add_argument("--count", type=int, required=True)
    ap.add_argument("--stem", default=None)
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    stem = args.stem or os.path.splitext(os.path.basename(args.video))[0]

    cap = cv2.VideoCapture(args.video)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    ok, _ = cap.read()
    cap.release()

    indices = uniform_indices(total, args.count)
    if ok:
        rows = extract_opencv(args.video, args.outdir, stem, indices, fps)
    else:
        rows = extract_ffmpeg(args.video, args.outdir, stem, indices, fps)

    for name, video, idx, ts in rows:
        print(f"{name},{video},{idx},{ts:.3f}")
    print(f"done {stem}: {len(rows)}/{len(indices)}", file=sys.stderr)


if __name__ == "__main__":
    main()
