#!/bin/bash
# Проверка живого состояния прогона на настоящем LiveStatus из приложения.
set -e
cd "$(dirname "$0")"
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
SRC=../../../android/camfps/src
JAVAC=$(command -v javac || echo /home/ulorew/Android/jdk/bin/javac)
JAVA=$(command -v java || echo /home/ulorew/Android/jdk/bin/java)

echo "== различающая сила стенда =="
fails=0
for m in "проценты-от-попаданий:s|100.0 \* hits / ticks|100.0 * ticks / Math.max(hits,1)|" \
         "молчание-мотора-скрыто:s|\"МОТОР МОЛЧИТ\"|\"мотор на связи\"|" \
         "отрицательный-остаток:s|if (leftMs > 0)|if (true)|" \
         "нет-разрешения-подойти:s|\\\\nможно подходить|\\\\n|" \
         "отказ-выглядит-успехом:s|if (error != null)|if (false)|g" \
         "ноль-тактов-как-успех:s|if (done \&\& ticks == 0) {|if (false) {|"; do
  name="${m%%:*}"; expr="${m#*:}"
  M="$D/$name"; mkdir -p "$M/com/surftracker/camfps"
  sed "$expr" "$SRC/com/surftracker/camfps/LiveStatus.java" \
      > "$M/com/surftracker/camfps/LiveStatus.java"
  "$JAVAC" -encoding UTF-8 -d "$M/cls" -cp "$M:$SRC" LiveStatusCheck.java \
      "$M/com/surftracker/camfps/LiveStatus.java" || {
      echo "  ПОРЧА '$name' НЕ ПРИМЕНИЛАСЬ (не компилируется) — стенд не проверил её";
      fails=$((fails+1)); continue; }
  if "$JAVA" -Dfile.encoding=UTF-8 -cp "$M/cls" LiveStatusCheck >/dev/null 2>&1; then
    echo "  ПОРЧА '$name' НЕ ПОЙМАНА"; fails=$((fails+1))
  else echo "  порча '$name' поймана"; fi
done
[ $fails -eq 0 ] || { echo "СТЕНД НЕГОДЕН: $fails порч прошли"; exit 2; }

echo
echo "== проверка настоящего экрана =="
"$JAVAC" -encoding UTF-8 -d "$D/cls" -cp "$SRC" LiveStatusCheck.java \
    "$SRC/com/surftracker/camfps/LiveStatus.java"
"$JAVA" -Dfile.encoding=UTF-8 -cp "$D/cls" LiveStatusCheck
