#!/bin/bash
# Стенд для stm/libraries/CaptureSensor: проверка класса БЕЗ ЖЕЛЕЗА.
#
# Класс собирается обычным g++ с флагом CAPSENS_HOST: регистры TIM2 и базовый
# Sensor подменены заглушкой capture_sensor_host.h, а сам CaptureSensor.cpp
# компилируется без единой правки — иначе проверялся бы не тот код, который
# поедет на плату.
#
# ПРОВЕРЯЕТСЯ НЕ «ТЕСТЫ ЗЕЛЁНЫЕ», А ИХ РАЗЛИЧАЮЩАЯ СИЛА. Тест, проходящий и на
# исправном коде, и на сломанном, не проверяет ничего. Поэтому класс умеет
# ломаться по флагу компиляции, и каждая поломка обязана уронить СВОЙ
# названный тест. Если мутация собралась и тесты прошли — виноват стенд, а не
# мутация, и это здесь считается провалом.
set -u
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
LIB="$ROOT/stm/libraries/CaptureSensor/src"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT=$(mktemp -d); trap 'rm -rf "$OUT"' EXIT

# НАКЛОН МОСТА С ПИТОНОВОЙ СТОРОНЫ. Он жил в ТРЁХ местах и разошёлся: 362.7 в
# классе, 362.4 в smoothness.py, свежий замер 362.066. Правило «одно правило —
# одно место» на границе языков обрывается: импорт её не пересекает. Поэтому
# число извлекается отсюда и подаётся в сборку, а тест сверяет его с
# константой класса. Если разбор сломается, стенд обязан упасть, а не молча
# собраться без сверки.
PY_SLOPE=$(sed -n 's/^GAIN[[:space:]]*=[[:space:]]*\([0-9.]\+\).*/\1/p' \
           "$ROOT/tools/link/smoothness.py" | head -1)
if [ -z "$PY_SLOPE" ]; then
    echo "ОТКАЗ: не удалось извлечь GAIN из tools/link/smoothness.py."
    echo "       Сверка наклона между языками — не украшение: без неё"
    echo "       константы разъезжаются молча, как уже было."
    exit 2
fi

build_run() {  # $1 = флаг мутации (или пусто)
    g++ -std=c++11 -O1 -Wall -DCAPSENS_HOST -I "$LIB" -I "$HERE" \
        -DCAPSENS_PY_SLOPE="$PY_SLOPE" \
        ${1:+-D$1} -o "$OUT/t" \
        "$HERE/capture_sensor_tests.cpp" "$LIB/CaptureSensor.cpp" \
        2> "$OUT/cc" || { cat "$OUT/cc"; return 2; }
    cat "$OUT/cc"
    "$OUT/t" > "$OUT/log" 2>&1
    return $?
}

fail=0

echo "--- базовая сборка: обязана ПРОЙТИ ---"
if build_run ""; then
    grep -E "^ИТОГ" "$OUT/log"
else
    echo "БАЗОВАЯ СБОРКА НЕ ПРОШЛА:"; cat "$OUT/log"; fail=1
fi
echo

# мутация -> тест, который обязан упасть
declare -A EXPECT=(
  [CAPSENS_MUT_NO_AGE_FIX]="два режима защёлкивания"
  [CAPSENS_MUT_NO_PERIOD_CHECK]="период вне границ"
  [CAPSENS_MUT_NO_EDGE_CHECK]="фронтов нет"
  [CAPSENS_MUT_NAIVE_SLOPE]="перевод скважности"
  [CAPSENS_MUT_HEALTH_ALWAYS_OK]="годность не тождественна"
  [CAPSENS_MUT_NO_FROZEN_CHECK]="замирание таймера"
  [CAPSENS_MUT_HEALTH_BY_READS]="годность не тождественна"
  [CAPSENS_MUT_TS_AT_POLL]="метка времени по фронту"
)

echo "--- мутации: каждая обязана уронить СВОЙ тест ---"
for m in CAPSENS_MUT_NO_AGE_FIX CAPSENS_MUT_NO_PERIOD_CHECK \
         CAPSENS_MUT_NO_EDGE_CHECK CAPSENS_MUT_NAIVE_SLOPE \
         CAPSENS_MUT_HEALTH_ALWAYS_OK CAPSENS_MUT_NO_FROZEN_CHECK \
         CAPSENS_MUT_HEALTH_BY_READS CAPSENS_MUT_TS_AT_POLL; do
    want="${EXPECT[$m]}"
    if build_run "$m"; then
        echo "  СБОЙ  $m: тесты ПРОШЛИ, хотя класс сломан — стенд бессилен"
        fail=1
    else
        if grep -q "СБОЙ  $want" "$OUT/log"; then
            echo "  ok    $m -> уронил «$want»"
        else
            echo "  СБОЙ  $m: упало что-то не то, ждали «$want»"
            grep "СБОЙ" "$OUT/log" | sed 's/^/          /'
            fail=1
        fi
    fi
done

echo
if [ "$fail" = 0 ]; then echo "СТЕНД: всё сошлось"; else echo "СТЕНД: ЕСТЬ ПРОВАЛЫ"; fi
exit $fail
