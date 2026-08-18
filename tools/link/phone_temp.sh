#!/bin/bash
# ЖИВЫЕ температуры, а не кэш. В dumpsys thermalservice два блока, и первый
# ("Cached temperatures") — снимок с момента последнего теплового события: он
# может показывать 59 °C на давно остывшем аппарате. Читать надо второй.
/home/ulorew/Android/sdk/platform-tools/adb shell dumpsys thermalservice 2>/dev/null | tr -d '\r' | \
sed -n '/Current temperatures from HAL/,/^[A-Za-z].*:$/p' | \
grep -E "mName=(CPU|SKIN|BATTERY)" | \
sed 's/.*mValue=\([0-9.]*\).*mName=\([A-Z]*\).*/\2=\1/' | tr '\n' ' '
echo
