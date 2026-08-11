/*
 * B-G431B-ESC1: диагностика звена ESP32 <-> STM32.
 *
 * Положение: по VCP прошивка отвечает 50 из 50, по Bluetooth — ноль. Значит
 * рвётся между ESP и STM. Но направлений там два, и лечатся они по-разному:
 *
 *   байты на USART1 приходят  -> BT -> ESP -> STM работает, ищем обратное;
 *   байтов нет                -> не работает прямое: RX STM, TX ESP, или
 *                                перепутаны накрест.
 *
 * Отчёт идёт в Serial (USART2 -> встроенный ST-LINK VCP), а протокол живёт на
 * USART1. На этой плате это РАЗНЫЕ каналы, поэтому печатать во время работы
 * бинарного протокола наконец можно. На Nucleo так было нельзя, и подобную
 * неисправность пришлось бы ловить миганием.
 *
 * Считаются СЫРЫЕ байты, а не только разобранные кадры. Если провод звенит и
 * приходит мусор, счётчик байтов вырастет, а счётчик кадров нет — это другая
 * болезнь, чем полная тишина, и путать их нельзя.
 */
#include <proto_v2.h>

#define LINK_RX_PIN PB7    // A_HALL2, USART1_RX  <- TX ESP32 (GPIO26)
#define LINK_TX_PIN PB6    // A_HALL1, USART1_TX  -> RX ESP32 (GPIO25)

HardwareSerial LinkUart(LINK_RX_PIN, LINK_TX_PIN);

static uint32_t bytes_rx = 0;      // сырые байты
static uint32_t frames_ok = 0;     // разобранные кадры
static uint32_t crc_bad = 0;       // магик совпал, CRC нет
static uint32_t magic_bad = 0;     // байт не на месте магика
static uint32_t tel_sent = 0;

static uint8_t rxbuf[proto::REQ_LEN];
static uint8_t rxn = 0;
static uint8_t last_bytes[8];
static uint8_t lb_i = 0;

void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) { }
  Serial.println();
  Serial.println(F("=== диагностика звена ESP <-> STM ==="));
  Serial.println(F("протокол на USART1 (PB6 TX, PB7 RX), отчёт здесь"));

  LinkUart.begin(115200);

  pinMode(LED_BUILTIN, OUTPUT);
  Serial.println(F("жду кадры. Подключайтесь по Bluetooth и шлите."));
  Serial.println();
}

void loop() {
  static uint32_t last = 0;

  while (LinkUart.available() > 0) {
    uint8_t b = (uint8_t)LinkUart.read();
    bytes_rx++;
    last_bytes[lb_i] = b;
    lb_i = (uint8_t)((lb_i + 1) % 8);

    if (rxn == 0 && b != proto::MAGIC_REQ) { magic_bad++; continue; }
    rxbuf[rxn++] = b;
    if (rxn < proto::REQ_LEN) continue;

    uint8_t seq = 0, ver = 0;
    float w = 0, wd = 0;
    if (!proto::parseReq(rxbuf, &seq, &w, &wd, &ver)) {
      crc_bad++;
      // сдвиг на байт: выравнивание по длине залипло бы навсегда
      for (uint8_t i = 1; i < rxn; i++) rxbuf[i - 1] = rxbuf[i];
      rxn--;
      continue;
    }
    rxn = 0;
    frames_ok++;

    // Отвечаем настоящей телеметрией: если ответ не дойдёт до ноутбука, но
    // счётчик здесь растёт — неисправно обратное направление, и это будет
    // видно сразу.
    uint8_t out[proto::TEL_LEN];
    proto::buildTel(out, seq, 1.2345f, w, proto::ST_ENC_OK);
    LinkUart.write(out, proto::TEL_LEN);
    tel_sent++;
  }

  if (millis() - last >= 1000) {
    last = millis();
    Serial.print(F("байт "));    Serial.print(bytes_rx);
    Serial.print(F("  кадров ")); Serial.print(frames_ok);
    Serial.print(F("  CRC- "));   Serial.print(crc_bad);
    Serial.print(F("  не-магик ")); Serial.print(magic_bad);
    Serial.print(F("  отправлено ")); Serial.print(tel_sent);
    if (bytes_rx) {
      Serial.print(F("  последние: "));
      for (uint8_t i = 0; i < 8; i++) {
        uint8_t b = last_bytes[(lb_i + i) % 8];
        if (b < 16) Serial.print('0');
        Serial.print(b, HEX); Serial.print(' ');
      }
    }
    Serial.println();
    digitalWrite(LED_BUILTIN, bytes_rx ? HIGH : !digitalRead(LED_BUILTIN));
  }
}
