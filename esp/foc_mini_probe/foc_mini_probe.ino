/*
 * ESP32 + SimpleFOCMini + 4108: первый запуск вала, РАЗОМКНУТЫЙ контур.
 *
 * ПОЧЕМУ РАЗОМКНУТЫЙ. Контур замыкать нечем, пока не доказано, что датчик
 * видит именно этот вал и в согласованную сторону. В разомкнутом режиме
 * мотор — ЭТАЛОН: команда задаёт скорость поля, и измеренная энкодером
 * скорость обязана с ней совпасть. Разойдутся — виноват датчик или число пар
 * полюсов, и это видно сразу, без осциллографа. Замкни мы контур сразу,
 * ошибка знака дала бы разгон в упор, и отличить её от плохой настройки было
 * бы нечем.
 *
 * ДАТЧИК ЧИТАЕТСЯ, НО В КОНТУР НЕ ВХОДИТ. Он здесь измерительный прибор, а не
 * обратная связь: motor.controller = velocity_openloop.
 *
 * РАСПИНОВКА ПОД 38-ПИНОВУЮ DOIT DevKit, правый ряд. Выводы выбраны по
 * МЕСТУ НА ГРЕБЁНКЕ, а не по номерам: правый ряд идёт сверху вниз
 *
 *   GND, 23, 22, TX0, RX0, 21, GND, 19, 18, 5, 17, 16, 4, 0, 2, 15, D1, D0, CLK
 *            ↑EN            └─── энкодер ───┘  └IN1 IN2 IN3┘
 *
 * и три ШИМ ложатся СПЛОШНОЙ ТРОЙКОЙ сразу под CS энкодера. EN уходит на 22:
 * это медленный сигнал, длина провода ему безразлична.
 *
 *   IN1 -> GPIO17 | IN2 -> GPIO16 | IN3 -> GPIO4 | EN -> GPIO22
 *
 * 16 И 17 СВОБОДНЫ — ЭТО ПРОВЕРЕНО, А НЕ ПРЕДПОЛОЖЕНО. У модулей WROVER они
 * заняты внешней PSRAM, и в шапке bt_link.ino это записано как неразличимое
 * «по чипу ESP32-D0WD-V3 отличить WROOM от WROVER нельзя». Различить можно:
 * PSRAM отвечает сама, если включить её в сборке. Замер (esp/psram_check,
 * 4 сентября 2026) дал 0 байт — модуль WROOM, выводы наши.
 *
 * ЧЕГО НА ЭТОМ РЯДУ БРАТЬ НЕЛЬЗЯ:
 *   GPIO0     — strapping загрузчика. У DRV8313 на входах подтяжки ВНИЗ, они
 *               удержат его низким на старте, и плата уйдёт в загрузчик
 *               вместо скетча. Отказ выглядит как «прошивка не работает».
 *   GPIO6-11  — флеш. Выведены на гребёнку, но убивают загрузку намертво.
 *   GPIO2     — светодиод платы, им отбиваются этапы прогона.
 *   GPIO15    — strapping MTDO: притянутый вниз, гасит лог загрузки в порту.
 *               Плата загрузится, но диагностики лишимся.
 *
 * 25/26 БОЛЬШЕ НЕ РЕЗЕРВИРУЮТСЯ. Они держались под UART к STM32, но контур
 * замыкается здесь же, на ESP32, а телефон говорит с ним по Bluetooth
 * напрямую — промежуточной плате в этой схеме места нет. Если она вернётся,
 * 25, 26, 27, 14 дают сплошную гребёнку на четыре на ЛЕВОМ ряду.
 *
 * ПРЕДОХРАНИТЕЛИ. Потолок напряжения 2 В — правило владельца для долгой
 * работы подряд, перенесено из tools/link/stand.py. Питание 12 В стоит в
 * driver.voltage_power_supply не для красоты: из него библиотека считает
 * скважность, и заниженное значение молча дало бы больше напряжения на
 * обмотках, чем просили.
 *
 * ПРОГОН ИДЁТ САМ И КОНЧАЕТСЯ САМ. Человек в замере не участвует: этапы
 * отбиваются светодиодом и движением вала, в конце драйвер гаснет. Это то же
 * правило, по которому сделан stm/sensor_check_g431 — прогон, размеченный
 * временем и просьбами «покрутите сейчас», со стенда не виден.
 *
 * СИГНАЛЫ СВЕТОДИОДОМ (GPIO2):
 *   5 быстрых миганий — прошивка стартовала
 *   горит ровно       — этап «покой», вал стоять ДОЛЖЕН
 *   мигает 2 Гц       — этап «вперёд»
 *   мигает 0.5 Гц     — этап «назад»
 *   частое мигание    — прогон кончен, всё сошлось, драйвер выключен
 *   длинные вспышки   — прогон кончен, НЕ сошлось, смотреть вывод
 *
 * Сборка:  esp/build.sh foc_mini_probe
 * Заливка: esp/build.sh foc_mini_probe --прошить
 */

#include <SimpleFOC.h>

// ---------------------- распиновка ----------------------
static const int PIN_IN1 = 17, PIN_IN2 = 16, PIN_IN3 = 4, PIN_EN = 22;
// РАСПИНОВКА СМЕНЕНА 18 сентября: SPI переехал на левый ряд, правый закрыт
// платой SimpleFOCMini. CLK 14, MISO 27, MOSI 26, CS 25 — все обычные пины.
static const int PIN_SCK = 14, PIN_MISO = 27, PIN_MOSI = 26, PIN_CS = 25;
static const int PIN_LED = 2;

// ---------------------- параметры (в лог) ----------------------
// ЧИСЛО ПАР ПОЛЮСОВ ЗДЕСЬ — ГИПОТЕЗА, А НЕ ФАКТ. 11 досталось от мотора 4108;
// на iFlight 3506 оно почти наверняка другое. Этот пробник как раз и служит
// для замера: в разомкнутом контуре отношение ИЗМЕРЕННОЙ скорости к уставке
// равно (заданные пары / истинные пары). То есть ratio > 1 означает, что
// истинных пар МЕНЬШЕ заданных, и истинное число = 11 / ratio.
static const float POLE_PAIRS   = 11;      // ГИПОТЕЗА от 4108, проверяется прогоном
static const float V_SUPPLY     = 12.0f;   // питание драйвера
static const float V_LIMIT      = 2.0f;    // правило владельца для долгой работы
static const float SPIN_RAD_S   = 0.6f;    // уставка этапов вращения
static const uint32_t T_REST_MS = 3000, T_SPIN_MS = 6000;

BLDCMotor       motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver3PWM  driver = BLDCDriver3PWM(PIN_IN1, PIN_IN2, PIN_IN3, PIN_EN);

// Датчик берётся ТЕМ ЖЕ описанием, что проверено пробником as5048a_probe:
// SPI mode 1, 14 бит, регистр угла 0x3FFF. AS5048_SPI — готовый профиль
// SimpleFOC, но частота у него 1 МГц, и это ровно та, на которой пробник дал
// ноль ошибок чётности на дюпонах. Разгонять — отдельным замером.
MagneticSensorSPI sensor = MagneticSensorSPI(AS5048_SPI, PIN_CS);
SPIClass spi(VSPI);

// ---------------------- этапы ----------------------
enum Stage { REST_1, FWD, REST_2, REV, REST_3, DONE };
static Stage    stage      = REST_1;
static uint32_t stage_t0   = 0;
static float    target     = 0.0f;

// Замер скорости по датчику: угол разворачивается по кратчайшей дуге, иначе
// переход 359->0 читается как скачок на целый оборот и портит среднее.
static float    unwrapped  = 0.0f, last_raw = 0.0f;
static float    stage_a0   = 0.0f;
static float    v_fwd = 0.0f, v_rev = 0.0f;

// Скорость для телеметрии считается ЗДЕСЬ, по датчику, а не берётся из
// motor.shaft_velocity: в разомкнутом контуре библиотека его не вычисляет и
// он остаётся нулём при живом вращении. Сухой прогон это и показал — вал
// стоял, но и при движении поле показывало бы ноль, а это худший вид
// неверного вывода: правдоподобный.
//
// Окно 100 мс, а не соседние отсчёты: при шаге квантования 0.022° и периоде
// 20 мс одиночная разность даёт ступеньки в 1.1 рад/с на ровном ходу.
static float    v_meas = 0.0f;
static float    vref_a = 0.0f;
static uint32_t vref_t = 0;

static void blink(int n, int on_ms, int off_ms) {
  for (int i = 0; i < n; i++) {
    digitalWrite(PIN_LED, HIGH); delay(on_ms);
    digitalWrite(PIN_LED, LOW);  delay(off_ms);
  }
}

static void enter(Stage s, float t) {
  // Итог предыдущего этапа считается ЗДЕСЬ, на его собственном интервале, а
  // не общим средним по прогону: разгон и торможение принадлежат этапу.
  float dt = (millis() - stage_t0) / 1000.0f;
  if (stage == FWD && dt > 0) v_fwd = (unwrapped - stage_a0) / dt;
  if (stage == REV && dt > 0) v_rev = (unwrapped - stage_a0) / dt;
  stage = s; target = t; stage_t0 = millis(); stage_a0 = unwrapped;
  Serial.printf("# этап %d, уставка %.2f рад/с\n", (int)s, t);
}

void setup() {
  pinMode(PIN_LED, OUTPUT);
  Serial.begin(115200);
  delay(300);
  blink(5, 60, 60);

  spi.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CS);   // ЯВНО: иначе MOSI уйдёт на 23
  sensor.init(&spi);

  driver.voltage_power_supply = V_SUPPLY;
  driver.voltage_limit = V_LIMIT * 2;                // потолок драйвера выше моторного: ограничивает мотор
  if (!driver.init()) {
    Serial.println("# ОТКАЗ: драйвер не инициализировался");
    while (1) blink(1, 700, 300);
  }
  motor.linkDriver(&driver);
  motor.linkSensor(&sensor);                         // только для чтения, контур разомкнут

  motor.controller   = MotionControlType::velocity_openloop;
  motor.voltage_limit = V_LIMIT;
  motor.velocity_limit = 5.0f;
  motor.init();

  Serial.println();
  Serial.println("# == SimpleFOCMini + 4108, разомкнутый контур ==");
  Serial.printf("# IN1=%d IN2=%d IN3=%d EN=%d, питание %.1f В, потолок %.1f В, пар полюсов %.0f\n",
                PIN_IN1, PIN_IN2, PIN_IN3, PIN_EN, V_SUPPLY, V_LIMIT, POLE_PAIRS);
  Serial.println("# t:мс angle:град vel:рад/с_измер target:рад/с");

  sensor.update();
  last_raw = sensor.getAngle();
  enter(REST_1, 0.0f);
}

void loop() {
  sensor.update();
  float raw = sensor.getAngle();
  float d = raw - last_raw;
  while (d >  PI) d -= 2 * PI;                       // кратчайшая дуга
  while (d < -PI) d += 2 * PI;
  unwrapped += d; last_raw = raw;

  // loopFOC() вызывается и в разомкнутом контуре — так сделано в phone_link.ino
  // на этом же железе и этой же версии 2.4.0. Лишним он тут не будет в любом
  // случае, а разбираться в поведении библиотеки на живом моторе дороже.
  motor.loopFOC();
  motor.move(target);

  uint32_t now = millis();
  if (now - vref_t >= 100) {
    v_meas = (unwrapped - vref_a) * 1000.0f / (now - vref_t);
    vref_a = unwrapped; vref_t = now;
  }

  uint32_t dt = millis() - stage_t0;
  switch (stage) {
    case REST_1: digitalWrite(PIN_LED, HIGH);
                 if (dt > T_REST_MS) enter(FWD,  SPIN_RAD_S);  break;
    case FWD:    digitalWrite(PIN_LED, (millis() / 250) & 1);
                 if (dt > T_SPIN_MS) enter(REST_2, 0.0f);      break;
    case REST_2: digitalWrite(PIN_LED, HIGH);
                 if (dt > T_REST_MS) enter(REV, -SPIN_RAD_S);  break;
    case REV:    digitalWrite(PIN_LED, (millis() / 1000) & 1);
                 if (dt > T_SPIN_MS) enter(REST_3, 0.0f);      break;
    case REST_3: digitalWrite(PIN_LED, HIGH);
                 if (dt > T_REST_MS) {
                   enter(DONE, 0.0f);
                   // Поле снимается СРАЗУ по окончании. Оставленный включённым
                   // драйвер держит постоянный ток в одной паре обмоток —
                   // нагрев локальный, и это хуже вращения (tools/link/stand.py).
                   motor.disable();
                   float k_fwd = v_fwd / SPIN_RAD_S, k_rev = v_rev / (-SPIN_RAD_S);
                   Serial.printf("# ИТОГ: вперёд %.3f рад/с (отношение %.3f), "
                                 "назад %.3f рад/с (отношение %.3f)\n",
                                 v_fwd, k_fwd, v_rev, k_rev);
                   bool ok = k_fwd > 0.8f && k_fwd < 1.25f && k_rev > 0.8f && k_rev < 1.25f;
                   if (ok) {
                     Serial.println("# СОШЛОСЬ: датчик видит вал, знак согласован, "
                                    "масштаб верный -> можно замыкать контур");
                   } else {
                     Serial.println("# НЕ СОШЛОСЬ. Отношение далеко от 1. Разбор:");
                     Serial.println("#   отношение РОВНО ноль  -> вал не двигался вовсе: первым делом");
                     Serial.println("#                            проверить, подано ли питание на драйвер");
                     Serial.println("#   отношение около нуля  -> датчик не видит ЭТОТ вал (магнит/зазор)");
                     Serial.println("#   отношение отрицательно -> знак датчика обратен полю: поменять");
                     Serial.println("#                            две фазы местами ИЛИ sensor_direction");
                     Serial.println("#   отношение кратно целому -> неверное число пар полюсов");
                     Serial.println("#   вал дёргается/стоит     -> мало напряжения или перепутаны фазы");
                   }
                 }
                 break;
    case DONE:   blink(1, (v_fwd != 0 && v_rev != 0) ? 80 : 600, 200); return;
  }

  // Телеметрия в том же формате, что у пробника энкодера: её читает
  // tools/stand/enc_plot.py без единой правки.
  static uint32_t next_ms = 0;
  if (millis() >= next_ms) {
    next_ms = millis() + 20;                         // 50 Гц, как у пробника
    Serial.printf("t:%lu,angle:%.3f,vel:%.3f,target:%.3f\n",
                  (unsigned long)millis(), raw * 180.0f / PI,
                  v_meas, target);
  }
}
