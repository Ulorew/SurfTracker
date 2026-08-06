#!/bin/bash
# Закрепление на больших ядрах (cpu4-7, A78 2.5 ГГц). Маска 0xf0.
# taskset применяется к УЖЕ ЗАПУЩЕННОМУ процессу приложения: сам процесс
# закрепить из Java нельзя, а запускать бенчмарк adb-бинарём тикет запрещает.
set -eu
. ~/Android/env.sh
PKG=com.surftracker.verify
DEV=/sdcard/Android/data/$PKG/files
MASK=${MASK:-f0}
for T in 1 2 4; do
  adb shell rm -f $DEV/bench_t$T.json
  adb shell am force-stop $PKG
  adb shell am start -n $PKG/.MainActivity --es mode bench --ei threads $T \
      --ei warmup 30 --ei runs 200 >/dev/null
  sleep 1
  PID=$(adb shell pidof $PKG | tr -d '\r')
  if [ -n "$PID" ]; then
    adb shell "for t in /proc/$PID/task/*; do taskset -p $MASK \$(basename \$t) >/dev/null 2>&1; done" || true
    echo "закреплено на маске $MASK (pid $PID, потоков запрошено $T)"
  else
    echo "процесс не найден — закрепление не применено"
  fi
  for i in $(seq 1 90); do
    adb shell "[ -f $DEV/bench_t$T.json ] && echo ok" 2>/dev/null | grep -q ok && break
    sleep 2
  done
  adb pull -q $DEV/bench_t$T.json /tmp/pin_t$T.json >/dev/null 2>&1 || true
done
echo "--- закреплено на A78 (cpu4-7): p50 / p95 ---"
for T in 1 2 4; do
  [ -f /tmp/pin_t$T.json ] && python3 -c "
import json;d=json.load(open('/tmp/pin_t$T.json'))
print(f\"  {d['threads']}: p50 {d['p50_ms']:6.1f}  p95 {d['p95_ms']:6.1f}  min {d['min_ms']:6.1f}\")"
done
