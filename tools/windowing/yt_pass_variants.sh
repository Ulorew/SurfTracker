#!/bin/bash
# Замер режимов ограничения убеждения полем зрения на ютубном материале.
# База ("off") — уже снятый прогон yt_pass: он делался кодом БЕЗ ограничения.
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
W=models/night_legacy_s3_best.pt
FLAGS="--enable-a --filter-level 2 --enable-gate --score-form distance"

for MODE in window frame; do
  OUT=tools/windowing/output/yt_pass_$MODE
  mkdir -p $OUT
  find Data/videos/YT_1 Data/videos/YT_SH Data/YT_1 -iname "*.mp4" 2>/dev/null | grep -v _annotated | sort | while read -r f; do
    name=$(basename "$f" .mp4 | tr -c 'A-Za-z0-9_.-' '_' | cut -c1-60)
    [ -f "$OUT/$name.jsonl" ] && { echo "уже есть: $MODE/$name"; continue; }
    echo "=== $MODE $name $(date +%H:%M) ==="
    $PY tools/windowing/track_run.py --video "$f" --weights $W --tick-hz 3.0 \
        --view-clamp $MODE --out "$OUT/$name.mp4" --log-out "$OUT/$name.jsonl" $FLAGS 2>&1 | tail -2
  done
done
echo "VARIANTS DONE $(date +%H:%M)"
