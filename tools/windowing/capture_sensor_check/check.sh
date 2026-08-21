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

build_run() {  # $1 = флаг мутации (или пусто)
    g++ -std=c++11 -O1 -Wall -DCAPSENS_HOST -I "$LIB" -I "$HERE" \
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
  [CAPSENS_MUT_NO_PAIR_CHECK]="несогласованная пара"
  [CAPSENS_MUT_NO_PERIOD_CHECK]="период вне границ"
  [CAPSENS_MUT_NO_EDGE_CHECK]="фронтов нет"
  [CAPSENS_MUT_NAIVE_SLOPE]="перевод скважности"
  [CAPSENS_MUT_HEALTH_ALWAYS_OK]="годность не тождественна"
)

echo "--- мутации: каждая обязана уронить СВОЙ тест ---"
for m in CAPSENS_MUT_NO_PAIR_CHECK CAPSENS_MUT_NO_PERIOD_CHECK \
         CAPSENS_MUT_NO_EDGE_CHECK CAPSENS_MUT_NAIVE_SLOPE \
         CAPSENS_MUT_HEALTH_ALWAYS_OK; do
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
