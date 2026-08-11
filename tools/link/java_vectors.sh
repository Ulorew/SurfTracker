#!/bin/bash
# Векторы протокола v2 для реализации телефона. Ноутбук, обычный javac —
# ProtoV2 намеренно не зависит от android.*, ловить ошибку кадрирования на
# устройстве незачем.
set -eu
# JDK берётся из окружения сборки APK, если оно есть: системного javac на
# этой машине нет, а тот же JDK гарантирует ту же версию языка, что и в APK.
[ -f "$HOME/Android/env.sh" ] && . "$HOME/Android/env.sh"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
javac -encoding UTF-8 -d "$OUT" \
    "$ROOT/android/camfps/src/com/surftracker/camfps/ProtoV2.java" \
    "$ROOT/tools/link/JavaVectors.java"
java -cp "$OUT" JavaVectors
