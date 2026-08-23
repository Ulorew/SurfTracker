#!/bin/bash
# Сборка APK без Gradle, по образцу android/camfps/build.sh.
# Здесь проще: ресурсов нет, нативных библиотек нет, только манифест + один класс.
set -eu
cd "$(dirname "$0")"
. ~/Android/env.sh
BT=$ANDROID_HOME/build-tools/35.0.0
JAR=$ANDROID_HOME/platforms/android-35/android.jar
OUT=build
PKG=com.surftracker.vibelog

rm -rf $OUT && mkdir -p $OUT/classes $OUT/dex

$BT/aapt2 link -o $OUT/base.apk --manifest AndroidManifest.xml -I $JAR \
    --min-sdk-version 29 --target-sdk-version 35

$JAVA_HOME/bin/javac -source 17 -target 17 -nowarn \
    -classpath "$JAR" -d $OUT/classes $(find src -name '*.java')

$BT/d8 --lib $JAR --min-api 29 --output $OUT/dex $(find $OUT/classes -name '*.class')

python3 - "$OUT" << 'PY'
import shutil, sys, zipfile
out = sys.argv[1]
shutil.copy(f"{out}/base.apk", f"{out}/merged.apk")
with zipfile.ZipFile(f"{out}/merged.apk", "a", zipfile.ZIP_DEFLATED) as z:
    z.write(f"{out}/dex/classes.dex", "classes.dex")
PY

KS=$HOME/Android/debug.keystore
[ -f "$KS" ] || $JAVA_HOME/bin/keytool -genkeypair -keystore "$KS" -storepass android \
    -keypass android -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US" >/dev/null 2>&1

$BT/zipalign -f -p 4 $OUT/merged.apk $OUT/aligned.apk
$BT/apksigner sign --ks "$KS" --ks-pass pass:android --key-pass pass:android \
    --out $OUT/$PKG.apk $OUT/aligned.apk
$BT/apksigner verify $OUT/$PKG.apk && echo "APK: $OUT/$PKG.apk ($(du -h $OUT/$PKG.apk|cut -f1))"
