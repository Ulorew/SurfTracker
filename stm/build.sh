#!/bin/bash
# Сборка и прошивка скетчей платы, одной командой.
#
#   build.sh phone_link          — собрать
#   build.sh phone_link --прошить — собрать и залить через ST-LINK
#   build.sh --список            — какие скетчи есть
#
# ЗАЧЕМ ФАЙЛОМ. Три вещи здесь стоили поиска, и каждая ломает сборку молча:
#
#  1. FQBN. Плата B-G431B-ESC1 лежит под «Disco», а НЕ под «ESC_board» —
#     хотя последнее называется «Electronic speed controllers» и выглядит
#     единственно правильным. Под ESC_board есть только Wraith V1 и STorM32,
#     и попытка собрать туда падает с «invalid value for option pnum».
#
#  2. ВЕРСИЯ ЯДРА 2.10.1, не 3.0.0. На 3.0.0 HardwareSerial стал абстрактным,
#     конструктор с пинами исчез, и канал к ESP32 не собирается вовсе.
#     Автообновление ядра уже ломало сборку дважды (см. шапку phone_link.ino).
#
#  3. ВЕРСИЯ SimpleFOC 2.3.5, не 2.4.0 — пара к ядру 2.10.1.
#
# Библиотека SurfProtoV2 подключается симлинком из stm/libraries: держать её
# копию в ~/Arduino значило бы иметь две правды об одном протоколе.
# pipefail ОБЯЗАТЕЛЕН. Без него статус конвейера берётся у ПОСЛЕДНЕЙ команды,
# то есть у `tail`, и он всегда 0 — провалившаяся компиляция выглядела бы
# удачной, а `upload` без --input-dir залил бы на плату ПРЕДЫДУЩИЙ бинарник из
# кэша. Проверка воркфлоу это воспроизвела.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLI="$HOME/Android/arduino/arduino-cli"
FQBN="STMicroelectronics:stm32:Disco:pnum=B_G431B_ESC1"

[ -x "$CLI" ] || { echo "нет arduino-cli в $CLI"; exit 1; }

if [ "${1:-}" = "--список" ] || [ $# -eq 0 ]; then
    echo "скетчи в stm/:"
    for d in "$ROOT"/stm/*/; do
        n=$(basename "$d")
        [ -f "$d/$n.ino" ] && echo "  $n"
    done
    exit 0
fi

SKETCH="$ROOT/stm/$1"
[ -f "$SKETCH/$(basename "$SKETCH").ino" ] || { echo "нет скетча $1"; exit 1; }

# Симлинк на библиотеку протокола — идемпотентно, чтобы работало на любой машине.
SB=$("$CLI" config get directories.user)
mkdir -p "$SB/libraries"
ln -sfn "$ROOT/stm/libraries/SurfProtoV2" "$SB/libraries/SurfProtoV2"

echo "== сборка $1 =="
# Бинарь кладётся в ЯВНЫЙ каталог, и заливается потом ровно он: без
# --input-dir upload берёт файл из кэша arduino-cli, который может быть от
# прошлой удачной сборки.
OUT="$SKETCH/build"
rm -rf "$OUT"
"$CLI" compile -b "$FQBN" --output-dir "$OUT" "$SKETCH" 2>&1 \
    | grep -vE "^$|pragma message|note:|\^" | tail -6
BIN="$OUT/$(basename "$SKETCH").ino.bin"
[ -s "$BIN" ] || { echo "ОТКАЗ: бинарь не собрался ($BIN)"; exit 1; }
echo "  бинарь: $BIN ($(stat -c %s "$BIN") байт)"

if [ "${2:-}" = "--прошить" ]; then
    # ПРОШИВКА — ДЕЙСТВИЕ С ЖЕЛЕЗОМ. Плата под напряжением, на валу мотор;
    # заливка обрывает текущую прошивку и на секунды оставляет выходы в
    # неопределённом состоянии. Поэтому подтверждение спрашивается здесь, а не
    # подразумевается флагом.
    echo
    echo "ВНИМАНИЕ: заливка перезапишет прошивку на плате."
    echo "Убедитесь, что это согласовано и вал никому не мешает."
    printf "продолжить? [да/нет] "
    read -r ответ
    [ "$ответ" = "да" ] || { echo "отменено"; exit 1; }
    "$CLI" upload -b "$FQBN" --input-dir "$OUT" "$SKETCH" 2>&1 | tail -4
fi
