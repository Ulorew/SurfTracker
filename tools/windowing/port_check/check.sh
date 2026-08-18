#!/bin/bash
# Сличение телефонного переноса с офлайновым трекером.
#
# Порядок: КОНТРОЛЬ -> порчи -> сличение. Контроль появился после того, как
# фаза порч оказалась пустой: режим прижатия центра ей не передавался, питон
# шёл с умолчанием True, Java с боевым false, и стороны расходились на 45
# тактов ДО всякой порчи. Каждая строка «порча поймана» сообщала о разнице
# конфигураций, счётчик fails не мог стать ненулевым, а ветка «СТЕНД НЕГОДЕН»
# была мёртвым кодом.
#
# Поэтому здесь два обязательных условия, а не одно:
#   контроль  — БЕЗ порчи стороны обязаны совпасть (иначе меряем не то);
#   порча     — с порчей обязаны разойтись (иначе стенд слеп).
# Ни одного из них по отдельности не хватает.
set -eu
cd "$(dirname "$0")"
D=$(mktemp -d)
trap 'rm -rf "$D"' EXIT

SRC=../../../android/camfps/src
PY=$(cd ../../.. && pwd)/.venv/bin/python
# Тот же JDK, которым собирается apk: сличать перенос компилятором из другого
# набора значило бы допустить ещё один источник разницы в самой проверке.
JAVAC=$(command -v javac || echo /home/ulorew/Android/jdk/bin/javac)
JAVA=$(command -v java || echo /home/ulorew/Android/jdk/bin/java)

echo "== сборка =="
"$PY" gen_scenarios.py
"$JAVAC" -d "$D" -cp "$SRC" PortDrive.java "$SRC/com/surftracker/camfps/Tracker.java" 2>&1 | grep -v "^Note:" || true

MUTATIONS="frac k expand miss shrink alpha reacq tau"
# Механизм А проверяется ТОЛЬКО когда он включён: на выключенном подмена его
# констант обязана не менять ничего, и это отдельный контроль ниже.
MUT_A="lam veto"
bad=0

# МАТРИЦА РЕЖИМОВ. Механизм А появился отдельным флагом, и без матрицы он
# оказался бы вне сличения ровно так же, как когда-то оказался режим прижатия
# центра: фаза порч тогда сравнивала две РАЗНЫЕ конфигурации и печатала
# «поймана» даже для пустой порчи.
for a in false true; do
for mode in false true; do
  echo
  echo "===== прижатие=$mode  механизм А=$a ====="

  # --- КОНТРОЛЬ: без порчи обязаны совпасть -------------------------------
  "$PY" py_drive.py scenarios.txt "$D/py_${mode}_$a.csv" "$mode" "$a"
  "$JAVA" -cp "$D" PortDrive scenarios.txt "$D/java_${mode}_$a.csv" "$mode" "$a"
  if ! "$PY" compare.py "$D/py_${mode}_$a.csv" "$D/java_${mode}_$a.csv" --quiet; then
    echo "  КОНТРОЛЬ ПРОВАЛЕН: стороны расходятся БЕЗ порчи."
    echo "  Дальше мерить нечего — любая «пойманная порча» была бы этой же разницей."
    "$PY" compare.py "$D/py_${mode}_$a.csv" "$D/java_${mode}_$a.csv" | head -12
    exit 2
  fi
  echo "  контроль: без порчи расхождений нет"

  # --- ПОРЧИ: каждая обязана быть поймана ---------------------------------
  LIST="$MUTATIONS"
  [ "$a" = "true" ] && LIST="$MUTATIONS $MUT_A"
  for m in $LIST; do
    PORT_CHECK_MUTATE=$m "$PY" py_drive.py scenarios.txt "$D/py_${mode}_${a}_$m.csv" "$mode" "$a" 2>/dev/null
    if "$PY" compare.py "$D/py_${mode}_${a}_$m.csv" "$D/java_${mode}_$a.csv" --quiet; then
      echo "  ПОРЧА '$m' НЕ ПОЙМАНА — стенд слеп к этой константе"
      bad=$((bad+1))
    else
      echo "  порча '$m' поймана"
    fi
  done

  # КОНТРОЛЬ НАОБОРОТ: при выключенном механизме А подмена ЕГО констант не
  # должна менять ничего. Если меняет — значит он влияет и в выключенном виде.
  if [ "$a" = "false" ]; then
    for m in $MUT_A; do
      PORT_CHECK_MUTATE=$m "$PY" py_drive.py scenarios.txt "$D/py_off_$m.csv" "$mode" "$a" 2>/dev/null
      if ! "$PY" compare.py "$D/py_off_$m.csv" "$D/java_${mode}_$a.csv" --quiet; then
        echo "  ВЫКЛЮЧЕННЫЙ механизм А всё же влияет (порча '$m' видна)"
        bad=$((bad+1))
      else
        echo "  выключенный механизм А глух к '$m' — как и должен"
      fi
    done
  fi
done
done

echo
if [ $bad -ne 0 ]; then
  echo "СТЕНД НЕГОДЕН: $bad порч прошли незамеченными"
  exit 2
fi
echo "ИТОГ: перенос совпадает с офлайновым трекером в ОБОИХ режимах,"
echo "      и в обоих режимах стенд ловит все $(echo $MUTATIONS | wc -w) подмен констант"
