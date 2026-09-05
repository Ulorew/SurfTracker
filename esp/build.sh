#!/bin/bash
# Сборка и прошивка скетчей ESP32, одной командой. Брат stm/build.sh.
#
#   build.sh as5048a_probe            — собрать
#   build.sh as5048a_probe --прошить  — собрать и залить по USB
#   build.sh --список                 — какие скетчи есть
#
# ЧЕМ ОТЛИЧАЕТСЯ ОТ stm/build.sh:
#
#  1. Заливка идёт по USB через тот же CP2102, что и вывод Serial — своего
#     ST-LINK у ESP32 нет. Порт ищется по by-id, а не по /dev/ttyUSB0:
#     номер уезжает при каждом перетыкании, и залить можно не туда.
#
#  2. --libraries ../stm/libraries. Заголовок протокола ОДИН на обе прошивки
#     (см. шапку bt_link.ino); копия здесь однажды разошлась бы по CRC.
#
#  3. Заливка на ESP32 ПЕРЕЗАПИСЫВАЕТ мост bt_link, а вместе с ним канал
#     телефон -> мотор. Это не «одна из прошивок платы», это единственный
#     мост, поэтому подтверждение спрашивается прямо здесь.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLI="$HOME/Android/arduino/arduino-cli"
FQBN="esp32:esp32:esp32"

[ -x "$CLI" ] || { echo "нет arduino-cli в $CLI"; exit 1; }

if [ "${1:-}" = "--список" ] || [ $# -eq 0 ]; then
    echo "скетчи в esp/:"
    for d in "$ROOT"/esp/*/; do
        n=$(basename "$d")
        [ -f "$d/$n.ino" ] && echo "  $n"
    done
    exit 0
fi

SKETCH="$ROOT/esp/$1"
[ -f "$SKETCH/$(basename "$SKETCH").ino" ] || { echo "нет скетча $1"; exit 1; }

echo "== сборка $1 =="
OUT="$SKETCH/build"
rm -rf "$OUT"
# --input-dir при заливке обязателен по той же причине, что и в stm/build.sh:
# без него upload берёт бинарь из кэша, возможно от прошлой сборки.
"$CLI" compile -b "$FQBN" --libraries "$ROOT/stm/libraries" \
       --output-dir "$OUT" "$SKETCH" 2>&1 \
    | grep -vE "^$|pragma message|note:|\^" | tail -6
BIN="$OUT/$(basename "$SKETCH").ino.bin"
[ -s "$BIN" ] || { echo "ОТКАЗ: бинарь не собрался ($BIN)"; exit 1; }
echo "  бинарь: $BIN ($(stat -c %s "$BIN") байт)"

if [ "${2:-}" = "--прошить" ]; then
    PORT=$(ls /dev/serial/by-id/*CP2102* 2>/dev/null | head -1 || true)
    [ -n "$PORT" ] || { echo "ОТКАЗ: плата не найдена в /dev/serial/by-id"; exit 1; }
    echo
    echo "ВНИМАНИЕ: заливка ПЕРЕЗАПИШЕТ прошивку ESP32 (сейчас там мост bt_link)."
    echo "Порт: $PORT"
    printf "продолжить? [да/нет] "
    read -r ANSWER   # имя латиницей: bash не принимает кириллицу в идентификаторах
    [ "$ANSWER" = "да" ] || { echo "отменено"; exit 1; }
    "$CLI" upload -b "$FQBN" -p "$PORT" --input-dir "$OUT" "$SKETCH" 2>&1 | tail -6
fi
