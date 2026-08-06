#!/bin/bash
# Пункт 2: матрица потоков. Каждый прогон — отдельный запуск приложения
# (не итерация внутри одного): так планировщик видит обычный запуск, а не
# долгоживущий процесс, уже получивший привилегии.
set -eu
. ~/Android/env.sh
PKG=com.surftracker.verify
DEV=/sdcard/Android/data/$PKG/files
for T in 1 2 4; do
  adb shell rm -f $DEV/bench_t$T.json
  adb shell am start -n $PKG/.MainActivity --es mode bench --ei threads $T \
      --ei warmup 30 --ei runs 200 >/dev/null
  for i in $(seq 1 90); do
    adb shell "[ -f $DEV/bench_t$T.json ] && echo ok" 2>/dev/null | grep -q ok && break
    sleep 2
  done
  adb pull -q $DEV/bench_t$T.json /tmp/bench_t$T.json >/dev/null 2>&1 || echo "нет результата для $T потоков"
done
echo "--- потоки: p50 / p95 (мс) ---"
for T in 1 2 4; do
  [ -f /tmp/bench_t$T.json ] && python3 -c "
import json;d=json.load(open('/tmp/bench_t$T.json'))
print(f\"  {d['threads']} поток(а/ов): p50 {d['p50_ms']:6.1f}  p95 {d['p95_ms']:6.1f}  p99 {d['p99_ms']:6.1f}  min {d['min_ms']:6.1f}  XNNPACK={d['xnnpack']}\")"
done
