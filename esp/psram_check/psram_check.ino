/*
 * Один вопрос: WROOM или WROVER? От ответа зависит, свободны ли GPIO16/17.
 *
 * В шапке bt_link.ino это записано как неразрешимое: «по чипу (ESP32-D0WD-V3)
 * отличить WROOM от WROVER нельзя, PSRAM внешняя». По маркировке чипа —
 * действительно нельзя, но PSRAM отвечает сама, если её включить в сборке
 * (FQBN ...:PSRAM=enabled). Есть ответ — модуль WROVER, и 16/17 отданы памяти;
 * нет — WROOM, и они свободны как обычные выводы.
 */
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println();
  Serial.printf("# чип: %s, ревизия %d, ядер %d\n",
                ESP.getChipModel(), ESP.getChipRevision(), ESP.getChipCores());
  Serial.printf("# флеш: %u байт\n", ESP.getFlashChipSize());
  size_t ps = ESP.getPsramSize();
  Serial.printf("# PSRAM: %u байт -> %s\n", ps,
                ps ? "WROVER: GPIO16/17 ЗАНЯТЫ памятью" :
                     "WROOM: GPIO16/17 свободны");
}
void loop() { delay(1000); }
