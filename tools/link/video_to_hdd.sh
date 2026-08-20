#!/bin/bash
# Перенос видеофайлов на HDD с заменой симлинками.
#
#   video_to_hdd.sh --проба <каталог>   — показать, что будет перенесено
#   video_to_hdd.sh <каталог>           — перенести
#
# ЗАЧЕМ. Видео занимает почти всё место проекта (19 ГБ из 20 в старых
# прогонах), но нужно редко: смотрят его глазами раз в несколько дней, а
# лежит оно на системном SSD, где место кончается. Выкинуть нельзя — это
# исходники разметки и записи прогонов.
#
# ПОФАЙЛОВО, А НЕ КАТАЛОГАМИ. Рядом с mp4 лежат метрики и jsonl — они лёгкие,
# читаются часто и должны остаться на быстром диске. Перенос каталога целиком
# утащил бы их за компанию.
#
# ПРОВЕРКА РАЗМЕРА ДО УДАЛЕНИЯ. Копия сверяется с оригиналом по байтам, и
# только после совпадения оригинал заменяется симлинком. NTFS через fuseblk
# умеет обрывать запись на переполнении молча, и «перенос» без сверки означал
# бы потерю исходников.
#
# ИМЕНА ПЕРЕМЕННЫХ ЛАТИНИЦЕЙ. Первая редакция звала их по-русски, как в
# питоновских инструментах проекта, и bash отказался: имя переменной у него
# только [A-Za-z_][A-Za-z0-9_]*, а кириллическое присваивание он разбирает как
# команду и падает с «No such file or directory». Комментарии это не касается.
set -eu

DEST="${VIDEO_HDD:-/mnt/sda3/Projects/SurfTracker/media_lm}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

DRY=0
if [ "${1:-}" = "--проба" ]; then DRY=1; shift; fi
TARGET="${1:-.}"

cd "$ROOT"
[ -d "$TARGET" ] || { echo "нет каталога $TARGET"; exit 1; }
mkdir -p "$DEST"

total=0; moved=0; skipped=0; bytes=0
while IFS= read -r -d '' f; do
    [ -L "$f" ] && { skipped=$((skipped+1)); continue; }   # уже перенесён
    size=$(stat -c %s "$f")
    total=$((total+1)); bytes=$((bytes+size))
    rel="${f#./}"
    dst="$DEST/$rel"
    if [ "$DRY" = "1" ]; then
        awk -v s="$size" -v p="$rel" 'BEGIN{printf "  %8.1f МБ  %s\n", s/1048576, p}'
        continue
    fi
    mkdir -p "$(dirname "$dst")"
    cp -n "$f" "$dst" 2>/dev/null || true
    there=$(stat -c %s "$dst" 2>/dev/null || echo 0)
    if [ "$there" != "$size" ]; then
        echo "  ОТКАЗ (размер не сошёлся, оригинал НЕ тронут): $rel"
        echo "        было $size, стало $there"
        continue
    fi
    rm -f "$f"
    ln -s "$dst" "$f"
    moved=$((moved+1))
    [ $((moved % 10)) -eq 0 ] && echo "  перенесено $moved..."
done < <(find "$TARGET" -type f \( -iname "*.mp4" -o -iname "*.avi" -o -iname "*.mov" -o -iname "*.mkv" \) -print0 2>/dev/null)

gb=$(awk -v b="$bytes" 'BEGIN{printf "%.1f", b/1073741824}')
echo
if [ "$DRY" = "1" ]; then
    echo "ПРОБА: файлов $total, суммарно $gb ГБ; уже перенесено ранее: $skipped"
else
    echo "перенесено $moved из $total ($gb ГБ), уже были симлинками: $skipped"
fi
echo "назначение: $DEST"
