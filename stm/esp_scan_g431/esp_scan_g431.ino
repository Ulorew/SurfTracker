/*
 * Где физически сидит передатчик ESP32?
 *
 * Нумерация hall-разъёма на этой плате уже оказалась зеркальной: датчик паяли
 * в «PB8», сигнал пришёл на PB6. Поэтому пины ищем по фактическому сигналу, а
 * не по маркировке.
 *
 * Приём: два окна по 5 секунд — без трафика по Bluetooth и с трафиком. На
 * PB8 постоянно сидит датчик (около 2170 фронтов в секунду), поэтому просто
 * «где есть активность» здесь не работает. Работает «где активность
 * ВЫРОСЛА»: датчик частоты не меняет, а UART добавляет свои фронты только
 * когда идут байты.
 */
#include <Arduino.h>

static const uint8_t PINS[]  = {PB6, PB7, PB8};
static const char*   NAMES[] = {"PB6", "PB7", "PB8"};
volatile uint32_t cnt[3] = {0, 0, 0};
void i0() { cnt[0]++; }
void i1() { cnt[1]++; }
void i2() { cnt[2]++; }
static void (*ISR3[3])() = {i0, i1, i2};

void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) { }
  Serial.println();
  Serial.println(F("=== поиск передатчика ESP по фронтам ==="));
  for (uint8_t i = 0; i < 3; i++) {
    pinMode(PINS[i], INPUT);
    attachInterrupt(digitalPinToInterrupt(PINS[i]), ISR3[i], CHANGE);
  }

  Serial.println(F("окно 1 (5 с): БЕЗ трафика"));
  uint32_t a[3];
  delay(5000);
  for (uint8_t i = 0; i < 3; i++) a[i] = cnt[i];
  for (uint8_t i = 0; i < 3; i++) {
    Serial.print(F("  ")); Serial.print(NAMES[i]);
    Serial.print(F(": ")); Serial.println(a[i]);
  }

  Serial.println(F("окно 2 (6 с): ШЛИТЕ трафик"));
  delay(6000);
  uint32_t b[3];
  for (uint8_t i = 0; i < 3; i++) b[i] = cnt[i];

  Serial.println();
  Serial.println(F("=== ИТОГ: прирост за окно 2 ==="));
  uint8_t best = 0xFF; int32_t bestd = 0;
  for (uint8_t i = 0; i < 3; i++) {
    int32_t d1 = (int32_t)a[i];                 // за 5 с
    int32_t d2 = (int32_t)(b[i] - a[i]);        // за 6 с
    int32_t expect = d1 * 6 / 5;                // если бы ничего не изменилось
    int32_t growth = d2 - expect;
    Serial.print(F("  ")); Serial.print(NAMES[i]);
    Serial.print(F(": было ")); Serial.print(d1);
    Serial.print(F("/5с, стало ")); Serial.print(d2);
    Serial.print(F("/6с, прирост ")); Serial.println(growth);
    if (growth > bestd) { bestd = growth; best = i; }
  }
  if (best != 0xFF && bestd > 200) {
    Serial.print(F("ПЕРЕДАТЧИК ESP сидит на ")); Serial.println(NAMES[best]);
  } else {
    Serial.println(F("прироста нет НИ НА ОДНОМ пине:"));
    Serial.println(F("  либо ESP не передаёт, либо провод не доходит до платы"));
  }
}

void loop() { }
