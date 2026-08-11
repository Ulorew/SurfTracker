/*
 * ESP32: мост Bluetooth <-> UART для контура телефон -> мотор.
 *
 * Протокол: docs/ПРОТОКОЛ_ТЕЛЕФОН_STM32.md, версия 2. Он байтовый, с магиком
 * и CRC, и ничего не знает про транспорт — поэтому смена USB на BT не
 * потребовала правок ни в прошивке STM32, ни в спецификации, ни в клиенте.
 * По той же причине переезд с Nucleo-F411RE на B-G431B-ESC1 прошёл мимо этого
 * файла целиком.
 *
 * Кадрирование и CRC берутся из stm/libraries/SurfProtoV2 — ОДНОГО заголовка
 * на обе прошивки. Своя копия здесь однажды уже разошлась бы: v1 и v2
 * отличаются полиномом CRC (0x07 против отражённого 0x31), и заглушка на
 * старом полиноме молча отвергала бы каждый кадр.
 *
 * Сборка: arduino-cli compile -b esp32:esp32:esp32 \
 *             --libraries ../stm/libraries bt_link
 *
 * Плата: ESP32-D0WD-V3 (классический ESP32, есть BT Classic). Мост сделан на
 * SPP, а не на BLE: Android поддерживает SPP штатно как последовательный
 * сокет, тогда как BLE потребовал бы своего GATT-сервиса с обеих сторон.
 *
 * ДВЕ РОЛИ, как и в прошивке STM32 — чтобы слои мерились по отдельности:
 *
 *   STUB_MODE 1 — ESP32 САМ отвечает по протоколу, STM32 не нужен вовсе.
 *                 Ради одного числа: сколько стоит Bluetooth. Полученные
 *                 период и RTT сравниваются с проводным эталоном
 *                 (потери 0/500, период p95 40.13 мс, RTT p95 2.80 мс), и
 *                 разница — цена транспорта, а не чья-то ещё.
 *   STUB_MODE 0 — прозрачный мост: байты BT <-> Serial2, без разбора.
 *                 Разбирать незачем: ресинхронизация по магику уже есть на
 *                 обоих концах, и лишний разбор посередине только добавил бы
 *                 своих ошибок.
 */

#include "BluetoothSerial.h"
#include <proto_v2.h>   // ТОТ ЖЕ заголовок, что у STM32: stm/libraries/SurfProtoV2

#define STUB_MODE 0

// ---------------------- параметры (в лог по §3) ----------------------
static const char*    BT_NAME   = "SurfTracker-Link";
static const uint32_t UART_BAUD = 115200;   // как в спецификации
// GPIO25/26, а НЕ 16/17. У модулей WROVER пины 16 и 17 заняты под внешнюю
// PSRAM, и на такой плате мост молча не заработал бы — а по чипу
// (ESP32-D0WD-V3) отличить WROOM от WROVER нельзя, PSRAM внешняя. 25 и 26
// свободны на всех вариантах: не участвуют в загрузке, не заняты флешем,
// не input-only. Цена выбора — ноль, цена ошибки — вечер отладки паяного
// соединения.
static const int      PIN_RX    = 25;       // Serial2 RX  <- TX STM32 (PB6 на ESC1)
static const int      PIN_TX    = 26;       // Serial2 TX  -> RX STM32 (PB7 на ESC1)
static const int      PIN_LED   = 2;

BluetoothSerial SerialBT;

#if STUB_MODE
static uint8_t  rxbuf[proto::REQ_LEN];
static uint8_t  rxn = 0;
static uint32_t last_rx_ms = 0;
static bool     had_first = false;
static bool     wd_latch = true;      // до первого кадра мотор бы стоял
static bool     crc_dropped = false;
static float    w_target = 0.0f;

static void rxShift() {
  for (uint8_t i = 1; i < rxn; i++) rxbuf[i - 1] = rxbuf[i];
  rxn--;
  while (rxn > 0 && rxbuf[0] != proto::MAGIC_REQ) {
    for (uint8_t i = 1; i < rxn; i++) rxbuf[i - 1] = rxbuf[i];
    rxn--;
  }
}

static void reply(uint8_t seq) {
  // theta синтетическая и ЗАВЕДОМО меняющаяся: постоянное значение нельзя
  // отличить от залипшего поля, а именно это и была одна из двух ошибок,
  // найденных проводным эхо-тестом.
  float theta = sinf(millis() * 0.001f) * 0.5f;
  uint8_t st = 0;
  if (wd_latch) st |= proto::ST_WATCHDOG;
  wd_latch = false;
  // ST_ENC_OK НЕ выставляется: энкодера здесь нет, и врать про него нельзя —
  // иначе заглушка будет выглядеть исправнее настоящего железа.
  if (fabsf(w_target) > VEL_LIMIT) st |= proto::ST_CLAMP;
  if (crc_dropped) st |= (uint8_t)(1 << proto::ST_CRC_SHIFT);
  crc_dropped = false;

  uint8_t out[proto::TEL_LEN];
  // w_ramp у заглушки равен уставке: рампы здесь нет и быть не должно —
  // заглушка мерит транспорт, а не поведение вала.
  proto::buildTel(out, seq, theta, w_target, st);
  SerialBT.write(out, proto::TEL_LEN);
}

static void pump() {
  while (SerialBT.available() > 0) {
    uint8_t b = (uint8_t)SerialBT.read();
    if (rxn == 0 && b != proto::MAGIC_REQ) continue;
    rxbuf[rxn++] = b;
    if (rxn < proto::REQ_LEN) continue;
    uint8_t seq = 0, ver = 0;
    float w = 0.0f, wd = 0.0f;
    if (!proto::parseReq(rxbuf, &seq, &w, &wd, &ver)) {
      crc_dropped = true;
      rxShift();
      continue;
    }
    rxn = 0;
    if (ver != proto::VERSION) continue;   // чужая версия — молчим
    w_target = w;
    last_rx_ms = millis();
    had_first = true;
    reply(seq);
  }
}
#endif

void setup() {
  pinMode(PIN_LED, OUTPUT);
  SerialBT.begin(BT_NAME);
#if !STUB_MODE
  Serial2.begin(UART_BAUD, SERIAL_8N1, PIN_RX, PIN_TX);
#endif
}

void loop() {
#if STUB_MODE
  pump();
  if (!had_first || (millis() - last_rx_ms) > WATCHDOG_MS) wd_latch = true;
  bool alive = had_first && (millis() - last_rx_ms) <= WATCHDOG_MS;
#else
  // Прозрачный мост. Байты гоняются немедленно, без накопления: буферизация
  // ради "эффективности" добавила бы задержки ровно там, где мы её меряем.
  while (SerialBT.available()) Serial2.write((uint8_t)SerialBT.read());
  while (Serial2.available())  SerialBT.write((uint8_t)Serial2.read());
  bool alive = SerialBT.hasClient();
#endif
  digitalWrite(PIN_LED, alive ? HIGH : ((millis() >> 8) & 1));
}
