#!/bin/bash
# Сличение ДВУХ реализаций расширенного протокола: питон против C++.
#
# ЗАЧЕМ. Протокол теперь живёт в двух местах — tools/link/proto_v2.py (клиент)
# и stm/libraries/SurfProtoV2 (плата). Расхождение таблиц CRC или раскладки
# полей выглядит для человека как «канал молчит», и искать его будут в
# радиосвязи, в питании, в чём угодно — но не в том, что две стороны собирают
# разные байты. У боевой части протокола контрольные векторы для этого уже
# есть; стендовая была добавлена без них, и эта проверка закрывает дыру.
#
# ОТДЕЛЬНО ПРО ПОДТВЕРЖДЕНИЕ. DIAG вырос до 27 байт: три байта перед CRC несут
# ПРИМЕНЁННЫЕ режим, напряжение и скорость. Кодирование там не «положить как
# есть», а сотые с прижатием и округлением — то есть арифметика, а всякая
# арифметика на двух языках расходится на границах. Поэтому векторы ниже бьют
# именно по границам: потолок 2.00 В, оба конца скорости +-1.00, заказ выше
# потолка, дробное значение с округлением.
#
# КАК. Обе стороны собирают одни и те же пакеты и сличаются ПОБАЙТОВО. C++
# компилируется тем же компилятором, что и прошивка, но собирается для хоста:
# нам нужна не плата, а именно арифметика заголовка.
set -euo pipefail
cd "$(dirname "$0")"
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
ROOT=$(cd ../../.. && pwd)
PY="$ROOT/.venv/bin/python"

echo "== сборка C++ стороны =="
cat > "$D/t.cpp" <<'EOF'
#include <cstdio>
#include <cstdint>
#include <cstring>
#include "proto_v2.h"
static void dump(const char *name, const uint8_t *b, int n) {
  printf("%s", name);
  for (int i = 0; i < n; i++) printf(" %02X", b[i]);
  printf("\n");
}
int main() {
  uint8_t b[32];
  proto::buildCmd(b, 3, proto::CMD_MODE, 1.0f);      dump("cmd_mode", b, proto::CMD_LEN);
  proto::buildCmd(b, 0, proto::CMD_VOLT, 1.5f);      dump("cmd_volt", b, proto::CMD_LEN);
  proto::buildCmd(b, 127, proto::CMD_SPIN, -0.25f);  dump("cmd_spin", b, proto::CMD_LEN);

  // Обычные кадры: боевой режим на умолчании и удержание на 1.50 В.
  proto::buildDiag(b, 5, 12345, 500, 1000, 80000, 156000, 0,
                   proto::MODE_FIGHT, 2.0f, 0.0f);
  dump("diag", b, proto::DIAG_LEN);
  proto::buildDiag(b, 1, 7, 919, 170000, 169000, 170500, 0x0F,
                   proto::MODE_HOLD, 1.5f, 0.0f);
  dump("diag_flags", b, proto::DIAG_LEN);

  // ГРАНИЦЫ ПОДТВЕРЖДЕНИЯ. Потолок напряжения, оба конца скорости, дно
  // напряжения — ровно те значения, на которых сотые и знак ломаются.
  proto::buildDiag(b, 2, 1, 2, 3, 4, 5, 0, proto::MODE_SPIN, 2.0f, 1.0f);
  dump("diag_spin_max", b, proto::DIAG_LEN);
  proto::buildDiag(b, 3, 1, 2, 3, 4, 5, 0, proto::MODE_SPIN, 0.10f, -1.0f);
  dump("diag_spin_min", b, proto::DIAG_LEN);
  // Заказ выше потолка: прижатие обязано совпасть, а не «примерно совпасть».
  proto::buildDiag(b, 4, 1, 2, 3, 4, 5, 0, proto::MODE_SPIN, 9.9f, 5.0f);
  dump("diag_clamp", b, proto::DIAG_LEN);
  proto::buildDiag(b, 6, 1, 2, 3, 4, 5, 0, proto::MODE_SPIN, 0.6f, -5.0f);
  dump("diag_clamp_neg", b, proto::DIAG_LEN);
  // Округление до сотых и отрицательная дробь в int8.
  proto::buildDiag(b, 7, 1, 2, 3, 4, 5, 0, proto::MODE_HOLD, 0.33f, -0.25f);
  dump("diag_round", b, proto::DIAG_LEN);
  // Дно: ноль всюду. Нулевой байт напряжения означает «поле не питается».
  proto::buildDiag(b, 0, 0, 0, 0, 0, 0, 0, proto::MODE_FIGHT, 0.0f, 0.0f);
  dump("diag_zero", b, proto::DIAG_LEN);

  // боевые пакеты тоже: расширение не должно их сдвинуть
  proto::buildReq(b, 1, 1.0f, 0.1f);                 dump("req", b, proto::REQ_LEN);
  proto::buildTel(b, 7, 0.5f, -0.25f, 0x08);         dump("tel", b, proto::TEL_LEN);
  return 0;
}
EOF
g++ -std=c++11 -I"$ROOT/stm/libraries/SurfProtoV2/src" -o "$D/t" "$D/t.cpp"
"$D/t" > "$D/cpp.txt"

echo "== питонова сторона =="
# build_diag зовётся ИЗ МОДУЛЯ, а не пишется здесь заново. Своя сборка кадра в
# тесте проверяла бы тест, а не клиента: раскладку в proto_v2.py можно было бы
# сломать, и сличение осталось бы зелёным.
LINKDIR="$ROOT/tools/link" "$PY" - > "$D/py.txt" <<'EOF'
import sys
sys.path.insert(0, __import__("os").environ["LINKDIR"])
from proto_v2 import *

def dump(name, b):
    print(name, " ".join(f"{x:02X}" for x in b))

dump("cmd_mode", build_cmd(3, CMD_MODE, 1.0))
dump("cmd_volt", build_cmd(0, CMD_VOLT, 1.5))
dump("cmd_spin", build_cmd(127, CMD_SPIN, -0.25))

dump("diag", build_diag(5, 12345, 500, 1000, 80000, 156000, 0, MODE_FIGHT, 2.0, 0.0))
dump("diag_flags", build_diag(1, 7, 919, 170000, 169000, 170500, 0x0F, MODE_HOLD, 1.5, 0.0))
dump("diag_spin_max", build_diag(2, 1, 2, 3, 4, 5, 0, MODE_SPIN, 2.0, 1.0))
dump("diag_spin_min", build_diag(3, 1, 2, 3, 4, 5, 0, MODE_SPIN, 0.10, -1.0))
dump("diag_clamp", build_diag(4, 1, 2, 3, 4, 5, 0, MODE_SPIN, 9.9, 5.0))
dump("diag_clamp_neg", build_diag(6, 1, 2, 3, 4, 5, 0, MODE_SPIN, 0.6, -5.0))
dump("diag_round", build_diag(7, 1, 2, 3, 4, 5, 0, MODE_HOLD, 0.33, -0.25))
dump("diag_zero", build_diag(0, 0, 0, 0, 0, 0, 0, MODE_FIGHT, 0.0, 0.0))

dump("req", build_req(1, 1.0, 0.1))
dump("tel", build_tel(7, 0.5, -0.25, 0x08))
EOF

echo "== сличение =="
if diff -q "$D/cpp.txt" "$D/py.txt" >/dev/null; then
    echo "  побайтово совпало, строк: $(wc -l < "$D/cpp.txt")"
else
    echo "  РАСХОЖДЕНИЕ:"
    diff "$D/cpp.txt" "$D/py.txt" | head -20
    exit 2
fi

# ДЛИНЫ ПРОВЕРЯЮТСЯ ЯВНО. Совпадение байтов ничего не сказало бы, если бы обе
# стороны одинаково ошиблись длиной боевых пакетов.
echo "== длины: DIAG вырос, боевые не тронуты =="
awk '{ n = NF - 1; c[n] = c[n] " " $1 }
     END { for (n in c) print n " байт:" c[n] }' "$D/cpp.txt" | sort -n | sed 's/^/  /'
awk '$1 ~ /^diag/ && NF - 1 != 27 { print "ОТКАЗ: " $1 " не 27 байт"; e = 1 }
     $1 == "req" && NF - 1 != 11   { print "ОТКАЗ: req сдвинут"; e = 1 }
     $1 == "tel" && NF - 1 != 12   { print "ОТКАЗ: tel сдвинут"; e = 1 }
     $1 ~ /^cmd/ && NF - 1 != 11   { print "ОТКАЗ: cmd сдвинут"; e = 1 }
     END { exit e ? 1 : 0 }' "$D/cpp.txt"

# РАЗБОР, А НЕ ТОЛЬКО СБОРКА. Симметричная ошибка в кодировании и в разборе
# сократилась бы и осталась невидимой для сличения байтов — поэтому здесь
# заложенные значения требуются обратно из САМИХ БАЙТОВ, собранных C++.
echo "== разбор: подтверждение достаётся обратно =="
LINKDIR="$ROOT/tools/link" CPP="$D/cpp.txt" "$PY" - <<'EOF'
import os, sys
sys.path.insert(0, os.environ["LINKDIR"])
from proto_v2 import DIAG_LEN, parse_diag, MODE_SPIN, MODE_HOLD

frames = {}
for line in open(os.environ["CPP"]):
    parts = line.split()
    frames[parts[0]] = bytes(int(x, 16) for x in parts[1:])

# Ждём ровно то, что заложено в векторы выше.
want = {
    "diag":           (0, 2.00,  0.00),
    "diag_flags":     (1, 1.50,  0.00),
    "diag_spin_max":  (2, 2.00,  1.00),
    "diag_spin_min":  (2, 0.10, -1.00),
    "diag_clamp":     (2, 2.00,  1.00),      # 9.9 В и 5 рад/с прижаты
    "diag_clamp_neg": (2, 0.60, -1.00),
    "diag_round":     (1, 0.33, -0.25),
    "diag_zero":      (0, 0.00,  0.00),
}
bad = []
for name, (mode, volt, spin) in want.items():
    p = parse_diag(frames[name])
    if p is None:
        bad.append(f"{name}: кадр C++ не разобрался питоном")
        continue
    if (p["ack_mode"], p["ack_volt"], p["ack_spin"]) != (mode, volt, spin):
        bad.append(f"{name}: {p['ack_mode']}/{p['ack_volt']}/{p['ack_spin']} "
                   f"вместо {mode}/{volt}/{spin}")
    else:
        print(f"  {name}: режим {p['ack_mode_name']}, "
              f"{p['ack_volt']:.2f} В, {p['ack_spin']:+.2f} рад/с")

# СТАРАЯ ДЛИНА ОБЯЗАНА ОТВЕРГАТЬСЯ. Иначе плата со старой прошивкой молча
# сошла бы за новую, и подтверждения читались бы из мусора.
old = frames["diag"][:23] + frames["diag"][-1:]
if parse_diag(old) is not None:
    bad.append("24-байтный кадр старого формата принят как новый")
else:
    print("  кадр старой длины (24 байта) отвергнут")

if bad:
    for b in bad:
        print("ОТКАЗ:", b)
    sys.exit(2)
EOF

# ПРОВЕРКА РАЗЛИЧАЮЩЕЙ СИЛЫ. Совпадение двух реализаций ничего не стоит, если
# сличение слепо: испорченный CRC обязан быть пойман. Ровно так однажды уже
# вышло в стенде переноса трекера — он подписывал как «совпадающие» и заведомо
# сломанные версии.
echo "== порча: сличение обязано её увидеть =="
sed 's/^cmd_mode \(.*\) ..$/cmd_mode \1 FF/' "$D/cpp.txt" > "$D/bad.txt"
if diff -q "$D/bad.txt" "$D/py.txt" >/dev/null; then
    echo "  ОТКАЗ: подменённый CRC прошёл незамеченным"
    exit 2
fi
echo "  порченый CRC пойман"

# ПОРЧА В НОВЫХ БАЙТАХ отдельно: сличение по старым 24 байтам прошло бы, а
# подтверждение при этом врало бы — то есть ровно тот отказ, ради которого эти
# три байта и заводились. Портится байт скорости (26-й, поле 27 с именем).
awk '$1 == "diag_spin_min" { $27 = "00" } { print }' "$D/cpp.txt" > "$D/bad2.txt"
# Требуется не просто «файлы различаются», а РОВНО ОДНА расхождённая строка и
# именно та. Иначе сама подмена могла бы попортить формат и дать зелёный ответ
# по случайной причине — проверка различающей силы, которая сама себя обманула.
# diff при расхождении возвращает 1, а здесь стоит pipefail — поэтому его код
# гасится явно, иначе проверка падала бы ровно тогда, когда СРАБОТАЛА.
diff "$D/bad2.txt" "$D/py.txt" > "$D/d2.txt" || true
changed=$(awk '/^[<>]/ {print $2}' "$D/d2.txt" | sort -u | xargs)
if [ "$changed" != "diag_spin_min" ]; then
    echo "  ОТКАЗ: подмена подтверждения не изолирована (расходятся: ${changed:-ничего})"
    exit 2
fi
echo "  порченое подтверждение поймано"

echo
echo "ИТОГ: обе реализации расширенного протокола собирают одни байты,"
echo "      подтверждение (режим/напряжение/скорость) сходится на границах,"
echo "      и боевые пакеты (req/tel) расширением не сдвинуты"
