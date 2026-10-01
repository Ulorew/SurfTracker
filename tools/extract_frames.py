#!/usr/bin/env python3
"""Extract every Nth frame from videos, dropping near-duplicates and blurry frames.

Built for the SurfTracker windsurf dataset: sequential frames of a video are
highly correlated, so a plain "every Nth frame" dump still yields a lot of
near-identical images. Two cheap filters cut that down:

  * dHash Hamming distance vs. recently kept frames  -> drops near-duplicates
  * variance of Laplacian                            -> drops motion blur / defocus

Frames are decoded sequentially using grab() (no decode) + retrieve() (decode
only what we keep), which is far faster and more reliable than seeking.

Examples
--------
  # See what you'd get, without writing anything
  python extract_frames.py --dry-run

  # Default run: every 15th frame, filters on
  python extract_frames.py

  # Time-based step, cap per video, no blur filter
  python extract_frames.py --every-sec 1.0 --max-per-video 100 --blur-thresh 0
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

# Корень репозитория: умолчания путей считаются от него, а не от cwd.
REPO = Path(__file__).resolve().parent.parent

VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".mts", ".webm"}


# --------------------------------------------------------------------------- #
# frame quality / similarity metrics
# --------------------------------------------------------------------------- #

def dhash(gray: np.ndarray, size: int = 8) -> int:
    """64-bit difference hash: compares each pixel to its right neighbour."""
    small = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA)
    bits = small[:, 1:] > small[:, :-1]
    return int("".join("1" if b else "0" for b in bits.flatten()), 2)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def blur_score(gray: np.ndarray) -> float:
    """Variance of the Laplacian. Low = blurry. Scale-dependent, so we
    normalise the input to a fixed height first to keep the threshold
    comparable across videos of different resolutions."""
    h = 480
    if gray.shape[0] != h:
        scale = h / gray.shape[0]
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


# --------------------------------------------------------------------------- #
# per-video extraction
# --------------------------------------------------------------------------- #

@dataclass
class Stats:
    considered: int = 0
    kept: int = 0
    dropped_dup: int = 0
    dropped_blur: int = 0
    blur_scores: list[float] = field(default_factory=list)

    def merge(self, other: "Stats") -> None:
        self.considered += other.considered
        self.kept += other.kept
        self.dropped_dup += other.dropped_dup
        self.dropped_blur += other.dropped_blur
        self.blur_scores.extend(other.blur_scores)


def extract_one(
    video: Path,
    out_dir: Path,
    *,
    step: int | None,
    every_sec: float | None,
    hash_dist: int,
    hash_window: int,
    blur_thresh: float,
    max_per_video: int | None,
    jpeg_quality: int,
    flat: bool,
    dry_run: bool,
    manifest: list[dict],
) -> Stats:
    st = Stats()
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"  !! cannot open {video.name}", file=sys.stderr)
        return st

    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

    if every_sec is not None:
        if fps <= 0:
            print(f"  !! {video.name}: unknown fps, cannot use --every-sec", file=sys.stderr)
            cap.release()
            return st
        eff_step = max(1, round(fps * every_sec))
    else:
        eff_step = max(1, step or 1)

    target = out_dir if flat else out_dir / video.stem
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)

    recent_hashes: list[int] = []
    idx = 0

    while True:
        # grab() advances without decoding: cheap way to skip frames
        if not cap.grab():
            break
        if idx % eff_step != 0:
            idx += 1
            continue

        ok, frame = cap.retrieve()
        if not ok or frame is None:
            idx += 1
            continue

        st.considered += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if blur_thresh > 0:
            score = blur_score(gray)
            st.blur_scores.append(score)
            if score < blur_thresh:
                st.dropped_blur += 1
                idx += 1
                continue
        else:
            st.blur_scores.append(blur_score(gray))

        if hash_dist > 0:
            h = dhash(gray)
            if any(hamming(h, prev) <= hash_dist for prev in recent_hashes):
                st.dropped_dup += 1
                idx += 1
                continue
            recent_hashes.append(h)
            if len(recent_hashes) > hash_window:
                recent_hashes.pop(0)

        name = f"{video.stem}_f{idx:06d}.jpg"
        out_path = target / name
        if not dry_run:
            cv2.imwrite(str(out_path), frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
        manifest.append({
            "image": str(out_path.relative_to(out_dir)),
            "video": video.name,
            "frame_index": idx,
            "timestamp_sec": round(idx / fps, 3) if fps > 0 else "",
        })
        st.kept += 1
        idx += 1

        if max_per_video is not None and st.kept >= max_per_video:
            break

    cap.release()
    print(f"  {video.name}: {width}x{height} @ {fps:.2f}fps, {total} frames, step {eff_step}"
          f"  ->  kept {st.kept} / considered {st.considered}"
          f"  (dup {st.dropped_dup}, blur {st.dropped_blur})")
    return st


# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(
        description="Extract every Nth frame from videos, filtering duplicates and blur.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--src", type=Path, default=REPO / "Data" / "videos",
                   help="video file, or directory to scan for videos")
    p.add_argument("--out", type=Path, default=REPO / "Data" / "frames" / "labeled",
                   help="output directory for JPEGs")
    p.add_argument("--step", type=int, default=15,
                   help="keep every Nth frame")
    p.add_argument("--every-sec", type=float, default=None,
                   help="keep one frame every X seconds (overrides --step)")
    p.add_argument("--hash-dist", type=int, default=6,
                   help="drop a frame if its dHash is within this Hamming distance of a "
                        "recently kept frame; 0 disables the duplicate filter")
    p.add_argument("--hash-window", type=int, default=5,
                   help="how many recently kept frames to compare against")
    p.add_argument("--blur-thresh", type=float, default=40.0,
                   help="drop frames whose Laplacian variance is below this; 0 disables")
    p.add_argument("--max-per-video", type=int, default=None,
                   help="stop after this many kept frames per video")
    p.add_argument("--quality", type=int, default=95,
                   help="JPEG quality (95 keeps annotation-grade detail)")
    p.add_argument("--per-video-dirs", action="store_true",
                   help="write into out/<video_stem>/ instead of one flat directory")
    p.add_argument("--dry-run", action="store_true",
                   help="report counts without writing files")
    args = p.parse_args()

    if args.src.is_dir():
        videos = sorted(f for f in args.src.iterdir()
                        if f.suffix.lower() in VIDEO_SUFFIXES)
    elif args.src.is_file():
        videos = [args.src]
    else:
        print(f"error: {args.src} not found", file=sys.stderr)
        return 1

    if not videos:
        print(f"error: no videos found in {args.src}", file=sys.stderr)
        return 1

    print(f"{len(videos)} video(s) in {args.src}")
    print(f"output: {args.out}" + ("  [DRY RUN, nothing written]" if args.dry_run else ""))
    print()

    total_stats = Stats()
    manifest: list[dict] = []
    for v in videos:
        st = extract_one(
            v, args.out,
            step=args.step,
            every_sec=args.every_sec,
            hash_dist=args.hash_dist,
            hash_window=args.hash_window,
            blur_thresh=args.blur_thresh,
            max_per_video=args.max_per_video,
            jpeg_quality=args.quality,
            flat=not args.per_video_dirs,
            dry_run=args.dry_run,
            manifest=manifest,
        )
        total_stats.merge(st)

    print()
    print(f"TOTAL: kept {total_stats.kept} of {total_stats.considered} candidate frames "
          f"(dropped {total_stats.dropped_dup} duplicates, "
          f"{total_stats.dropped_blur} blurry)")

    if total_stats.blur_scores:
        q = np.percentile(total_stats.blur_scores, [5, 25, 50, 75, 95])
        print("blur score (Laplacian variance) percentiles — use to tune --blur-thresh:")
        print(f"  p5={q[0]:.1f}  p25={q[1]:.1f}  p50={q[2]:.1f}  p75={q[3]:.1f}  p95={q[4]:.1f}")

    if not args.dry_run and manifest:
        args.out.mkdir(parents=True, exist_ok=True)
        csv_path = args.out / "frames_manifest.csv"
        with csv_path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["image", "video", "frame_index", "timestamp_sec"])
            w.writeheader()
            w.writerows(manifest)
        print(f"manifest: {csv_path}  ({len(manifest)} rows)")
        print("  -> keeps the video/frame origin of every image, so you can split "
              "train/val BY VIDEO and avoid leakage between correlated frames.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
