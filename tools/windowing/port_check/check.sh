#!/bin/bash
# Сличение телефонного переноса с офлайновым трекером ПО МАТРИЦЕ РЕЖИМОВ.
#
# Порядок в каждом сочетании: КОНТРОЛЬ -> порчи.
#   контроль — БЕЗ порчи стороны обязаны совпасть (иначе меряем не то);
#   порча    — с порчей обязаны разойтись (иначе стенд слеп).
# Ни одного из условий по отдельности не хватает: фаза порч однажды уже
# сравнивала две РАЗНЫЕ конфигурации и печатала «поймана» для пустой порчи.
#
# Матрица, а не один режим: каждый новый флаг иначе остаётся вне проверки.
set -eu
cd "$(dirname "$0")"
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
SRC=../../../android/camfps/src
PY=$(cd ../../.. && pwd)/.venv/bin/python
JAVAC=$(command -v javac || echo /home/ulorew/Android/jdk/bin/javac)
JAVA=$(command -v java || echo /home/ulorew/Android/jdk/bin/java)
A=$SRC/com/surftracker/camfps

echo "== сборка =="
"$PY" gen_scenarios.py
"$JAVAC" -d "$D" -cp "$SRC" PortDrive.java "$A/Tracker.java" "$A/KalmanTracker.java" \
    2>&1 | grep -v "^Note:" || true

# Какие порчи ОБЯЗАНЫ быть пойманы, а какие обязаны МОЛЧАТЬ, зависит от
# сочетания. Константа, которой в данном режиме не пользуются, не должна менять
# ничего — и это такая же проверка, как и обратная: механизм, влияющий в
# выключенном виде, включён не там, где думают.
BASE="k expand miss reacq tau"      # общие для всех режимов
MUT_L1="shrink alpha"               # альфа-бета и EMA размера: только уровень 1
MUT_A="lam veto"                    # механизм А
MUT_K="rpos rlogh accel"            # Калман
MUT_G="chi2"                        # гейт
MUT_RADIUS="frac"                   # радиус приёма в ведении: не нужен при гейте
bad=0

# режим: имя | А | Калман | гейт
for combo in "уровень1|false|false|false" \
             "уровень1+А|true|false|false" \
             "Калман|false|true|false" \
             "Калман+гейт|false|true|true" \
             "Калман+гейт+А|true|true|true"; do
  name=${combo%%|*}; rest=${combo#*|}
  a=${rest%%|*}; rest=${rest#*|}
  kal=${rest%%|*}; gate=${rest##*|}
  for mode in false true; do
    echo
    echo "===== $name, прижатие=$mode ====="
    "$PY" py_drive.py scenarios.txt "$D/py.csv" "$mode" "$a" "$kal" "$gate"
    "$JAVA" -cp "$D" PortDrive scenarios.txt "$D/java.csv" "$mode" "$a" "$kal" "$gate"
    if ! "$PY" compare.py "$D/py.csv" "$D/java.csv" --quiet; then
      echo "  КОНТРОЛЬ ПРОВАЛЕН: стороны расходятся БЕЗ порчи"
      "$PY" compare.py "$D/py.csv" "$D/java.csv" | head -12
      exit 2
    fi
    echo "  контроль: без порчи расхождений нет"

    LIST="$BASE"; SILENT=""
    if [ "$kal" = "true" ]; then LIST="$LIST $MUT_K"; SILENT="$SILENT $MUT_L1";
                            else LIST="$LIST $MUT_L1"; fi
    if [ "$gate" = "true" ]; then LIST="$LIST $MUT_G"; SILENT="$SILENT $MUT_RADIUS";
                            else LIST="$LIST $MUT_RADIUS"; fi
    if [ "$a" = "true" ]; then LIST="$LIST $MUT_A"; else SILENT="$SILENT $MUT_A"; fi

    for m in $LIST; do
      PORT_CHECK_MUTATE=$m "$PY" py_drive.py scenarios.txt "$D/m.csv" \
          "$mode" "$a" "$kal" "$gate" 2>/dev/null
      if "$PY" compare.py "$D/m.csv" "$D/java.csv" --quiet; then
        echo "  ПОРЧА '$m' НЕ ПОЙМАНА"; bad=$((bad+1))
      else
        echo "  порча '$m' поймана"
      fi
    done
    for m in $SILENT; do
      PORT_CHECK_MUTATE=$m "$PY" py_drive.py scenarios.txt "$D/s.csv" \
          "$mode" "$a" "$kal" "$gate" 2>/dev/null
      if ! "$PY" compare.py "$D/s.csv" "$D/java.csv" --quiet; then
        echo "  '$m' ВЛИЯЕТ, хотя в этом режиме не используется"; bad=$((bad+1))
      else
        echo "  '$m' молчит — как и должен в этом режиме"
      fi
    done
  done
done

echo
[ $bad -eq 0 ] || { echo "СТЕНД НЕГОДЕН: $bad порч прошли незамеченными"; exit 2; }
echo "ИТОГ: перенос совпадает с офлайновым трекером во ВСЕХ сочетаниях режимов,"
echo "      и в каждом сочетании стенд ловит подмены его собственных констант"
