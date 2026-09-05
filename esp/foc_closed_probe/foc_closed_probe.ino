/*
 * ESP32 + SimpleFOCMini + 4108 + AS5048A: ЗАМКНУТЫЙ контур.
 *
 * Предшественник — esp/foc_mini_probe, разомкнутый прогон. Он доказал ровно
 * то, без чего замыкать было нечего: датчик видит этот вал, 11 пар полюсов
 * верны, масштаб верен (отношение измеренной скорости к уставке 1.003 и
 * 0.998), а знак датчика ОБРАТЕН направлению поля.
 *
 * ЗНАК НЕ ЧИНИТСЯ ПАЯЛЬНИКОМ. initFOC() определяет направление датчика сам:
 * двигает вал полем и смотрит, растёт угол или убывает. Здесь этот результат
 * ПЕЧАТАЕТСЯ, чтобы его можно было прибить константой (motor.sensor_direction
 * и zero_electric_angle) и больше не крутить вал на каждом старте. Пока
 * определение оставлено включённым — прибивать имеет смысл проверенное
 * число, а не первое попавшееся.
 *
 * ЧТО МЕРЯЕТСЯ, ОБЪЯВЛЕНО ДО ПРОГОНА:
 *
 *   1. СКОРОСТЬ. Уставка держится замкнутым контуром, а не задаётся полем.
 *      Критерий — отношение средней измеренной скорости к уставке в пределах
 *      0.95..1.05 на каждом из трёх темпов. Разомкнутый контур давал ровно
 *      единицу по построению (поле само и есть скорость); здесь единица уже
 *      означает работу регулятора.
 *
 *   2. УГОЛ. Ступенька на +90°, затем на -90°. Критерий — установившаяся
 *      ошибка меньше 1° и отсутствие автоколебаний (размах в последней
 *      секунде удержания меньше 0.5°). Ошибка знака датчика, если бы она
 *      осталась, здесь дала бы уход в упор, а не промах — это самый честный
 *      тест на согласованность.
 *
 * НАСТРОЙКИ РЕГУЛЯТОРА взяты умышленно вялыми: P скорости 0.2, I 2.0. Задача
 * прогона — доказать, что контур замыкается и знаки согласованы, а не выжать
 * динамику. Разгонять есть смысл после, отдельным замером, где критерий будет
 * про перерегулирование.
 *
 * ПОТОЛОК НАПРЯЖЕНИЯ 2 В — правило владельца для долгой работы подряд
 * (tools/link/stand.py). Он же ограничивает и выравнивание в initFOC:
 * voltage_sensor_align ставится отдельно, иначе выравнивание пошло бы на
 * полном напряжении драйвера и дёрнуло вал заметно сильнее прогона.
 *
 * СИГНАЛЫ СВЕТОДИОДОМ (GPIO2):
 *   5 быстрых миганий — прошивка стартовала
 *   долгое свечение   — идёт initFOC, вал ДВИГАЕТСЯ сам, это нормально
 *   горит ровно       — этап удержания
 *   мигает 2 Гц       — этап вращения
 *   частое мигание    — прогон кончен, критерии сошлись, драйвер выключен
 *   длинные вспышки   — прогон кончен, НЕ сошлось, смотреть вывод
 *
 * Сборка:  esp/build.sh foc_closed_probe
 */

#include <SimpleFOC.h>

static const int PIN_IN1 = 17, PIN_IN2 = 16, PIN_IN3 = 4, PIN_EN = 22;
static const int PIN_SCK = 18, PIN_MISO = 19, PIN_MOSI = 21, PIN_CS = 5;
static const int PIN_LED = 2;

static const float POLE_PAIRS = 11;
static const float V_SUPPLY   = 12.0f;
static const float V_LIMIT    = 2.0f;
static const float V_ALIGN    = 1.0f;    // выравнивание тише прогона

BLDCMotor         motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver3PWM    driver = BLDCDriver3PWM(PIN_IN1, PIN_IN2, PIN_IN3, PIN_EN);
MagneticSensorSPI sensor = MagneticSensorSPI(AS5048_SPI, PIN_CS);
SPIClass spi(VSPI);

// ---------------------- план прогона ----------------------
//
// План таблицей, а не лестницей if-ов: этапы читаются целиком, и добавить
// темп — это строка, а не правка машины состояний.
struct Step { const char* name; int mode; float value; uint32_t ms; };
static const Step PLAN[] = {
  {"покой",        0,  0.00f, 2000},
  {"скорость 0.3", 1,  0.30f, 5000},
  {"скорость 0.6", 1,  0.60f, 5000},
  {"скорость -0.6",1, -0.60f, 5000},
  {"покой",        0,  0.00f, 1500},
  {"угол +90",     2,  90.0f, 4000},
  {"угол -90",     2, -90.0f, 4000},
  {"покой",        0,  0.00f, 1500},
  // Длинный ровный ход в конце — чтобы на контур можно было ПОСМОТРЕТЬ, а не
  // только прочитать про него. Он же даёт материал для оценки плавности:
  // тридцати секунд хватает и глазу, и разбору остатка угла.
  {"плавно 0.6",   1,  0.60f, 30000},
  {"покой",        0,  0.00f, 1000},
};
static const int NSTEP = sizeof(PLAN) / sizeof(PLAN[0]);

static int      step_i   = -1;
static uint32_t step_t0  = 0;
static float    step_a0  = 0.0f;        // угол на входе в этап, рад
static float    angle_ref = 0.0f;       // опора для угловых ступенек
static bool     all_ok   = true;

// ВСЁ СЧИТАЕТСЯ В СИСТЕМЕ БИБЛИОТЕКИ (motor.shaft_angle), а НЕ по сырому
// sensor.getAngle(). Причина конкретная и стоила одного прогона: initFOC
// определил sensor_direction=CCW и с тех пор сам инвертирует датчик, так что
// у контура положительная уставка означает УБЫВАЮЩИЙ сырой угол. Замер по
// сырому дал отношение -0.70, а угловая ступенька — ошибку в 471°: цель
// задавалась в одной системе отсчёта, а вёл контур в другой.
//
// Практическое следствие для стенда: «положительная уставка» теперь означает
// вращение в ту сторону, где сырой угол датчика убывает. Куда это по кадру —
// вопрос крепления камеры, и решается он прогоном, а не знаком в коде.

// Установившаяся скорость меряется на ХВОСТЕ этапа, а не на всём интервале.
// Регулятор намеренно вялый (P 0.2, I 2.0), и разгон занимает заметную долю
// пяти секунд: среднее по всему этапу занижено на треть при уставке 0.3 и на
// десятую при 0.6 — то есть выглядит как ошибка масштаба, которой нет.
static const float SETTLE_FRAC = 0.6f;   // хвост, по которому считается итог
static bool     settled   = false;
static float    settle_a  = 0.0f;
static uint32_t settle_t  = 0;
static float    hold_min  = 0.0f, hold_max = 0.0f;
static uint32_t hold_t0   = 0;

static void blink(int n, int on_ms, int off_ms) {
  for (int i = 0; i < n; i++) {
    digitalWrite(PIN_LED, HIGH); delay(on_ms);
    digitalWrite(PIN_LED, LOW);  delay(off_ms);
  }
}

// Оценка этапа считается на ЕГО интервале и печатается сразу: разбирать
// сводку в конце труднее, чем читать вердикт рядом с этапом.
static void finish_step() {
  if (step_i < 0) return;
  const Step& s = PLAN[step_i];
  float a = motor.shaft_angle;
  if (s.mode == 1) {
    if (!settled) { Serial.printf("# %-13s: хвост не набрался\n", s.name); return; }
    float dt = (millis() - settle_t) / 1000.0f;
    float v  = (a - settle_a) / dt;
    float k  = v / s.value;
    bool ok = k > 0.95f && k < 1.05f;
    all_ok &= ok;
    Serial.printf("# %-13s: установившаяся %+.3f рад/с за %.1f с хвоста, "
                  "отношение %+.3f -> %s\n",
                  s.name, v, dt, k, ok ? "СОШЛОСЬ" : "НЕ СОШЛОСЬ");
  } else if (s.mode == 2) {
    float err   = (a - (angle_ref + s.value * PI / 180.0f)) * 180.0f / PI;
    float ripple = (hold_max - hold_min) * 180.0f / PI;
    bool ok = fabsf(err) < 1.0f && ripple < 0.5f;
    all_ok &= ok;
    Serial.printf("# %-13s: ошибка удержания %+.3f°, размах за последнюю "
                  "секунду %.3f° -> %s\n",
                  s.name, err, ripple, ok ? "СОШЛОСЬ" : "НЕ СОШЛОСЬ");
  }
}

static void enter_step(int i) {
  finish_step();
  step_i = i;
  if (i >= NSTEP) return;
  step_t0 = millis();
  step_a0 = motor.shaft_angle;
  settled = false;
  hold_min = hold_max = motor.shaft_angle;
  hold_t0 = 0;
  // Опора для углового режима берётся ОДИН раз, на входе в первую ступеньку:
  // иначе вторая ступенька отсчитывалась бы от достигнутого положения, и
  // «-90» означало бы возврат, а не симметричный уход в другую сторону.
  if (PLAN[i].mode == 2 && (i == 0 || PLAN[i-1].mode != 2)) angle_ref = step_a0;
  switch (PLAN[i].mode) {
    case 0: motor.controller = MotionControlType::velocity; break;
    case 1: motor.controller = MotionControlType::velocity; break;
    case 2: motor.controller = MotionControlType::angle;    break;
  }
  Serial.printf("# этап: %s\n", PLAN[i].name);
}

void setup() {
  pinMode(PIN_LED, OUTPUT);
  Serial.begin(115200);
  delay(300);
  blink(5, 60, 60);

  spi.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CS);
  sensor.init(&spi);

  // ИНТЕРВАЛ ОЦЕНКИ СКОРОСТИ — 5 мс вместо стандартных 0.1 мс.
  //
  // Замер, который это потребовал: контур держал свою оценку скорости ровно
  // на уставке (0.597 при 0.600), а вал шёл на 0.5115 рад/с по наклону угла —
  // завышение на 16.8%, и одинаковое на всех уставках. Регулятор был не
  // виноват: он добросовестно приводил к цели величину, которая врала.
  //
  // Механизм. Скорость считается как Δугла/Ts, где Ts отмеряется от прошлого
  // ВЫЧИСЛЕНИЯ, а угол берётся из чтения, случившегося чуть раньше. При
  // min_elapsed_time = 100 мкс цикл успевает уложиться в ~120 мкс, и задержка
  // обмена по SPI (около 20 мкс на слово) систематически укорачивает Ts
  // относительно интервала, за который угол на самом деле изменился. 20 из
  // 120 — это и есть наблюдавшиеся 17%.
  //
  // 5 мс уводят ту же задержку в 0.4% и заодно сбивают шум оценки: при шаге
  // квантования 0.022° цена одного тика на интервале 0.1 мс — 3.8 рад/с, а на
  // 5 мс — 0.077 рад/с.
  sensor.min_elapsed_time = 0.005f;

  motor.linkSensor(&sensor);

  driver.voltage_power_supply = V_SUPPLY;
  driver.voltage_limit = V_LIMIT * 2;
  if (!driver.init()) { Serial.println("# ОТКАЗ: драйвер"); while (1) blink(1, 700, 300); }
  motor.linkDriver(&driver);

  motor.voltage_limit        = V_LIMIT;
  motor.voltage_sensor_align = V_ALIGN;
  motor.controller           = MotionControlType::velocity;
  motor.torque_controller    = TorqueControlType::voltage;

  // КОЭФФИЦИЕНТЫ ПОДНЯТЫ ПО ЗАМЕРУ, А НЕ НА ГЛАЗ. Прогон с P 0.2 / I 2.0 не
  // добирал уставку (отношение 0.76..0.92), и первым подозреваемым был потолок
  // напряжения. Телеметрия Uq его сняла: максимум 0.75 В из разрешённых 2.0,
  // в упоре 0% времени. Запас есть, значит не хватало именно регулятора.
  //
  // Tf поднят с 0.02 до 0.05: оценка скорости на этом валу шумит (СКО 0.4 при
  // уставке 0.3 рад/с), и с коротким фильтром рост P раскачивал бы контур,
  // а не ускорял.
  //
  // P_angle СНИЖЕН с 10 до 6: на удержании был размах 3..8°, то есть контур
  // положения качался о шум скоростного. Медленнее — значит ровнее.
  // Значения — победитель калибровки (docs/foc_tuning/README.md).
  motor.PID_velocity.P = 2.0f;
  motor.PID_velocity.I = 80.0f;
  motor.PID_velocity.D = 0.0f;
  motor.PID_velocity.output_ramp = 200.0f;
  motor.LPF_velocity.Tf = 0.05f;
  motor.P_angle.P      = 6.0f;
  motor.velocity_limit = 2.0f;

  Serial.println();
  Serial.println("# == замкнутый контур: скорость и угол ==");
  Serial.printf("# IN1=%d IN2=%d IN3=%d EN=%d, питание %.1f В, потолок %.1f В\n",
                PIN_IN1, PIN_IN2, PIN_IN3, PIN_EN, V_SUPPLY, V_LIMIT);
  Serial.println("# initFOC: вал сейчас двинется сам, это выравнивание");
  digitalWrite(PIN_LED, HIGH);

  motor.init();
  if (!motor.initFOC()) {
    Serial.println("# ОТКАЗ: initFOC не выровнялся — датчик или фазы");
    while (1) blink(1, 700, 300);
  }
  // РАДИ ЭТИХ ДВУХ ЧИСЕЛ и печатается вывод: ими закрепляется выравнивание,
  // после чего старт перестаёт двигать вал.
  Serial.printf("# initFOC: sensor_direction=%s, zero_electric_angle=%.4f\n",
                motor.sensor_direction == Direction::CW ? "CW" : "CCW",
                motor.zero_electric_angle);
  // Uq в телеметрии — не для красоты: без него «контур не добирает уставку»
  // неотличимо от «контуру не хватает напряжения». Первое чинится
  // коэффициентами, второе — питанием или антизубцовой калибровкой, и
  // перепутать их значит крутить не ту ручку.
  Serial.println("# t:мс angle:град vel:рад/с target:уставка uq:В");
  enter_step(0);
}

void loop() {
  motor.loopFOC();

  const Step& s = PLAN[step_i < NSTEP ? step_i : NSTEP - 1];
  if (step_i < NSTEP) {
    switch (s.mode) {
      case 0: motor.move(0.0f); digitalWrite(PIN_LED, HIGH); break;
      case 1: motor.move(s.value); digitalWrite(PIN_LED, (millis()/250)&1); break;
      case 2: motor.move(angle_ref + s.value * PI / 180.0f);
              digitalWrite(PIN_LED, HIGH); break;
    }
    // Опора хвоста ставится один раз, когда пройдена SETTLE_FRAC этапа.
    uint32_t прошло = millis() - step_t0;
    if (!settled && прошло > (uint32_t)(s.ms * SETTLE_FRAC)) {
      settled = true; settle_a = motor.shaft_angle; settle_t = millis();
    }
    // Размах удержания — только за последнюю секунду этапа: раньше в него
    // попал бы сам переход на новую цель, и «автоколебания» показывались бы
    // всегда.
    if (s.mode == 2 && прошло + 1000 > s.ms) {
      if (hold_t0 == 0) { hold_t0 = millis(); hold_min = hold_max = motor.shaft_angle; }
      hold_min = min(hold_min, motor.shaft_angle);
      hold_max = max(hold_max, motor.shaft_angle);
    }

    if (millis() - step_t0 > s.ms) {
      enter_step(step_i + 1);
      if (step_i >= NSTEP) {
        // Поле снимается сразу: оставленный включённым драйвер держит
        // постоянный ток в одной паре обмоток, и нагрев локальный.
        motor.disable();
        Serial.printf("# ПРОГОН КОНЧЕН: %s\n",
                      all_ok ? "все критерии сошлись, контур замкнут"
                             : "есть несошедшиеся этапы, см. выше");
      }
      return;
    }
  } else {
    blink(1, all_ok ? 80 : 600, 200);
    return;
  }

  static uint32_t next_ms = 0;
  if (millis() >= next_ms) {
    next_ms = millis() + 20;
    Serial.printf("t:%lu,angle:%.3f,vel:%.3f,target:%.3f,uq:%.3f\n",
                  (unsigned long)millis(),
                  motor.shaft_angle * 180.0f / PI,
                  motor.shaft_velocity,
                  s.value, motor.voltage.q);
  }
}
