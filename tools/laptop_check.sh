#!/bin/bash
# Готов ли ноутбук к полю. Запускать из корня репозитория НА НОУТБУКЕ,
# пока есть интернет: всё, чего не хватит, ставится только с ним.
#
#   tools/laptop_check.sh            # полная проверка, со сборкой
#   tools/laptop_check.sh --быстро   # без сборок
#
# Проверка СОБИРАЕТ прошивку и APK, а не ищет файлы. Найденный arduino-cli
# без ядра esp32 или SDK без build-tools выглядят установленными ровно до
# той минуты, когда на воде понадобится поправить одну строку.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
FAST=${1:-}
ok=0; bad=0
pass() { printf "  ✓ %s\n" "$1"; ok=$((ok+1)); }
fail() { printf "  ✗ %s\n      -> %s\n" "$1" "$2"; bad=$((bad+1)); }

echo "== репозиторий =="
pass "HEAD $(git log --oneline -1 2>/dev/null)"
for f in models/phone/surf_w8a32.tflite:2997445 models/phone/person_w8a32.tflite:3039349; do
  p=${f%%:*}; n=${f##*:}
  [ "$(stat -c %s "$p" 2>/dev/null)" = "$n" ] && pass "$p ($n Б)" \
    || fail "$p нет или не того размера (нужно $n Б)" "взять из комплекта: models/phone/"
done

echo "== python =="
if [ -x .venv/bin/python ]; then
  for m in numpy serial; do
    .venv/bin/python -c "import $m" 2>/dev/null && pass "модуль $m" \
      || fail "нет модуля $m" ".venv/bin/pip install numpy pyserial"
  done
  .venv/bin/python -c "import socket; socket.AF_BLUETOOTH; socket.BTPROTO_RFCOMM" 2>/dev/null \
    && pass "python умеет Bluetooth RFCOMM (нужно предполёту)" \
    || fail "python без AF_BLUETOOTH" "нужен системный python Linux с BlueZ: sudo apt install bluez libbluetooth-dev, затем пересоздать .venv"
else
  fail "нет .venv" "python3 -m venv .venv && .venv/bin/pip install numpy pyserial"
fi

echo "== Bluetooth ноутбука =="
if command -v bluetoothctl >/dev/null; then
  bluetoothctl show 2>/dev/null | grep -q "Powered: yes" && pass "адаптер включён" \
    || fail "адаптер выключен или его нет" "bluetoothctl power on"
else
  fail "нет bluetoothctl" "sudo apt install bluez"
fi

echo "== USB к ESP32 =="
id -nG | grep -qw dialout && pass "пользователь в группе dialout" \
  || fail "нет прав на /dev/ttyUSB*" "sudo usermod -aG dialout $USER  (и перелогиниться)"

echo "== прошивка ESP32 =="
CLI=$HOME/Android/arduino/arduino-cli
if [ -x "$CLI" ]; then
  pass "arduino-cli"
  "$CLI" core list 2>/dev/null | grep -q "esp32:esp32 *3.3.11" && pass "ядро esp32 3.3.11" \
    || fail "нет ядра esp32 3.3.11" "$CLI core install esp32:esp32@3.3.11   (~5 ГБ, только с интернетом)"
  grep -q "version=2.4.0" "$HOME/Arduino/libraries/Simple_FOC/library.properties" 2>/dev/null \
    && pass "SimpleFOC 2.4.0" \
    || fail "нет SimpleFOC 2.4.0" "tar xzf Simple_FOC.tar.gz -C ~/Arduino/libraries/   (из комплекта)"
  if [ "$FAST" != "--быстро" ]; then
    esp/build.sh field_link >/tmp/lc_esp.log 2>&1 && grep -q "бинарь:" /tmp/lc_esp.log \
      && pass "field_link СОБИРАЕТСЯ" || fail "field_link не собирается" "см. /tmp/lc_esp.log"
  fi
else
  fail "нет arduino-cli в $CLI" "скачать arduino-cli в ~/Android/arduino/ (https://arduino.github.io/arduino-cli/)"
fi

echo "== Android =="
if [ -f "$HOME/Android/env.sh" ]; then
  . "$HOME/Android/env.sh"
  command -v adb >/dev/null && pass "adb" || fail "нет adb" "sdkmanager 'platform-tools'"
  [ -d "$ANDROID_HOME/build-tools/35.0.0" ] && pass "build-tools 35.0.0" \
    || fail "нет build-tools 35.0.0" "sdkmanager 'build-tools;35.0.0'"
  [ -f "$ANDROID_HOME/platforms/android-35/android.jar" ] && pass "platform android-35" \
    || fail "нет android-35" "sdkmanager 'platforms;android-35'"
  command -v java >/dev/null && pass "java" || fail "нет JDK" "положить JDK в ~/Android/jdk"
  ls "$HOME/Android/libs/"litert-*.aar >/dev/null 2>&1 && pass "LiteRT в ~/Android/libs" \
    || fail "нет LiteRT" "tar xzf android_libs.tar.gz -C ~/Android/   (из комплекта)"
  if [ "$FAST" != "--быстро" ]; then
    (cd android/camfps && bash build.sh >/tmp/lc_apk.log 2>&1) && [ -s android/camfps/build/com.surftracker.camfps.apk ] \
      && pass "APK СОБИРАЕТСЯ" || fail "APK не собирается" "см. /tmp/lc_apk.log"
  fi
else
  fail "нет ~/Android/env.sh" "cp env.sh ~/Android/   (из комплекта), поправить пути"
fi

echo "== видео =="
command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null && pass "ffmpeg, ffprobe" \
  || fail "нет ffmpeg" "sudo apt install ffmpeg"

echo
if [ $bad -eq 0 ]; then echo "ГОТОВ: $ok проверок, ни одного отказа"
else echo "НЕ ГОТОВ: $bad отказов из $((ok+bad)). Чинить сейчас, пока есть интернет."; exit 1; fi
