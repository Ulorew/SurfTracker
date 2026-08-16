#!/bin/bash
# Эмулятор для проверки интерфейса.
#
# Заведён потому, что на телефоне заблокирован ввод через adb
# (INJECT_EVENTS): нажать кнопку и пройти экран насквозь там нельзя, а
# смотреть на скриншот и надеяться — это ровно тот способ проверки, которым
# в этом проекте уже дважды объявляли исправным сломанное.
#
# Камеры у образа нет, поэтому прогон на эмуляторе доходит только до открытия
# камеры и падает. Этого хватает для проверки экранов, настроек и записи
# прогон.json, но НЕ для проверки слежения — оно проверяется на телефоне.
set -eu
export ANDROID_HOME=${ANDROID_HOME:-/home/ulorew/Android/sdk}
AVD=${AVD:-surf}
PORT=${PORT:-5560}
case "${1:-старт}" in
  старт|start)
    pgrep -f "emulator.*-avd $AVD" >/dev/null && { echo "уже запущен"; exit 0; }
    nohup "$ANDROID_HOME/emulator/emulator" -avd "$AVD" -no-audio -no-boot-anim \
        -gpu swiftshader_indirect -port "$PORT" >/tmp/emu_$AVD.log 2>&1 &
    for i in $(seq 1 40); do
      [ "$(adb -s emulator-$PORT shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && {
        echo "готов: emulator-$PORT"; exit 0; }
      sleep 5
    done
    echo "не поднялся за 200 с"; exit 1 ;;
  стоп|stop)
    adb -s "emulator-$PORT" emu kill 2>/dev/null || pkill -f "emulator.*-avd $AVD" || true
    echo "остановлен" ;;
  ставь|install)
    adb -s "emulator-$PORT" install -r "$(dirname "$0")/../../android/camfps/build/com.surftracker.camfps.apk"
    for p in CAMERA RECORD_AUDIO BLUETOOTH_CONNECT BLUETOOTH_SCAN; do
      adb -s "emulator-$PORT" shell pm grant com.surftracker.camfps android.permission.$p 2>/dev/null || true
    done
    echo "поставлено, разрешения выданы" ;;
  *) echo "старт | стоп | ставь"; exit 1 ;;
esac
