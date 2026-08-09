/*
 * ESP32: мост Bluetooth <-> UART для контура телефон -> мотор.
 *
 * Протокол не меняется ни на байт: docs/ПРОТОКОЛ_ТЕЛЕФОН_STM32.md. Он
 * байтовый, с магиком и CRC, и ничего не знает про транспорт — поэтому
 * смена USB на BT не требует правок ни в прошивке STM32, ни в спецификации,
 * ни в клиенте.
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

#define STUB_MODE 1

// ---------------------- параметры (в лог по §3) ----------------------
static const char*    BT_NAME   = "SurfTracker-Link";
static const uint32_t UART_BAUD = 115200;   // как в спецификации
// GPIO25/26, а НЕ 16/17. У модулей WROVER пины 16 и 17 заняты под внешнюю
// PSRAM, и на такой плате мост молча не заработал бы — а по чипу
// (ESP32-D0WD-V3) отличить WROOM от WROVER нельзя, PSRAM внешняя. 25 и 26
// свободны на всех вариантах: не участвуют в загрузке, не заняты флешем,
// не input-only. Цена выбора — ноль, цена ошибки — вечер отладки паяного
// соединения.
static const int      PIN_RX    = 25;       // Serial2 RX  <- TX STM32 (PB6, D10)
static const int      PIN_TX    = 26;       // Serial2 TX  -> RX STM32 (PA10, D2)
static const int      PIN_LED   = 2;

// ---------------------- протокол ----------------------
static const uint8_t MAGIC    = 0xA5;
static const uint8_t REQ_LEN  = 7;
static const uint8_t RESP_LEN = 8;

static const uint8_t ST_WATCHDOG = 1 << 0;
static const uint8_t ST_ENC_OK   = 1 << 1;
static const uint8_t ST_VEL_CLIP = 1 << 2;
static const uint8_t ST_CRC_DROP = 1 << 3;

static const uint32_t WATCHDOG_MS = 300;
static const float    VEL_LIMIT   = 20.0f;

BluetoothSerial SerialBT;

static uint8_t crc8(const uint8_t *d, uint8_t n) {
  uint8_t c = 0x00;
  while (n--) {
    c ^= *d++;
    for (uint8_t i = 0; i < 8; i++)
      c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x07) : (uint8_t)(c << 1);
  }
  return c;
}

#if STUB_MODE
static uint8_t  rxbuf[REQ_LEN];
static uint8_t  rxn = 0;
static uint32_t last_rx_ms = 0;
static bool     had_first = false;
static bool     wd_latch = true;      // до первого кадра мотор бы стоял
static bool     crc_dropped = false;
static float    w_target = 0.0f;

static void rxShift() {
  for (uint8_t i = 1; i < rxn; i++) rxbuf[i - 1] = rxbuf[i];
  rxn--;
  while (rxn > 0 && rxbuf[0] != MAGIC) {
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
  if (wd_latch) st |= ST_WATCHDOG;
  wd_latch = false;
  // ST_ENC_OK НЕ выставляется: энкодера здесь нет, и врать про него нельзя —
  // иначе заглушка будет выглядеть исправнее настоящего железа.
  if (fabsf(w_target) > VEL_LIMIT) st |= ST_VEL_CLIP;
  if (crc_dropped) st |= ST_CRC_DROP;
  crc_dropped = false;

  uint8_t out[RESP_LEN];
  out[0] = MAGIC;
  out[1] = seq;
  memcpy(&out[2], &theta, 4);
  out[6] = st;
  out[7] = crc8(out, RESP_LEN - 1);
  SerialBT.write(out, RESP_LEN);
}

static void pump() {
  while (SerialBT.available() > 0) {
    uint8_t b = (uint8_t)SerialBT.read();
    if (rxn == 0 && b != MAGIC) continue;
    rxbuf[rxn++] = b;
    if (rxn < REQ_LEN) continue;
    if (crc8(rxbuf, REQ_LEN - 1) != rxbuf[REQ_LEN - 1]) {
      crc_dropped = true;
      rxShift();
      continue;
    }
    memcpy(&w_target, &rxbuf[2], 4);
    last_rx_ms = millis();
    had_first = true;
    reply(rxbuf[1]);
    rxn = 0;
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
