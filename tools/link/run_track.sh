#!/bin/bash
# Запуск прогона слежения. Предполёт ОБЯЗАТЕЛЕН и блокирующий.
#
# Проверка, вывод которой можно проглядеть, бесполезна: я дважды терял прогон
# на неверной прошивке, а на третий раз запустил, хотя предполёт напечатал
# «не пройден» — потому что запуск шёл следующей командой независимо от него.
# Теперь не запускается.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. ~/Android/env.sh 2>/dev/null || true

"$ROOT/tools/link/preflight.sh" || {
    echo
    echo "ЗАПУСК ОТМЕНЁН: предполёт не пройден."
    echo "Частые причины: снято 12 В (без него нет ESP и Bluetooth),"
    echo "на плате лежит диагностический скетч вместо phone_link,"
    echo "телефон отвалился от adb."
    exit 1
}

adb shell am force-stop com.surftracker.camfps >/dev/null 2>&1 || true
adb shell am start -n com.surftracker.camfps/.TrackActivity "$@" >/dev/null
echo
echo "ЗАПУЩЕНО. На телефоне кнопка СТАРТ."
