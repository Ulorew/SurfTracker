#!/bin/bash
# Надёжный запуск прогона на устройстве.
#
# Три отказа, стоившие прогонов, и защита от каждого:
#  1. экран погашен при старте -> активность не выходит на передний план,
#     MIUI отбирает камеру. Лечится пробуждением и снятием шторки ДО запуска;
#  2. другое приложение выходит на передний план и вытесняет прогон
#     (в логе amsBoostNotify чужого пакета). Лечится закреплением экрана;
#  3. молчаливый провал: прогон висит, файл нулевой. Лечится проверкой через
#     30 секунд — если запись не растёт, падаем сразу, а не через 25 минут.
set -eu
. ~/Android/env.sh
# Адрес устройства adb обязателен: DEV=<ip:порт> ./launch.sh ...
D=${DEV:?задайте DEV=<ip:порт> — адрес телефона для adb}
C=com.surftracker.camfps
COMBO=$1; SECONDS_RUN=$2; SEGBYTES=${3:-0}; HZ=${4:-3}
F=/sdcard/Android/data/$C/files

timeout 25 adb -s $D shell am task lock stop >/dev/null 2>&1 || true
timeout 25 adb -s $D shell am force-stop $C >/dev/null 2>&1
timeout 25 adb -s $D shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1
timeout 25 adb -s $D shell wm dismiss-keyguard >/dev/null 2>&1 || true
sleep 2
W=$(timeout 25 adb -s $D shell dumpsys power 2>/dev/null | grep -o 'mWakefulness=[A-Za-z]*' | head -1 | tr -d '\r')
echo "экран перед стартом: $W"
[ "$W" = "mWakefulness=Awake" ] || { echo "ОТКАЗ: экран не проснулся"; exit 1; }

timeout 25 adb -s $D shell rm -rf $F/rec_$COMBO $F/infer_$COMBO.json $F/camfps_$COMBO.json >/dev/null 2>&1
# Пруфы прошлого прогона — СНАЧАЛА забрать, потом удалять. Раньше здесь
# стояло только rm, и когда понадобилось перепроверить нулевые детекции по
# сырым пикселям, проверять оказалось нечего: ни одного файла не осталось
# ни на устройстве, ни в репозитории.
PROOFS="$(dirname "$0")/proofs/prev_$COMBO"
mkdir -p "$PROOFS"
for f in $(timeout 25 adb -s $D shell "ls $F/proof_min*.png 2>/dev/null" | tr -d '\r'); do
    timeout 25 adb -s $D pull "$f" "$PROOFS/" >/dev/null 2>&1 || true
done
timeout 25 adb -s $D shell "rm -f $F/proof_min*.png" >/dev/null 2>&1
timeout 25 adb -s $D shell am start -n $C/.MainActivity --es combo "$COMBO" --es yuv max \
    --ei seconds "$SECONDS_RUN" --ei yuv_hz "$HZ" --ez infer true \
    --el segment_bytes "$SEGBYTES" >/dev/null
sleep 6
TID=$(timeout 25 adb -s $D shell am stack list 2>/dev/null | grep -i camfps | grep -o "taskId=[0-9]*" | head -1 | cut -d= -f2)
[ -n "$TID" ] || { echo "ОТКАЗ: задача не найдена"; exit 1; }
timeout 25 adb -s $D shell am task lock "$TID" >/dev/null 2>&1
echo "закреплено, task=$TID"

sleep 30
SZ=$(timeout 25 adb -s $D shell "stat -c %s $F/rec_$COMBO/seg_0.mp4 2>/dev/null || echo 0" | tr -d '\r')
W=$(timeout 25 adb -s $D shell dumpsys power 2>/dev/null | grep -o 'mWakefulness=[A-Za-z]*' | head -1 | tr -d '\r')
echo "через 30 с: запись $((SZ/1000000)) МБ, экран $W"
[ "$SZ" -gt 10000000 ] || { echo "ОТКАЗ: запись не растёт — прогон не пошёл"; exit 1; }
echo "ПРОГОН ИДЁТ: $COMBO, $SECONDS_RUN с, сегменты $((SEGBYTES/1000000)) МБ, $HZ Гц"
