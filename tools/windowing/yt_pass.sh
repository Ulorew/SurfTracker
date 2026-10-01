#!/bin/bash
# Блок 2: прод-конфигурация по всем ютубным видео, ровный такт 333 мс.
#
# СТАТУС ОЖИДАНИЙ ПОНИЖЕННЫЙ (записано в шапку отчёта): f_x для ютубных —
# прикидки, камеры движутся и зумят, метрика меряет смесь свойств трекера и
# съёмки. Это грубая статистика для сравнения со стресс-подборкой, а не
# вердикт о трекере.
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
W=models/night_legacy_s3_best.pt
OUT=tools/windowing/output/yt_pass
mkdir -p $OUT
FLAGS="--enable-a --filter-level 2 --enable-gate --score-form distance"

find Data/videos/YT_1 Data/videos/YT_SH Data/YT_1 -iname "*.mp4" 2>/dev/null | grep -v _annotated | sort | while read -r f; do
  name=$(basename "$f" .mp4 | tr -c 'A-Za-z0-9_.-' '_' | cut -c1-60)
  [ -f "$OUT/$name.jsonl" ] && { echo "уже есть: $name"; continue; }
  echo "=== $name $(date +%H:%M) ==="
  $PY tools/windowing/track_run.py --video "$f" --weights $W --tick-hz 3.0 \
      --out "$OUT/$name.mp4" --log-out "$OUT/$name.jsonl" $FLAGS 2>&1 | tail -2
done
echo "YT PASS DONE $(date +%H:%M)"
