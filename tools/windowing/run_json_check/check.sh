#!/bin/bash
# Проверка разбора прогон.json на настоящем RunJson из приложения.
set -e
cd "$(dirname "$0")"
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
SRC=../../../android/camfps/src
JAVAC=$(command -v javac || echo "$HOME"/Android/jdk/bin/javac)
JAVA=$(command -v java || echo "$HOME"/Android/jdk/bin/java)
# Сначала — РАЗЛИЧАЮЩАЯ СИЛА. Три порчи разбора, каждая обязана быть поймана.
# Стенд, который не ловит подмену, объявил бы исправность и при настоящей
# поломке; в этом проекте так уже случалось не раз.
echo "== различающая сила стенда =="
fails=0
for m in "секунд-вместо-факта:s|\"длительность_с\"|\"секунд\"|" \
         "ключ-без-кавычек:s|indexOf('\"' + key + '\"')|indexOf(key)|" \
         "молчаливый-ноль:s|return Double.parseDouble(v.substring(0, e));|return 0;|"; do
  name="${m%%:*}"; sed_expr="${m#*:}"
  M="$D/mut_$name"; mkdir -p "$M/com/surftracker/camfps"
  sed "$sed_expr" "$SRC/com/surftracker/camfps/RunJson.java" \
      > "$M/com/surftracker/camfps/RunJson.java"
  "$JAVAC" -encoding UTF-8 -d "$M/cls" -cp "$M:$SRC" RunJsonCheck.java \
      "$M/com/surftracker/camfps/RunJson.java" || {
      echo "  ПОРЧА '$name' НЕ ПРИМЕНИЛАСЬ (не компилируется) — стенд не проверил её";
      fails=$((fails+1)); continue; }
  if "$JAVA" -Dfile.encoding=UTF-8 -cp "$M/cls" RunJsonCheck >/dev/null 2>&1; then
    echo "  ПОРЧА '$name' НЕ ПОЙМАНА"; fails=$((fails+1))
  else
    echo "  порча '$name' поймана"
  fi
done
[ $fails -eq 0 ] || { echo "СТЕНД НЕГОДЕН: $fails порч прошли"; exit 2; }

echo
echo "== проверка настоящего разбора =="
"$JAVAC" -encoding UTF-8 -d "$D/cls" -cp "$SRC" RunJsonCheck.java \
    "$SRC/com/surftracker/camfps/RunJson.java"
"$JAVA" -Dfile.encoding=UTF-8 -cp "$D/cls" RunJsonCheck
