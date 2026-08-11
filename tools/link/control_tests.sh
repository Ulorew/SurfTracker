#!/bin/bash
# Тесты закона управления + мутации §9 спецификации.
#
# Проверяется НЕ «тесты зелёные», а соответствие: каждая из четырёх мутаций
# обязана уронить свой названный тест. Тест, который проходит и на исправном
# коде, и на сломанном, различающей силы не имеет и ничего не проверяет.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INC="$ROOT/stm/libraries/SurfProtoV2/src"
SRC="$ROOT/tools/link/control_tests.cpp"
OUT=$(mktemp -d); trap 'rm -rf "$OUT"' EXIT

build_run() {  # $1 = флаг мутации (или пусто), $2 = имя
    g++ -std=c++11 -O1 -I "$INC" ${1:+-D$1} -o "$OUT/t" "$SRC" || return 2
    "$OUT/t" > "$OUT/log" 2>&1
    return $?
}

fail=0

echo "--- базовая сборка: обязана ПРОЙТИ ---"
if build_run "" ; then
    grep -E "^ИТОГ" "$OUT/log"
else
    echo "БАЗОВАЯ СБОРКА НЕ ПРОШЛА:"; cat "$OUT/log"; fail=1
fi
echo

# мутация -> тест, который обязан упасть (по §9)
declare -A EXPECT=(
  [MUT_NO_FRESHNESS]="пачка"
  [MUT_NO_EXTRAP_CAP]="обрыв с w_dot"
  [MUT_NO_RAMP]="скачок уставки"
  [MUT_NO_SLIP_RESET]="ложный срыв после watchdog"
)

echo "--- мутации: каждая обязана уронить СВОЙ тест ---"
for m in MUT_NO_FRESHNESS MUT_NO_EXTRAP_CAP MUT_NO_RAMP MUT_NO_SLIP_RESET; do
    want="${EXPECT[$m]}"
    if build_run "$m"; then
        echo "  СБОЙ  $m: тесты ПРОШЛИ, хотя код сломан — тест бессилен"
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
[ $fail -eq 0 ] && echo "ИТОГ: §9 выполнен — все четыре мутации ловятся" \
                || echo "ИТОГ: §9 НЕ выполнен"
exit $fail
