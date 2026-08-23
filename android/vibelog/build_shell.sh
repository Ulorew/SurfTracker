#!/bin/bash
# Собирает только dex для запуска через app_process (без установки APK).
set -eu
cd "$(dirname "$0")"
. ~/Android/env.sh
BT=$ANDROID_HOME/build-tools/35.0.0
JAR=$ANDROID_HOME/platforms/android-35/android.jar
OUT=build_shell
rm -rf $OUT && mkdir -p $OUT/classes $OUT/dex
$JAVA_HOME/bin/javac -source 17 -target 17 -nowarn -classpath "$JAR" \
    -d $OUT/classes src/com/surftracker/vibelog/Shell.java
$BT/d8 --lib $JAR --min-api 29 --output $OUT/dex $(find $OUT/classes -name '*.class')
cp $OUT/dex/classes.dex $OUT/vibelog.dex
echo "DEX: $OUT/vibelog.dex ($(du -h $OUT/vibelog.dex|cut -f1))"
