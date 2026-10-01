#!/bin/bash
# Сборка APK без Gradle: aapt2 -> javac -> d8 -> zip -> zipalign -> apksigner.
#
# Gradle сюда не заводится намеренно: он потянул бы разрешение зависимостей из
# сети на каждой сборке и свой демон, а всё, что нужно, — четыре вызова
# инструментов SDK. Весь цикл сборки идёт из терминала.
set -eu
. ~/Android/env.sh
BT=$ANDROID_HOME/build-tools/35.0.0
JAR=$ANDROID_HOME/platforms/android-35/android.jar
LIB=$HOME/Android/libs/x
OUT=build
PKG=com.surftracker.verify

rm -rf $OUT && mkdir -p $OUT/classes $OUT/dex $OUT/apk/lib/arm64-v8a

# 1. ресурсов нет — линкуем только манифест
$BT/aapt2 link -o $OUT/base.apk --manifest AndroidManifest.xml -I $JAR \
    --min-sdk-version 26 --target-sdk-version 35

# 2. компиляция
$JAVA_HOME/bin/javac -source 17 -target 17 -nowarn \
    -classpath "$JAR:$LIB/classes.jar:$LIB/api/classes.jar" \
    -d $OUT/classes $(find src -name '*.java')

# 3. dex: наши классы + рантайм LiteRT
$BT/d8 --lib $JAR --min-api 26 --output $OUT/dex \
    $(find $OUT/classes -name '*.class') "$LIB/classes.jar" "$LIB/api/classes.jar"

# 4. собираем apk: dex + нативные библиотеки
cp $LIB/jni/arm64-v8a/*.so $OUT/apk/lib/arm64-v8a/
cp $OUT/dex/classes.dex $OUT/apk/
(cd $OUT/apk && zip -q -r ../unsigned.apk .)
(cd $OUT && zip -q ../$OUT/base.apk -j apk/classes.dex >/dev/null 2>&1 || true)
python3 - "$OUT" << 'PY'
import shutil, subprocess, sys, zipfile, os
out = sys.argv[1]
# кладём dex и .so внутрь слинкованного aapt2 apk, сохраняя его манифест
shutil.copy(f"{out}/base.apk", f"{out}/merged.apk")
with zipfile.ZipFile(f"{out}/merged.apk", "a", zipfile.ZIP_DEFLATED) as z:
    names = set(z.namelist())
    if "classes.dex" not in names:
        z.write(f"{out}/dex/classes.dex", "classes.dex")
    for so in os.listdir(f"{out}/apk/lib/arm64-v8a"):
        z.write(f"{out}/apk/lib/arm64-v8a/{so}", f"lib/arm64-v8a/{so}")
PY

# 5. ключ и подпись
KS=$HOME/Android/debug.keystore
[ -f "$KS" ] || $JAVA_HOME/bin/keytool -genkeypair -keystore "$KS" -storepass android \
    -keypass android -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US" >/dev/null 2>&1

$BT/zipalign -f -p 4 $OUT/merged.apk $OUT/aligned.apk
$BT/apksigner sign --ks "$KS" --ks-pass pass:android --key-pass pass:android \
    --out $OUT/$PKG.apk $OUT/aligned.apk
$BT/apksigner verify $OUT/$PKG.apk && echo "APK: $OUT/$PKG.apk ($(du -h $OUT/$PKG.apk|cut -f1))"
