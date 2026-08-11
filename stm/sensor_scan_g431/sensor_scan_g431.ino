/*
 * B-G431B-ESC1: поиск сигнала датчика на hall-разъёме.
 *
 * Предыдущая проверка дала ноль фронтов на PB8. Причин у этого несколько, и
 * они требуют разных действий, поэтому сначала — разделить их, а не гадать.
 *
 * Опрашиваются ВСЕ ТРИ пина разъёма (PB6, PB7, PB8) одновременно, плюс
 * снимается статический уровень каждого с подтяжкой вниз и вверх. Это даёт
 * три разных исхода и три разных вывода:
 *
 *   фронты есть на каком-то пине  -> перепутан вывод разъёма, лечится
 *                                    заменой одной строки в прошивке;
 *   везде тихо, уровень следует
 *   за подтяжкой                  -> линия ВИСИТ. Датчик не подключён к этому
 *                                    пину, не запитан, или у него нет выхода
 *                                    ШИМ;
 *   везде тихо, уровень НЕ следует
 *   за подтяжкой                  -> линия чем-то удерживается: датчик
 *                                    подключён и питается, но молчит.
 *
 * Мотор не трогается: драйвер не инициализируется вовсе.
 */
#include <Arduino.h>

static const uint8_t PINS[] = {PB6, PB7, PB8};
static const char*   NAMES[] = {"PB6 (A_HALL1)", "PB7 (A_HALL2)", "PB8 (A_HALL3)"};
static const uint8_t N = 3;

volatile uint32_t cnt[N] = {0, 0, 0};

void isr0() { cnt[0]++; }
void isr1() { cnt[1]++; }
void isr2() { cnt[2]++; }
static void (*ISRS[N])() = {isr0, isr1, isr2};

static void levels(const __FlashStringHelper* title, uint32_t mode) {
  Serial.print(title);
  for (uint8_t i = 0; i < N; i++) {
    pinMode(PINS[i], mode);
  }
  delay(5);
  for (uint8_t i = 0; i < N; i++) {
    Serial.print(F("  "));
    Serial.print(NAMES[i]);
    Serial.print(F("="));
    Serial.print(digitalRead(PINS[i]) ? F("1") : F("0"));
  }
  Serial.println();
}

void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 2000) { }

  Serial.println();
  Serial.println(F("=== ESC1: поиск сигнала на hall-разъёме ==="));
  Serial.println();

  // Статические уровни. Если линия висит, показание следует за подтяжкой;
  // если её кто-то держит — не следует. Это и есть различение «не подключено»
  // от «подключено, но молчит».
  levels(F("с подтяжкой ВНИЗ: "), INPUT_PULLDOWN);
  levels(F("с подтяжкой ВВЕРХ:"), INPUT_PULLUP);

  Serial.println();
  Serial.println(F("Теперь считаю фронты на всех трёх пинах, 15 с."));
  Serial.println(F("ПОВЕРНИТЕ вал рукой — если датчик жив, фронты пойдут."));
  Serial.println();

  for (uint8_t i = 0; i < N; i++) {
    pinMode(PINS[i], INPUT);
    attachInterrupt(digitalPinToInterrupt(PINS[i]), ISRS[i], CHANGE);
  }
}

void loop() {
  static uint32_t t_start = 0;
  static uint32_t last = 0;
  static bool done = false;
  if (t_start == 0) { t_start = millis(); last = t_start; }
  if (done) return;

  if (millis() - last >= 1000) {
    last = millis();
    Serial.print(F("  t=")); Serial.print((millis() - t_start) / 1000);
    Serial.print(F("с "));
    for (uint8_t i = 0; i < N; i++) {
      Serial.print(F("  ")); Serial.print(NAMES[i]);
      Serial.print(F(": ")); Serial.print(cnt[i]);
    }
    Serial.println();
  }

  if (millis() - t_start >= 15000) {
    done = true;
    Serial.println();
    Serial.println(F("=== ИТОГ ==="));
    uint8_t live = 0xFF;
    for (uint8_t i = 0; i < N; i++) {
      Serial.print(NAMES[i]); Serial.print(F(": фронтов "));
      Serial.println(cnt[i]);
      if (cnt[i] > 100) live = i;
    }
    if (live != 0xFF) {
      Serial.print(F("СИГНАЛ НАЙДЕН на ")); Serial.println(NAMES[live]);
      Serial.println(F("Лечится заменой SENSOR_PIN в прошивке."));
    } else {
      Serial.println(F("Сигнала нет ни на одном пине разъёма."));
      Serial.println(F("Смотреть уровни выше: следуют за подтяжкой = линия висит."));
    }
  }
}
