/*
 * Проверка ОДНОГО направления: STM -> ESP -> Bluetooth.
 *
 * Шлёт телеметрию сама, без запроса, десять раз в секунду. Приёмник USART1
 * при этом не участвует вовсе — если кадры доедут до ноутбука, значит провод
 * PB6 и приёмник ESP исправны, и неисправность сузилась до второго провода.
 * Если не доедут — линии, скорее всего, соединены выход в выход.
 */
#include <proto_v2.h>

HardwareSerial LinkUart(PB7, PB6);   // RX, TX

void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) { }
  Serial.println();
  Serial.println(F("=== проба передатчика: шлю телеметрию без запроса ==="));
  LinkUart.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
}

void loop() {
  static uint32_t last = 0;
  static uint8_t seq = 0;
  static uint32_t n = 0;
  if (millis() - last >= 100) {
    last = millis();
    uint8_t out[proto::TEL_LEN];
    proto::buildTel(out, seq, 1.2345f, 0.0f, proto::ST_ENC_OK);
    LinkUart.write(out, proto::TEL_LEN);
    seq = (uint8_t)((seq + 1) & 0x7F);
    n++;
    digitalWrite(LED_BUILTIN, (n >> 1) & 1);
    if (n % 10 == 0) { Serial.print(F("отправлено кадров: ")); Serial.println(n); }
  }
}
