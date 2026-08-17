#!/bin/bash
# Проверка правила разрешения настроек прогона и умолчаний.
set -e
cd "$(dirname "$0")"
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
SRC=../../../android/camfps/src
JAVAC=$(command -v javac || echo /home/ulorew/Android/jdk/bin/javac)
JAVA=$(command -v java || echo /home/ulorew/Android/jdk/bin/java)

# Различающая сила: две порчи правила. Первая — самая опасная из возможных:
# сохранённое начинает бить переданное явно, и все команды с ноутбука молча
# идут с чужими числами.
echo "== различающая сила стенда =="
fails=0
for m in "интент-игнорируется:s/if (intent != null \&\& intent.has(key)) {/if (false) {/" \
         "умолчание-длительности:s/\"seconds\",  INT,   \"Длительность, с\", \"60\"/\"seconds\",  INT,   \"Длительность, с\", \"90\"/"; do
  name="${m%%:*}"; expr="${m#*:}"
  M="$D/$name"; mkdir -p "$M/com/surftracker/camfps"
  sed "$expr" "$SRC/com/surftracker/camfps/RunSettings.java" \
      > "$M/com/surftracker/camfps/RunSettings.java"
  "$JAVAC" -encoding UTF-8 -d "$M/cls" -cp "$M:$SRC" RunSettingsCheck.java \
      "$M/com/surftracker/camfps/RunSettings.java" || {
      echo "  ПОРЧА '$name' НЕ ПРИМЕНИЛАСЬ (не компилируется) — стенд не проверил её";
      fails=$((fails+1)); continue; }
  if "$JAVA" -Dfile.encoding=UTF-8 -cp "$M/cls" RunSettingsCheck >/dev/null 2>&1; then
    echo "  ПОРЧА '$name' НЕ ПОЙМАНА"; fails=$((fails+1))
  else echo "  порча '$name' поймана"; fi
done
[ $fails -eq 0 ] || { echo "СТЕНД НЕГОДЕН: $fails порч прошли"; exit 2; }

# ИМЯ ФАЙЛА НАСТРОЕК: экран и прогон обязаны читать одно и то же.
#
# Переименование файлов прогона на латиницу задело только одну сторону: экран
# остался писать в «прогон», прогон стал читать «run». С того коммита экран
# настроек не влиял ни на один прогон, а в run.json уходило
# «настройки_с_экрана: 0» — отчёт подтверждал, что всё чисто.
echo
echo "== имя файла настроек одно на всех =="
A=$SRC/com/surftracker/camfps
NAMES=$(grep -ho 'getSharedPreferences("[^"]*"' $A/*.java | sed 's/.*"\(.*\)"/\1/' | sort -u)
echo "  встречается: $(echo $NAMES)"
if [ "$(echo "$NAMES" | wc -l)" -ne 1 ]; then
  echo "  РАЗНЫЕ ИМЕНА — экран и прогон работают с разными файлами"
  exit 2
fi
echo "  ок: одно имя"

echo
echo "== проверка настоящих настроек =="
"$JAVAC" -encoding UTF-8 -d "$D/cls" -cp "$SRC" RunSettingsCheck.java \
    "$SRC/com/surftracker/camfps/RunSettings.java"
"$JAVA" -Dfile.encoding=UTF-8 -cp "$D/cls" RunSettingsCheck
