#!/bin/bash
# Сличение телефонного переноса с офлайновым трекером.
#
# Сначала — проверка различающей силы стенда: семь порч эталона, каждая
# обязана быть поймана. Порядок именно такой. Стенд, объявляющий согласие,
# ничего не стоит, пока не показано, что он умеет объявлять расхождение.
set -e
cd "$(dirname "$0")"
D=$(mktemp -d)
trap 'rm -rf "$D"' EXIT

SRC=../../../android/camfps/src
# Тот же JDK, которым собирается apk: сличать перенос компилятором из другого
# набора значило бы допустить ещё один источник разницы в самой проверке.
PY=$(cd ../../.. && pwd)/.venv/bin/python
JAVAC=$(command -v javac || echo /home/ulorew/Android/jdk/bin/javac)
JAVA=$(command -v java || echo /home/ulorew/Android/jdk/bin/java)

echo "== сборка =="
"$PY" gen_scenarios.py
"$JAVAC" -d "$D" -cp "$SRC" PortDrive.java "$SRC/com/surftracker/camfps/Tracker.java" 2>&1 | grep -v "^Note:" || true

echo
echo "== различающая сила стенда =="
fails=0
for m in frac k expand miss shrink alpha reacq; do
  PORT_CHECK_MUTATE=$m "$PY" py_drive.py scenarios.txt "$D/py_$m.csv" 2>/dev/null
  "$JAVA" -cp "$D" PortDrive scenarios.txt "$D/java.csv"
  if "$PY" compare.py "$D/py_$m.csv" "$D/java.csv" --quiet; then
    echo "  ПОРЧА '$m' НЕ ПОЙМАНА — стенд слеп к ней"
    fails=$((fails+1))
  else
    echo "  порча '$m' поймана"
  fi
done
if [ $fails -ne 0 ]; then
  echo "СТЕНД НЕГОДЕН: $fails порч прошли незамеченными"
  exit 2
fi

bad=0
for mode in true false; do
  echo
  echo "== сличение, прижатие центра окном=$mode =="
  "$PY" py_drive.py scenarios.txt "$D/py_$mode.csv" "$mode"
  "$JAVA" -cp "$D" PortDrive scenarios.txt "$D/java_$mode.csv" "$mode"
  if ! "$PY" compare.py "$D/py_$mode.csv" "$D/java_$mode.csv"; then bad=1; fi
done
if [ $bad -eq 0 ]; then
  echo
  echo "ИТОГ: перенос совпадает с офлайновым трекером в ОБОИХ режимах"
else
  echo "ИТОГ: РАСХОЖДЕНИЕ (см. выше)"
  exit 1
fi
