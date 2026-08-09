/*
 * Векторы протокола v2 НА ЦЕЛЕВОМ ЖЕЛЕЗЕ.
 *
 * Зачем отдельной прошивкой, если на хосте уже сходится: CRC-8/MAXIM
 * отражённый — то место, где табличная реализация на хосте и побитовая на
 * MCU расходятся чаще всего. Тест на векторах ловит это за секунды, живой
 * контур — за вечер, причём выглядит расхождение как «канал молчит».
 *
 * Здесь Serial используется КАК ТЕКСТ — это тестовая сборка, протокола в
 * этом канале нет. В боевой прошивке любой Serial.print запрещён.
 */
#include <proto_v2.h>

static int failed = 0;

static void expect(const char *name, const uint8_t *got, const char *want, uint8_t n) {
  char buf[64];
  int p = 0;
  for (uint8_t i = 0; i < n; i++) p += sprintf(buf + p, "%02X ", got[i]);
  if (p > 0) buf[p - 1] = 0;
  bool ok = strcmp(buf, want) == 0;
  if (!ok) failed++;
  Serial.print(ok ? F("ok   ") : F("ПРОВАЛ "));
  Serial.print(name);
  Serial.print(F("  "));
  Serial.println(buf);
  if (!ok) { Serial.print(F("     ожидалось: ")); Serial.println(want); }
}

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}
  delay(300);
  Serial.println(F("\n=== векторы протокола v2 на целевом железе ==="));

  // Самопроверка алгоритма до векторов: если она падает, дальше смотреть
  // нечего — расходится сам CRC, а не упаковка полей.
  const uint8_t s[] = {'1','2','3','4','5','6','7','8','9'};
  uint8_t c = proto::crc8(s, 9);
  Serial.print(c == 0xA1 ? F("ok   ") : F("ПРОВАЛ "));
  Serial.print(F("CRC(\"123456789\")=0x"));
  Serial.println(c, HEX);
  if (c != 0xA1) failed++;

  uint8_t f[proto::TEL_LEN];
  proto::buildReq(f, 0, 0.0f, 0.0f);
  expect("уставка нулевая     ", f, "A5 00 00 00 00 00 00 00 00 00 17", proto::REQ_LEN);
  proto::buildReq(f, 1, 1.0f, 0.1f);
  expect("уставка w=1 wd=0.1  ", f, "A5 01 00 00 80 3F CD CC CC 3D 44", proto::REQ_LEN);
  proto::buildReq(f, 127, -2.0f, 0.5f);
  expect("уставка w=-2 wd=0.5 ", f, "A5 7F 00 00 00 C0 00 00 00 3F CD", proto::REQ_LEN);

  proto::buildTel(f, 0, 0.0f, 0.0f, proto::ST_ENC_OK);
  expect("телеметрия штатная  ", f, "5A 00 00 00 00 00 00 00 00 00 08 D5", proto::TEL_LEN);
  proto::buildTel(f, 42, 0.1745329f, 0.5f, 0x2A);
  expect("телеметрия статусная", f, "5A 2A C1 B8 32 3E 00 00 00 3F 2A 43", proto::TEL_LEN);
  proto::buildTel(f, 127, -1.0f, 0.0f, 0x19);
  expect("телеметрия watchdog ", f, "5A 7F 00 00 80 BF 00 00 00 00 19 21", proto::TEL_LEN);

  // Разбор своего же кадра: упаковка и распаковка обязаны быть обратными.
  proto::buildReq(f, 42, -0.75f, 0.25f);
  uint8_t seq = 0, ver = 9; float w = 0, wd = 0;
  bool ok = proto::parseReq(f, &seq, &w, &wd, &ver)
            && seq == 42 && ver == 0 && w == -0.75f && wd == 0.25f;
  if (!ok) failed++;
  Serial.println(ok ? F("ok   разбор своего кадра") : F("ПРОВАЛ разбор своего кадра"));

  // Порча одного байта обязана ронять CRC — иначе проверка декоративна.
  f[5] ^= 0x01;
  ok = !proto::parseReq(f, &seq, &w, &wd);
  if (!ok) failed++;
  Serial.println(ok ? F("ok   порченый кадр отвергнут") : F("ПРОВАЛ порченый кадр принят"));

  // Свежесть seq: окно вперёд 1..64 по модулю 128.
  struct { uint8_t seq, last; bool want; } fr[] = {
    {1, 0, true}, {64, 0, true}, {65, 0, false}, {0, 0, false},
    {0, 127, true}, {63, 127, true}, {127, 0, false},
  };
  for (auto &t : fr) {
    bool got = proto::isFresher(t.seq, t.last);
    if (got != t.want) {
      failed++;
      Serial.print(F("ПРОВАЛ свежесть seq=")); Serial.print(t.seq);
      Serial.print(F(" last=")); Serial.println(t.last);
    }
  }
  Serial.println(F("ok   свежесть seq (7 случаев)"));

  Serial.print(F("\nИТОГ: "));
  Serial.println(failed == 0 ? F("ВСЕ ВЕКТОРЫ СОШЛИСЬ") : F("ЕСТЬ ПРОВАЛЫ"));
}

void loop() { delay(1000); }
