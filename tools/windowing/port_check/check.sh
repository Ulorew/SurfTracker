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
BASE="k expand miss reacq tau minwin radcap raddiag"  # общие для всех режимов
MUT_L1="shrink alpha"               # альфа-бета и EMA размера: только уровень 1
MUT_A="lam lam0 veto veto0"         # механизм А: и величина, и наличие
MUT_K="rpos rlogh accel"            # Калман
MUT_G="chi2 gpred grad"             # гейт и две правки, влияющие на решение
MUT_SUB="gwarm"                     # подчинённая правка: обязана МОЛЧАТЬ, см. ниже
# Радиус приёма нужен ВО ВСЕХ режимах. Прежде он был помечен как ненужный при
# гейте — и это было верно, пока гейт ЗАМЕЩАЛ его. С правкой «только сужать»
# радиус снова участвует в решении, и стенд это сам заметил обратным контролем:
# «frac ВЛИЯЕТ, хотя в этом режиме не используется». Ожидание было устаревшим,
# а не поведение неверным.
MUT_RADIUS="frac"                   # радиус приёма: участвует и при гейте
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
    # Прогрев гейта ПОДЧИНЁН правилу «только сужать» и потому обязан молчать.
    # Пока гейт не может принять ничего за радиусом, ранние такты (когда его
    # допуск шире кадра) ничем не отличаются от поздних: решает радиус. Это
    # проверяется как обычный обратный контроль, а не замалчивается — и если
    # правило «только сужать» когда-нибудь выключат, проверка сразу заголосит.
    # Что правка при этом НЕ мертва, доказывается отдельной фазой в конце.
    LIST="$LIST $MUT_RADIUS"
    if [ "$gate" = "true" ]; then LIST="$LIST $MUT_G"; SILENT="$SILENT $MUT_SUB";
                            else SILENT="$SILENT $MUT_SUB"; fi
    if [ "$a" = "true" ]; then LIST="$LIST $MUT_A"; else SILENT="$SILENT $MUT_A"; fi

    for m in $LIST; do
      PORT_CHECK_MUTATE=$m "$PY" py_drive.py scenarios.txt "$D/m.csv" \
          "$mode" "$a" "$kal" "$gate" 2>/dev/null
      # ИМЕНА СЦЕНАРИЕВ, а не голое «поймана». Порча ловится ровно теми
      # сценариями, которые трогают её механизм, и увидеть этот список —
      # единственный способ заметить, что механизм проверен не тем местом.
      # Так и вскрылось, что вето ловилось только нижней половиной.
      where=$("$PY" compare.py "$D/m.csv" "$D/java.csv" --names) || true
      if [ "$where" = "-" ]; then
        echo "  ПОРЧА '$m' НЕ ПОЙМАНА"; bad=$((bad+1))
      else
        echo "  порча '$m' поймана на: $where"
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

# --- ПОДЧИНЁННАЯ ПРАВКА НЕ МЕРТВА -----------------------------------------
#
# Прогрев гейта молчит во всех клетках выше, и это ожидаемо. Но «молчит» и
# «его нет» с виду одно и то же — ровно та ошибка, из-за которой штраф
# механизма А тринадцать дней считался проверенным. Поэтому здесь снимается
# правило «только сужать» И ПОКАЗЫВАЕТСЯ, что прогрев тогда решает.
# Сличаются два ПИТОНОВСКИХ прогона: телефон эту комбинацию не умеет, и
# сличать с ним нечего — проверяется достижимость ветки, а не перенос.
echo
echo "===== подчинённость: прогрев при снятом правиле «только сужать» ====="
PORT_CHECK_MUTATE=grad "$PY" py_drive.py scenarios.txt "$D/a.csv" false false true true 2>/dev/null
PORT_CHECK_MUTATE=grad_gwarm "$PY" py_drive.py scenarios.txt "$D/b.csv" false false true true 2>/dev/null
if "$PY" compare.py "$D/a.csv" "$D/b.csv" --quiet; then
  echo "  ПРОГРЕВ НЕ ВЛИЯЕТ ДАЖЕ БЕЗ СУЖЕНИЯ — ветка недостижима, правка мертва"
  bad=$((bad+1))
else
  where=$("$PY" compare.py "$D/a.csv" "$D/b.csv" --names) || true
  echo "  прогрев решает на: $where — значит молчание выше от подчинённости,"
  echo "  а не от того, что механизма нет"
fi

echo
[ $bad -eq 0 ] || { echo "СТЕНД НЕГОДЕН: $bad порч прошли незамеченными"; exit 2; }
echo "ИТОГ: перенос совпадает с офлайновым трекером во ВСЕХ сочетаниях режимов,"
echo "      и в каждом сочетании стенд ловит подмены его собственных констант"
