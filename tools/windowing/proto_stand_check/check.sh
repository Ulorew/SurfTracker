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
  proto::buildDiag(b, 5, 12345, 500, 1000, 80000, 156000, 0);
  dump("diag", b, proto::DIAG_LEN);
  proto::buildDiag(b, 1, 7, 919, 170000, 169000, 170500, 0x0F);
  dump("diag_flags", b, proto::DIAG_LEN);
  // боевые пакеты тоже: расширение не должно их сдвинуть
  proto::buildReq(b, 1, 1.0f, 0.1f);                 dump("req", b, proto::REQ_LEN);
  proto::buildTel(b, 7, 0.5f, -0.25f, 0x08);         dump("tel", b, proto::TEL_LEN);
  return 0;
}
EOF
g++ -std=c++11 -I"$ROOT/stm/libraries/SurfProtoV2/src" -o "$D/t" "$D/t.cpp"
"$D/t" > "$D/cpp.txt"

echo "== питонова сторона =="
LINKDIR="$ROOT/tools/link" "$PY" - > "$D/py.txt" <<'EOF'
import sys, struct
sys.path.insert(0, __import__("os").environ["LINKDIR"])
from proto_v2 import *

def dump(name, b):
    print(name, " ".join(f"{x:02X}" for x in b))

dump("cmd_mode", build_cmd(3, CMD_MODE, 1.0))
dump("cmd_volt", build_cmd(0, CMD_VOLT, 1.5))
dump("cmd_spin", build_cmd(127, CMD_SPIN, -0.25))

def diag(seq, t, ih, age, ch, cp, fl):
    b = bytes([MAGIC_DIAG, seq & 0x7F]) + struct.pack("<IIIII", t, ih, age, ch, cp) + bytes([fl])
    return b + bytes([crc8(b)])

dump("diag", diag(5, 12345, 500, 1000, 80000, 156000, 0))
dump("diag_flags", diag(1, 7, 919, 170000, 169000, 170500, 0x0F))
dump("req", build_req(1, 1.0, 0.1))
dump("tel", build_tel(7, 0.5, -0.25, 0x08))
EOF

echo "== сличение =="
if diff -q "$D/cpp.txt" "$D/py.txt" >/dev/null; then
    echo "  побайтово совпало, строк: $(wc -l < "$D/cpp.txt")"
else
    echo "  РАСХОЖДЕНИЕ:"
    diff "$D/cpp.txt" "$D/py.txt" | head -10
    exit 2
fi

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

echo
echo "ИТОГ: обе реализации расширенного протокола собирают одни байты,"
echo "      и боевые пакеты (req/tel) расширением не сдвинуты"
