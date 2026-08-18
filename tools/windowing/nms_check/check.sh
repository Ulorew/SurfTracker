#!/bin/bash
# Сличение НАСТОЯЩЕГО Tracker.nms с независимым эталоном.
#
# Порядок тот же, что в port_check: контроль (без порчи стороны обязаны
# совпасть) -> порчи эталона (обязаны разойтись). Порчи здесь вносятся в
# ЭТАЛОН, а не в перенос: вместе с контролем это зажимает механизм с двух
# сторон — перенос обязан совпасть с правильным правилом И разойтись с
# испорченным.
set -eu
cd "$(dirname "$0")"
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
SRC=../../../android/camfps/src
PY=$(cd ../../.. && pwd)/.venv/bin/python
JAVAC=$(command -v javac || echo /home/ulorew/Android/jdk/bin/javac)
JAVA=$(command -v java || echo /home/ulorew/Android/jdk/bin/java)

echo "== сборка =="
"$PY" gen_cases.py
"$JAVAC" -d "$D" -cp "$SRC" NmsDrive.java \
    "$SRC/com/surftracker/camfps/Tracker.java" \
    "$SRC/com/surftracker/camfps/KalmanTracker.java" 2>&1 | grep -v "^Note:" || true

"$JAVA" -cp "$D:$SRC" NmsDrive cases.txt "$D/java.csv"
"$PY" nms_ref.py cases.txt "$D/ref.csv"

if ! diff -q "$D/ref.csv" "$D/java.csv" >/dev/null; then
  echo "  КОНТРОЛЬ ПРОВАЛЕН: перенос расходится с эталоном"
  diff "$D/ref.csv" "$D/java.csv" | head -20
  exit 2
fi
echo "  контроль: $(($(wc -l < "$D/java.csv") - 1)) строк выхода совпали точно"

bad=0
for m in thresh iou size sortdir truncate capacity; do
  NMS_CHECK_MUTATE=$m "$PY" nms_ref.py cases.txt "$D/m.csv"
  if diff -q "$D/m.csv" "$D/java.csv" >/dev/null; then
    echo "  ПОРЧА '$m' НЕ ПОЙМАНА"; bad=$((bad+1))
  else
    n=$(diff "$D/m.csv" "$D/java.csv" | grep -c '^<' || true)
    echo "  порча '$m' поймана ($n строк расхождения)"
  fi
done

echo
[ $bad -eq 0 ] || { echo "СТЕНД НЕГОДЕН: $bad порч прошли незамеченными"; exit 2; }
echo "ИТОГ: Tracker.nms совпадает с независимым эталоном на всех случаях,"
echo "      свойства выхода выполняются, и стенд ловит подмену каждого правила"
