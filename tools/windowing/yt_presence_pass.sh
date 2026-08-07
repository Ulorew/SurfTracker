#!/bin/bash
# Знаменатель для ютубной статистики: на скольких тактах в кадре вообще есть
# сёрфер. Такт 0.5 Гц (не 3): нужна доля времени, а не траектория, и полный
# тайловый обход в 15 плиток на такте 3 Гц стоил бы часы без выигрыша в
# точности оценки доли.
set -u
cd ~/Projects/SurfTracker/v1_local
PY=~/miniconda3/envs/torch/bin/python
W=models/night_legacy_s3_best.pt
OUT=windowing/output/yt_presence
mkdir -p $OUT
find Data/videos/YT_1 Data/videos/YT_SH Data/YT_1 -iname "*.mp4" 2>/dev/null | grep -v _annotated | sort | while read -r f; do
  name=$(basename "$f" .mp4 | tr -c 'A-Za-z0-9_.-' '_' | cut -c1-60)
  [ -f "$OUT/$name.json" ] && { echo "уже есть: $name"; continue; }
  $PY windowing/yt_presence.py --video "$f" --weights $W --tick-hz 0.5 --out "$OUT/$name.json" 2>&1 | tail -1
done
echo "PRESENCE DONE $(date +%H:%M)"
