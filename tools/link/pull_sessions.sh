#!/bin/bash
# Стянуть полевые сессии с телефона и сразу посчитать сводку.
#
#   tools/link/pull_sessions.sh 2609        # все сессии, чья метка начинается с 2609 (26.09)
#   tools/link/pull_sessions.sh 2609 --без-видео
#
# Метка папки на телефоне — ГГММДД_ЧЧММ_<тег>. Видео по 60+ МБ на прогон и
# по Wi-Fi тянется минутами, поэтому его можно отложить: сводке оно не нужно.
#
# Телефон бывает виден в adb ДВАЖДЫ (mdns и явное подключение), и тогда
# любая команда без -s отказывает. Задайте ANDROID_SERIAL — adb читает его сам.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. ~/Android/env.sh 2>/dev/null
PREFIX=${1:?"укажите начало метки, например 2609"}
NOVID=${2:-}
D=/storage/emulated/0/Android/data/com.surftracker.camfps/files/track
OUT="$ROOT/runs/field_$PREFIX"
mkdir -p "$OUT"
LIST=$(adb shell ls "$D" | tr -d '\r' | grep "^$PREFIX" || true)
[ -n "$LIST" ] || { echo "на телефоне нет сессий с меткой $PREFIX*"; exit 1; }
for s in $LIST; do
  mkdir -p "$OUT/$s"
  for f in log.csv run.json; do adb pull "$D/$s/$f" "$OUT/$s/" >/dev/null 2>&1; done
  if [ "$NOVID" != "--без-видео" ]; then adb pull "$D/$s/video.mp4" "$OUT/$s/" >/dev/null 2>&1; fi
  echo "  $s: $(ls "$OUT/$s" | tr '\n' ' ')"
done
echo
"$ROOT/.venv/bin/python" "$ROOT/tools/link/session_stats.py" "$OUT"/*/ --csv "$OUT/сводка.csv"
