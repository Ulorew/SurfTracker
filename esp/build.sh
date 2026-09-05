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
# ОПЦИИ FQBN ПЕРЕДАЮТСЯ, А НЕ ЗАШИТЫ НАГЛУХО.
#
# Зачем: psram_check осмыслен ТОЛЬКО при PSRAM=enabled — иначе ответ «0 байт»
# приходит на любой плате, включая WROVER, и вердикт «модуль WROOM» становится
# ложным. Прежде опций не было вовсе, и этот скетч собирали ручным вызовом
# arduino-cli мимо build.sh; повтор через build.sh дал бы правдоподобный, но
# ничего не значащий ноль. Ровно тот класс ошибки, который проект ловит:
# величина выглядит измеренной, а измерения не было.
#
# Использование:  esp/build.sh psram_check --опции PSRAM=enabled
FQBN_BASE="esp32:esp32:esp32"
FQBN_OPTS=""
_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --опции) FQBN_OPTS="$2"; shift 2 ;;
        *)       _args+=("$1"); shift ;;
    esac
done
set -- "${_args[@]}"
if [ -n "$FQBN_OPTS" ]; then
    FQBN="$FQBN_BASE:$FQBN_OPTS"
    echo "FQBN с опциями: $FQBN"
else
    FQBN="$FQBN_BASE"
fi

# ЗАЩИТА ОТ ЛОЖНОГО ЗАМЕРА PSRAM: без опции этот скетч соберётся и напечатает
# ноль, который ничего не значит. Лучше отказать, чем выдать пустое число.
if [ "${1:-}" = "psram_check" ] && [ -z "$FQBN_OPTS" ]; then
    echo "ОТКАЗ: psram_check без опций даст 0 байт на ЛЮБОЙ плате."
    echo "       Запускать так:  esp/build.sh psram_check --опции PSRAM=enabled"
    exit 1
fi

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
