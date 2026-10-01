#!/bin/bash
# Сверка целиком: установка, заливка пакета, прогон, сверка.
# Ничего интерактивного — только числа в файлы.
set -eu
. ~/Android/env.sh
PKG=com.surftracker.verify
REPO=$(cd "$(dirname "$0")/../.." && pwd)
PACK=$REPO/export/phone_pack
DEV=/sdcard/Android/data/$PKG/files
THREADS=${1:-1}

adb install -r build/$PKG.apk >/dev/null
# папку создаёт само приложение при первом запуске; поднимаем его вхолостую
adb shell am start -n $PKG/.MainActivity >/dev/null 2>&1 || true
sleep 2
adb shell mkdir -p $DEV/frames

adb push -q "$PACK/surf_w8a32.tflite" $DEV/ >/dev/null
adb push -q "$PACK/frames/." $DEV/frames/ >/dev/null
adb shell rm -f $DEV/phone_out.json $DEV/phone_error.txt
adb logcat -c

adb shell am start -n $PKG/.MainActivity --ei threads $THREADS >/dev/null
echo "прогон запущен (потоков=$THREADS), жду результат..."
for i in $(seq 1 120); do
  adb shell "[ -f $DEV/phone_out.json ] && echo ok" 2>/dev/null | grep -q ok && break
  adb shell "[ -f $DEV/phone_error.txt ] && echo err" 2>/dev/null | grep -q err && {
      echo "ОШИБКА на устройстве:"; adb shell cat $DEV/phone_error.txt; exit 1; }
  sleep 2
done
adb logcat -d -s SurfVerify:* | tail -5
adb pull -q $DEV/phone_out.json /tmp/phone_out.json >/dev/null
echo "--- сверка (допуск 1e-5) ---"
$REPO/.venv/bin/python $REPO/tools/export/phone_pack.py compare --pack "$PACK" --phone /tmp/phone_out.json
