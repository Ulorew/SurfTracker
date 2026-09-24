/*
 * ESP32: боевая прошивка. Телефон -> Bluetooth -> ЭТА плата -> мотор 3506.
 *
 * ЗАМЕНЯЕТ ЦЕПЬ «ESP32-мост -> UART -> STM32». STM32 из цепи ушёл, и ESP32
 * теперь сам и принимает протокол, и крутит мотор. Телефонная сторона
 * (android/camfps, TrackActivity) не меняется НИ В ОДНОМ БАЙТЕ: то же имя
 * устройства SurfTracker-Link, тот же SPP, тот же протокол v2.
 *
 * ИЗ ЧЕГО СОБРАНО — и почему ничего не написано заново:
 *   proto_v2.h   — кадры и CRC. ОДИН заголовок на все прошивки и тесты.
 *   control_v2.h — свежесть seq, экстраполяция, потолок, сторож, рампа,
 *                  детектор срыва. Проверен на ноутбуке вместе с четырьмя
 *                  мутациями (tools/link/control_tests.sh). Сюда не
 *                  скопирован, а подключён: поедет тот же код, что тестирован.
 *   foc_bench    — мотор: пины, датчик, коэффициенты, компенсация зубцов.
 *                  Значения замерены 18-19 сентября (docs/foc_tuning).
 *   phone_link   — связка на STM32: медиана в телеметрию, ответ на КАЖДЫЙ
 *                  кадр, одна точка решения о питании фаз.
 *
 * ГЛАВНОЕ ОТЛИЧИЕ ОТ STM32: контур ЗАМКНУТ по скорости. Там был разомкнутый
 * velocity_openloop, и детектор срыва был единственной диагностикой. Здесь
 * вал ведёт ПИ по энкодеру, а детектор остаётся свидетелем: угол против
 * интеграла команды. Ветер, придержавший камеру, он увидит так же.
 *
 * ЗНАК. Спецификация: положительная ω -> вал ПРОТИВ часовой стрелки, если
 * смотреть сверху (со стороны камеры). Замерено на СТАРОЙ цепи. Новый мотор
 * и драйвер обязаны пройти замер заново: ошибка знака в слежении не даёт ни
 * ошибки, ни предупреждения — камера уезжает от цели, и это выглядит как
 * плохой коэффициент. Константа DIR ниже НЕ ЗАМЕРЕНА до стендового теста.
 *
 * ПОЛЕ. Калибровка initFOC при КАЖДОМ включении: вал коротко дёрнется. Это
 * сознательно — стабильность zero_electric_angle между включениями питания
 * не замерена (висит с 5 сентября), а калибровка на старте снимает вопрос.
 * После калибровки поле СНЯТО и включается только первым кадром с телефона.
 *
 * USB-SERIAL — диагностика, не управление в бою:
 *   STATE                  всё состояние одной строкой
 *   TEST v=0.3 t=3000      подать уставку через ТОТ ЖЕ путь, что у телефона
 *                          (accept -> рампа -> DIR -> мотор). Отказ, если
 *                          телефон подключён: два источника не смешиваются.
 *   SET P= I= Tf= COGK=    правка на месте, без перепрошивки
 *
 * Сборка:  esp/build.sh field_link [--прошить]
 */

#include <SimpleFOC.h>
#include "BluetoothSerial.h"
#include <proto_v2.h>
#include <control_v2.h>

// ---------------------------- железо ----------------------------
// Пины — как в foc_bench (там и объяснение переезда SPI на левый ряд).
// GPIO 12 НЕ ЗАНИМАТЬ: strapping, при загрузке обязан быть низким.
static const int PIN_IN1 = 17, PIN_IN2 = 16, PIN_IN3 = 4, PIN_EN = 22;
static const int PIN_SCK = 14, PIN_MISO = 27, PIN_MOSI = 26, PIN_CS = 25;
static const int PIN_LED = 2;

static const float POLE_PAIRS = 11;       // замерено, не из паспорта
static const float V_SUPPLY   = 12.0f;
static const float V_LIMIT    = 2.0f;     // правило владельца для долгой работы
static const float V_ALIGN    = 1.0f;

// ЗНАК: +1 или -1. Применяется к команде И к углу телеметрии одновременно,
// поэтому детектор срыва согласован при любом DIR.
//
// -1 ВЫВЕДЕН ИЗ ВИДЕО 18 сентября: при положительной команде move() сцена в
// кадре ехала влево (панорама -10 px/кадр), то есть камера поворачивала ПО
// часовой, если смотреть сверху. Спецификация требует обратного. Проводка
// фаз с тех пор не менялась, а направление move() задаётся именно ею: initFOC
// подгоняет знак датчика под поле, а не наоборот.
// ГЛАЗАМИ НЕ ПОДТВЕРЖДЁН — до подтверждения это вывод, а не замер.
static const int DIR = -1;

static const char* BT_NAME = "SurfTracker-Link";

// ---------------------------- поведение ----------------------------
// Поле снимается, если связи нет дольше этого (после того как рампа уже
// привела уставку к нулю по сторожу). Держать камеру под током без телефона
// незачем, а под ветром поле без команды только греет обмотки.
static const uint32_t LINK_LOST_OFF_MS = 3000;

// Замёрзший датчик. Известная ловушка SimpleFOC 2.4.0: при замершем датчике
// getVelocity() вечно отдаёт последнюю ненулевую скорость, и ПИ ведёт вал
// вслепую. Признак: сырой угол не меняется, хотя команда ненулевая. На самой
// медленной рабочей скорости 0.02 рад/с один шаг 14-битного датчика
// (0.00038 рад) проходится за 19 мс; 500 мс — запас в 25 раз.
static const uint32_t FREEZE_MS   = 500;
static const float    FREEZE_MINW = 0.02f;

// Медиана угла в телеметрию: 5 отсчётов через 20 мс (спецификация §5).
static const uint8_t  MED_N = 5;
static const uint32_t MED_STEP_MS = 20;

// Компенсация зубцов, 18 сентября: две гармоники, знак +1 измерен.
static const float COG_A44 = 0.0499f, COG_F44 = 19.7f * PI / 180.0f;
static const float COG_A22 = 0.0193f, COG_F22 = 68.4f * PI / 180.0f;
static const float COG_MAX = COG_A44 + COG_A22;
static float cog_k = 1.0f;

// ---------------------------- объекты ----------------------------
BLDCMotor         motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver3PWM    driver = BLDCDriver3PWM(PIN_IN1, PIN_IN2, PIN_IN3, PIN_EN);
MagneticSensorSPI sensor = MagneticSensorSPI(AS5048_SPI, PIN_CS);
SPIClass          spi(VSPI);
BluetoothSerial   SerialBT;
static ctl::Ctl   C;

static int      dirS = 1;              // sensor_direction после initFOC
static bool     field_on = false;
static bool     enc_fault = false;     // защёлка до сброса платы
static uint32_t lost_since_ms = 0;
static uint32_t crc_err_count = 0, rx_frames = 0, tx_frames = 0;
static uint32_t loop_prev_us = 0, loop_cnt = 0, loop_hz = 0, loop_t0 = 0;
static uint32_t loop_max_us = 0, loop_max_shown = 0;

// ---------------------------- угол ----------------------------
// Угол В СИСТЕМЕ ПРОТОКОЛА: знак вала (sensor_direction) и DIR вместе. Тот
// же угол идёт и в детектор срыва, и в телеметрию — иначе детектор сравнивал
// бы угол с интегралом команды другого знака и сработал бы на ровном ходу.
static inline float thetaProto() { return (float)DIR * dirS * sensor.getAngle(); }

static float    med_buf[MED_N];
static uint8_t  med_i = 0;
static bool     med_full = false;
static uint32_t med_last_ms = 0;

static float median5(const float* a, uint8_t n) {
  float c[MED_N];
  for (uint8_t i = 0; i < n; i++) c[i] = a[i];
  for (uint8_t i = 0; i <= n / 2; i++) {
    uint8_t m = i;
    for (uint8_t j = i + 1; j < n; j++) if (c[j] < c[m]) m = j;
    float t = c[i]; c[i] = c[m]; c[m] = t;
  }
  return c[n / 2];
}

static float thetaFiltered() {
  return med_full ? median5(med_buf, MED_N)
                  : (med_i ? med_buf[med_i - 1] : thetaProto());
}

// ---------------------------- датчик жив? ----------------------------
static float    frz_last_raw = 0.0f;
static uint32_t frz_since_ms = 0;

static bool encoderOk() {
  float a = sensor.getMechanicalAngle();
  return !enc_fault && !isnan(a) && a >= 0.0f && a <= 2.0f * PI + 1e-3f;
}

static void freezeCheck() {
  float raw = sensor.getMechanicalAngle();
  uint32_t now = millis();
  if (raw != frz_last_raw || !field_on || fabsf(C.w_ramp) < FREEZE_MINW) {
    frz_last_raw = raw; frz_since_ms = now;
    return;
  }
  if (now - frz_since_ms > FREEZE_MS && !enc_fault) {
    enc_fault = true;
    Serial.printf("# ОТКАЗ ДАТЧИКА: угол не меняется %lu мс при команде %.3f рад/с, "
                  "поле снято до сброса\n", (unsigned long)(now - frz_since_ms), C.w_ramp);
  }
}

// ---------------------------- приём ----------------------------
static uint8_t rxbuf[proto::REQ_LEN];
static uint8_t rxn = 0;

static void rxShift() {
  for (uint8_t i = 1; i < rxn; i++) rxbuf[i - 1] = rxbuf[i];
  rxn--;
  while (rxn > 0 && rxbuf[0] != proto::MAGIC_REQ) {
    for (uint8_t i = 1; i < rxn; i++) rxbuf[i - 1] = rxbuf[i];
    rxn--;
  }
}

static void sendTelemetry(uint8_t seq) {
  uint8_t st = C.statusByte(encoderOk());
  st |= (uint8_t)((crc_err_count & 0x03) << proto::ST_CRC_SHIFT);
  uint8_t out[proto::TEL_LEN];
  proto::buildTel(out, seq, thetaFiltered(), C.w_ramp, st);
  // Писать только при живом клиенте: запись в отсутствие читателя может
  // встать на очереди стека и остановить цикл мотора.
  if (SerialBT.hasClient()) { SerialBT.write(out, proto::TEL_LEN); tx_frames++; }
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
      crc_err_count++;
      rxShift();
      continue;
    }
    rxn = 0;
    if (ver != proto::VERSION) continue;
    if (isnan(w) || isinf(w) || isnan(wd) || isinf(wd)) { crc_err_count++; continue; }
    rx_frames++;
    // Применить или отвергнуть решает закон управления; ответ — в любом случае,
    // иначе телефон не сопоставит кадр по seq и не посчитает потери.
    C.accept(seq, w, wd, millis());
    sendTelemetry(seq);
  }
}

// ---------------------------- тест с USB ----------------------------
// Кадры подаются в ТОТ ЖЕ accept(), что и с телефона, раз в 50 мс. Кончился
// тест — кадры кончаются, и вал останавливает сторож через рампу, то есть
// ровно так, как при обрыве связи в поле. Бесплатная проверка сторожа.
static bool     test_on = false;
static float    test_v = 0.0f;
static uint32_t test_until = 0, test_next = 0;
static uint8_t  test_seq = 0;

// ЗАПИСЬ В ПАМЯТЬ во время теста: угол, команда, поле, срыв — 200 Гц, без
// единой печати, пока вал крутится. Печать по ходу возмущала сам цикл, и
// неровность хода нельзя было отличить от неровности, внесённой замером.
// Выгрузка — после конца теста плюс REC_TAIL_MS, чтобы захватить останов по
// сторожу и снятие поля.
// Память — из кучи при старте, компактно: статический массив на 96 КБ не
// влез в DRAM рядом со стеком Bluetooth (ошибка компоновщика).
static const uint16_t REC_MAX = 6000;       // 30 с при 200 Гц
static const uint32_t REC_TAIL_MS = 5000;
static float*   rec_th = nullptr;
static int16_t* rec_w  = nullptr;           // команда в мрад/с
static uint8_t* rec_fl = nullptr;
static uint16_t rec_n = 0;
static bool     rec_on = false;
static uint32_t rec_next_us = 0, rec_t0_ms = 0, rec_stop_ms = 0;

static void recPump() {
  if (!rec_on) return;
  uint32_t now = micros();
  if ((int32_t)(now - rec_next_us) >= 0 && rec_n < REC_MAX && rec_th) {
    rec_next_us += 5000;
    rec_th[rec_n] = thetaProto();
    rec_w[rec_n]  = (int16_t)lroundf(C.w_ramp * 1000.0f);
    rec_fl[rec_n] = (uint8_t)(field_on | (C.st_watchdog << 1) | (C.st_slip << 2) | (enc_fault << 3));
    rec_n++;
  }
  if (!test_on && (int32_t)(millis() - rec_stop_ms) >= 0) {
    rec_on = false;
    Serial.printf("#REC n=%u fs=200 loop_max_us=%lu\n", rec_n, (unsigned long)loop_max_shown);
    for (uint16_t i = 0; i < rec_n; i++)
      Serial.printf("%.6f,%.3f,%u\n", rec_th[i], rec_w[i] * 0.001f, rec_fl[i]);
    Serial.println("#REC_END");
  }
}

static void testPump() {
  if (!test_on) return;
  uint32_t now = millis();
  if ((int32_t)(now - test_until) >= 0) {
    test_on = false;
    rec_stop_ms = now + REC_TAIL_MS;         // печать — только после хвоста
    return;
  }
  if ((int32_t)(now - test_next) >= 0) {
    test_next = now + 50;
    test_seq = (uint8_t)((test_seq + 1) & 0x7F);
    C.accept(test_seq, test_v, 0.0f, now);
  }
}

// ---------------------------- USB-команды ----------------------------
static char line[96];
static uint8_t line_n = 0;

static float arg(const char* s, const char* key, float def) {
  char pat[12]; snprintf(pat, sizeof(pat), "%s=", key);
  const char* p = strstr(s, pat);
  if (!p) return def;
  // «P=» не должен находиться внутри «COGK=»-подобных ключей: требуем начало
  // строки или пробел перед ключом.
  if (p != s && p[-1] != ' ') return def;
  return atof(p + strlen(pat));
}

static void sayState() {
  Serial.printf("# STATE DIR=%d dirS=%d zea=%.4f field=%d link=%d wd=%d w_ramp=%.4f "
                "theta=%.4f enc_ok=%d enc_fault=%d slip=%d rx=%lu tx=%lu crc=%lu "
                "loop_hz=%lu loop_max_us=%lu P=%.2f I=%.2f Tf=%.4f COGK=%.1f vlim=%.2f\n",
                DIR, dirS, motor.zero_electric_angle, (int)field_on,
                (int)SerialBT.hasClient(), (int)C.st_watchdog, C.w_ramp, thetaProto(),
                (int)encoderOk(), (int)enc_fault, (int)C.st_slip,
                (unsigned long)rx_frames, (unsigned long)tx_frames,
                (unsigned long)crc_err_count, (unsigned long)loop_hz,
                (unsigned long)loop_max_shown,
                motor.PID_velocity.P, motor.PID_velocity.I, motor.LPF_velocity.Tf,
                cog_k, V_LIMIT);
}

static void handle(const char* s) {
  if (!strncmp(s, "STATE", 5)) {
    sayState();
  } else if (!strncmp(s, "TEST", 4)) {
    if (SerialBT.hasClient()) { Serial.println("# ОТКАЗ: телефон подключён, тест смешал бы источники"); return; }
    if (enc_fault) { Serial.println("# ОТКАЗ: датчик в отказе, нужен сброс платы"); return; }
    test_v = arg(s, "v", 0.0f);
    uint32_t t = (uint32_t)arg(s, "t", 3000.0f);
    if (t > 25000) t = 25000;                // + хвост 5 с = ёмкость записи
    test_on = true; test_until = millis() + t; test_next = 0;
    rec_on = true; rec_n = 0; rec_next_us = micros(); rec_t0_ms = millis();
    Serial.printf("# ТЕСТ v=%.3f t=%lu\n", test_v, (unsigned long)t);
  } else if (!strncmp(s, "SET", 3)) {
    motor.PID_velocity.P  = arg(s, "P", motor.PID_velocity.P);
    motor.PID_velocity.I  = arg(s, "I", motor.PID_velocity.I);
    motor.LPF_velocity.Tf = arg(s, "Tf", motor.LPF_velocity.Tf);
    cog_k                 = arg(s, "COGK", cog_k);
    motor.PID_velocity.reset();
    sayState();
  } else {
    Serial.printf("# ? %s\n", s);
  }
}

// ---------------------------- поле ----------------------------
static void setField(bool on) {
  if (on == field_on) return;
  if (on) {
    // Интегратор с прошлого включения не должен выстрелить в первый такт.
    motor.PID_velocity.reset();
    motor.enable();
  } else {
    motor.disable();
  }
  field_on = on;
}

// ============================ ЦИКЛ ============================
void setup() {
  pinMode(PIN_LED, OUTPUT);
  // Буфер вывода побольше: при штатных 128 байтах строка STATE (~300 байт)
  // блокировала цикл мотора, пока уходила по UART. Замерено: худший такт
  // 11.8 мс при опросе STATE раз в 0.2 с. Диагностика не должна трясти вал.
  Serial.setTxBufferSize(4096);
  Serial.begin(115200);
  delay(300);

  spi.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CS);
  sensor.init(&spi);
  sensor.min_elapsed_time = 0.002f;          // MET, замер 5 и 18 сентября
  motor.linkSensor(&sensor);

  driver.voltage_power_supply = V_SUPPLY;
  driver.voltage_limit = V_LIMIT * 2;
  if (!driver.init()) { Serial.println("# ОТКАЗ: драйвер"); while (1) { digitalWrite(PIN_LED, (millis() >> 6) & 1); } }
  motor.linkDriver(&driver);

  motor.voltage_limit        = V_LIMIT;
  motor.voltage_sensor_align = V_ALIGN;
  motor.controller           = MotionControlType::velocity;
  motor.torque_controller    = TorqueControlType::voltage;
  motor.velocity_limit       = 3.0f;
  motor.PID_velocity.P = 4.0f; motor.PID_velocity.I = 5.0f;
  motor.PID_velocity.D = 0.0f; motor.PID_velocity.output_ramp = 200.0f;
  // Tf=0.12, А НЕ 0.01 ИЗ foc_bench. Замер 24 сентября акселерометром
  // телефона в люльке, вал стоит, поле включено, два повтора вперемешку:
  //   Tf=0.01 -> вибрация x140 от фона; 0.04 -> x26; 0.06 -> x15; 0.12 -> x8.
  // Ход по энкодеру при этом не меняется: СКО 0.09-0.19 град во всём ряду
  // 0.04-0.12 на 0.05 / 0.11 / 0.3 рад/с. Tf=0.01 выбирался 18 сентября по
  // дрожанию угла вала — прибором, который вибрацию люльки не видит, — и в
  // кадре он съедал резкость в 5-19 раз.
  motor.LPF_velocity.Tf = 0.12f;

  motor.init();
  if (!motor.initFOC()) { Serial.println("# ОТКАЗ: initFOC"); while (1) { digitalWrite(PIN_LED, (millis() >> 6) & 1); } }
  dirS = (motor.sensor_direction == Direction::CW) ? 1 : -1;
  motor.disable();
  field_on = false;
  // Бюджет напряжения: зубцовая добавка идёт МИМО ограничителя
  // (feed_forward прибавляется после _constrain), поэтому ПИ получает остаток.
  motor.updateVoltageLimit(V_LIMIT - COG_MAX);

  rec_th = (float*)  malloc(REC_MAX * sizeof(float));
  rec_w  = (int16_t*)malloc(REC_MAX * sizeof(int16_t));
  rec_fl = (uint8_t*)malloc(REC_MAX);
  if (!rec_th || !rec_w || !rec_fl) Serial.println("# запись в память недоступна: нет кучи");

  ctl::Params cp = ctl::defaults();          // значения спецификации §4
  C.init(cp);

  SerialBT.begin(BT_NAME);
  Serial.printf("# field_link ГОТОВ: %s, DIR=%d (вывод из видео, глазами не подтверждён), dirS=%d zea=%.4f vlim=%.2f\n",
                BT_NAME, DIR, dirS, motor.zero_electric_angle, V_LIMIT);
  loop_prev_us = micros();
  loop_t0 = millis();
}

void loop() {
  uint32_t now_us = micros();
  uint32_t el_us = now_us - loop_prev_us;
  float dt = el_us * 1e-6f;
  loop_prev_us = now_us;
  if (el_us > loop_max_us) loop_max_us = el_us;
  if (dt > 0.05f) dt = 0.0f;

  // Зубцы: по АБСОЛЮТНОМУ механическому углу, каждый такт.
  if (field_on && cog_k != 0.0f) {
    float th = sensor.getMechanicalAngle();
    motor.feed_forward_voltage.q = cog_k * (COG_A44 * cosf(44.0f * th - COG_F44)
                                          + COG_A22 * cosf(22.0f * th - COG_F22));
  } else {
    motor.feed_forward_voltage.q = 0.0f;
  }
  motor.loopFOC();                           // обновляет датчик и при снятом поле

  float theta = thetaProto();
  if (millis() - med_last_ms >= MED_STEP_MS) {
    med_last_ms = millis();
    med_buf[med_i] = theta;
    med_i = (uint8_t)((med_i + 1) % MED_N);
    if (med_i == 0) med_full = true;
  }

  pump();
  testPump();
  recPump();
  C.step(millis(), dt, theta);
  freezeCheck();

  // ---- ПИТАНИЕ ФАЗ: ОДНА ТОЧКА РЕШЕНИЯ ----
  // Вкл: связь жива (кадры идут) или рампа ещё не пришла к нулю.
  // Выкл: отказ датчика; либо связи нет LINK_LOST_OFF_MS и рампа в нуле.
  bool alive = C.had_first && !C.st_watchdog;
  if (alive) lost_since_ms = millis();
  bool ramp_zero = fabsf(C.w_ramp) < 1e-4f;
  bool want_on = !enc_fault && C.had_first &&
                 (alive || !ramp_zero || (millis() - lost_since_ms) < LINK_LOST_OFF_MS);
  setField(want_on);

  motor.move((float)DIR * C.w_ramp);         // при снятом поле только обновит угол

  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') { if (line_n) { line[line_n] = 0; handle(line); line_n = 0; } }
    else if (line_n < sizeof(line) - 1) line[line_n++] = c;
  }

  loop_cnt++;
  if (millis() - loop_t0 >= 1000) {
    loop_hz = loop_cnt; loop_cnt = 0; loop_t0 = millis();
    loop_max_shown = loop_max_us; loop_max_us = 0;
  }

  // Светодиод: горит — связь жива; медленно мигает — ждём телефон;
  // часто — отказ датчика.
  digitalWrite(PIN_LED, enc_fault ? ((millis() >> 6) & 1)
                      : alive ? HIGH : ((millis() >> 9) & 1));
}
