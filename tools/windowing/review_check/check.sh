#!/bin/bash
# Проверка разбора записи прогона на настоящем ReviewModel из приложения.
set -e
cd "$(dirname "$0")"
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
SRC=../../../android/camfps/src
JAVAC=$(command -v javac || echo /home/ulorew/Android/jdk/bin/javac)
JAVA=$(command -v java || echo /home/ulorew/Android/jdk/bin/java)
DEPS="$SRC/com/surftracker/camfps/ReviewModel.java $SRC/com/surftracker/camfps/RunJson.java"

echo "== различающая сила стенда =="
fails=0
for m in "колонки-по-номеру:s|for (int i = 0; i < head.length; i++) if (head\[i\].trim().equals(name)) return i;|switch (name) { case \"t_ms\": return 1; case \"Sc\": return 18; case \"winCx\": return 19; case \"winCy\": return 20; case \"bx\": return 28; }|" \
         "потеря-каждый-такт:s|if (prevTracking \&\& !t.tracking)|if (!t.tracking)|" \
         "забыт-пересчёт:s|return sensorX \* (viewW / (double) sensorW);|return sensorX;|" \
         "смещение-видео-потеряно:s|return videoOffsetMs + t.tMs;|return t.tMs;|"; do
  name="${m%%:*}"; expr="${m#*:}"
  M="$D/$name"; mkdir -p "$M/com/surftracker/camfps"
  cp "$SRC/com/surftracker/camfps/RunJson.java" "$M/com/surftracker/camfps/"
  sed "$expr" "$SRC/com/surftracker/camfps/ReviewModel.java" \
      > "$M/com/surftracker/camfps/ReviewModel.java"
  "$JAVAC" -encoding UTF-8 -d "$M/cls" -cp "$M:$SRC" ReviewCheck.java \
      "$M/com/surftracker/camfps/ReviewModel.java" \
      "$M/com/surftracker/camfps/RunJson.java" || {
      echo "  ПОРЧА '$name' НЕ ПРИМЕНИЛАСЬ (не компилируется) — стенд не проверил её";
      fails=$((fails+1)); continue; }
  if "$JAVA" -Dfile.encoding=UTF-8 -cp "$M/cls" ReviewCheck >/dev/null 2>&1; then
    echo "  ПОРЧА '$name' НЕ ПОЙМАНА"; fails=$((fails+1))
  else echo "  порча '$name' поймана"; fi
done
[ $fails -eq 0 ] || { echo "СТЕНД НЕГОДЕН: $fails порч прошли"; exit 2; }

# ИМЕНА ФАЙЛОВ: писатель против читателей.
#
# Лог писался как «прогон.csv», а экран разбора и карточка искали «лог.csv» —
# кнопка «Разбор» не появлялась вовсе. Стенды этого не видели: разбор кормили
# файлом, названным по ЧИТАТЕЛЮ, а писателя не проверяет ничто. Нашлось первым
# же настоящим прогоном на телефоне.
echo
echo "== имена файлов прогона: писатель и читатели совпадают =="
A=$SRC/com/surftracker/camfps
WRITES=$(grep -o 'new File(runDir, "[^"]*")' $A/TrackActivity.java \
        | sed 's/.*"\(.*\)".*/\1/' | tr '\n' ' ')
# «прогон» пишется как база: base.getPath() + ".json". Разворачиваем.
case " $WRITES " in *" прогон "*) WRITES="$WRITES прогон.json";; esac
READS=$(grep -ho 'new File(dir, "[^"]*")\|new File(runDir, "[^"]*")' \
        $A/ReviewActivity.java $A/RunsActivity.java $A/RunDetailActivity.java \
        | sed 's/.*"\(.*\)".*/\1/' | sort -u | tr '\n' ' ')
echo "  пишет:  $WRITES"
echo "  читают: $READS"
MISSING=""
for f in $READS; do
  case " $WRITES " in *" $f "*) ;; *) MISSING="$MISSING $f";; esac
done
if [ -n "$MISSING" ]; then
  echo "  ЧИТАЮТ ТО, ЧЕГО НИКТО НЕ ПИШЕТ:$MISSING"
  exit 2
fi
echo "  ок: каждое читаемое имя кем-то пишется"

echo
echo "== проверка настоящего разбора =="
"$JAVAC" -encoding UTF-8 -d "$D/cls" -cp "$SRC" ReviewCheck.java $DEPS
"$JAVA" -Dfile.encoding=UTF-8 -cp "$D/cls" ReviewCheck
