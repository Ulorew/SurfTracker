#!/bin/bash
# Проверка расчёта уставки (правки A и B): синхронизация с углом вала.
set -e
cd "$(dirname "$0")"
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
SRC=../../../android/camfps/src
JAVAC=$(command -v javac || echo /home/ulorew/Android/jdk/bin/javac)
JAVA=$(command -v java || echo /home/ulorew/Android/jdk/bin/java)
A=$SRC/com/surftracker/camfps

echo "== различающая сила стенда =="
fails=0
for m in "синхронизация-выключена:s@if (syncOk) {@if (false) {@" \
         "проверка-здравого-смысла-снята:s@> SANE_DEG@> 1e9@" \
         "знак-потерян:s@thetaCap + sign \* Math.toRadians(errDeg)@thetaCap + Math.toRadians(errDeg)@" \
         "вал-сейчас-игнорируется:s@double fresh = tgt - thetaNow;@double fresh = tgt - thetaCap;@"; do
  name="${m%%:*}"; expr="${m#*:}"
  M="$D/$name"; mkdir -p "$M/com/surftracker/camfps"
  sed "$expr" "$A/LoopControl.java" > "$M/com/surftracker/camfps/LoopControl.java"
  "$JAVAC" -encoding UTF-8 -d "$M/cls" -cp "$M:$SRC" LoopCheck.java \
      "$M/com/surftracker/camfps/LoopControl.java" || {
      echo "  ПОРЧА '$name' НЕ ПРИМЕНИЛАСЬ (не компилируется) — стенд не проверил её"
      fails=$((fails+1)); continue; }
  if "$JAVA" -Dfile.encoding=UTF-8 -cp "$M/cls" LoopCheck >/dev/null 2>&1; then
    echo "  ПОРЧА '$name' НЕ ПОЙМАНА"; fails=$((fails+1))
  else echo "  порча '$name' поймана"; fi
done
[ $fails -eq 0 ] || { echo "СТЕНД НЕГОДЕН: $fails порч прошли"; exit 2; }

echo
echo "== проверка настоящего расчёта =="
"$JAVAC" -encoding UTF-8 -d "$D/cls" -cp "$SRC" LoopCheck.java "$A/LoopControl.java"
"$JAVA" -Dfile.encoding=UTF-8 -cp "$D/cls" LoopCheck
