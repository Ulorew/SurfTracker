/*
 * Настроечный скетч: управление скоростью + телеметрия для поиска рывков.
 *
 * Железо: Nucleo-F411RE + SimpleFOCShield v2.0.4 + BGM4108 + AS5048 (PWM)
 *
 * КОМАНДЫ (набирать слитно, с переводом строки):
 *
 *   R0        стоп
 *   R1        постоянная скорость   (замкнутый контур скорости)
 *   R2        качание в пределах ±A (замкнутый контур скорости)
 *   R3        постоянная скорость   (рампа по положению)
 *   R4        качание в пределах ±A (рампа по положению)
 *   R5        постоянная скорость   (разомкнутый контур, датчик не участвует)
 *
 *   V5        скорость 5 град/с  (знак задаёт направление в режимах R1/R3/R5)
 *   A45       амплитуда качания ±45 град
 *   Z         запомнить текущее положение как ноль
 *   W20       окно вычисления скорости, мс. Главная ручка против шума
 *             скорости, см. комментарий у doVelWindow.
 *
 * ВАЖНО про W и MVF. Окно и фильтр усредняют одно и то же, и запаздывания
 * у них СКЛАДЫВАЮТСЯ. Поднять W, не сняв фильтр, — верный способ получить
 * автоколебания: при W10 и Tf=0.05 в контуре ~60 мс задержки против 40 мс
 * постоянной интегрирования (P/I = 0.2/5), запас по фазе кончается и Uq
 * встаёт в упор. Порядок всегда такой: сперва MVF вниз, потом W вверх.
 *
 *   L         снять буферный лог: 2000 точек в ОЗУ с шагом 500 мкс,
 *             то есть окно 1 с. Дамп после заполнения — печать не мешает
 *             захвату и не прореживает данные, в отличие от телеметрии.
 *   L2000     то же, но шаг 2000 мкс — окно 4 с (для медленных режимов)
 *
 *   P1 / P0   телеметрия вкл / выкл
 *   F20       период телеметрии, мс
 *
 *   M...      всё, что относится к мотору. Полезное:
 *               MVP0.2   П-коэффициент регулятора скорости
 *               MVI5     И-коэффициент регулятора скорости
 *               MVD0     Д-коэффициент регулятора скорости
 *               MVR1000  ограничение скорости изменения выхода (рывок)
 *               MVF0.005 постоянная фильтра скорости (именно MVF, не MLF:
 *                        L — это группа пределов, у неё только C/U/V)
 *               MLU3     предел напряжения на мотор, В
 *               MLV20    предел скорости, рад/с
 *               MAP20    П-коэффициент регулятора угла
 *             Набрать M одну — выведет список.
 *
 *   ?         список всех команд
 *
 * ТЕЛЕМЕТРИЯ, порядок колонок:
 *   цель[°/с]  скорость_фильтр[°/с]  скорость_сырая[°/с]  угол[°]  датчик[°]
 *   напряжение[В]  импульс[мкс]  частота_цикла[Гц]
 *
 * ЛОГ, порядок колонок:
 *   время[мкс]  импульс[мкс]  цель[°/с]  угол[°]  датчик[°]
 *   скорость_фильтр[°/с]  напряжение[В]
 *
 * Колонки «угол» и «датчик» различаются только в R5: там «угол» — расчётная
 * уставка, а «датчик» — что получилось на самом деле. Их расхождение и есть
 * срыв. В остальных режимах это одна и та же величина с точностью до нуля.
 *
 * Колонка «импульс» — сырая длина PWM-импульса датчика в целых микросекундах.
 * Весь оборот укладывается в диапазон min..max из конструктора, то есть
 * 920-7+1 = 914 ступенек на оборот, 0.394° на ступеньку. На 5 °/с ступенька
 * меняется раз в ~79 мс, и ровно с этим периодом регулятор скорости получает
 * одиночный выброс вместо ровного сигнала. В логе это видно напрямую.
 *
 * Скорость порта 500000 — при 115200 печать начинает мешать циклу управления.
 *
 *
 * ГДЕ ПОТОЛОК ЭТОГО ДАТЧИКА (разбор от 2026-08-08, чтобы не считать заново)
 *
 * Длину импульса меряют целыми микросекундами, весь оборот укладывается
 * в 914 таких шагов — то есть 0.39° на отсчёт. Плюс дрожание задержки
 * обработчика прерывания даёт ещё ±1 отсчёт. Скорость получается делением
 * приращения угла на окно, поэтому шум скорости равен 0.39°/W.
 *
 * Отсюда жёсткое неравенство. Чтобы шум сравнялся с полезными 5 °/с,
 * нужно окно W > 0.39/5 = 78 мс. Измеренный предел устойчивости контура —
 * около 30 мс (на 40 мс автоколебания). Разрыв в 2.6 раза настройкой
 * не закрывается: на рабочем W20 остаётся ±19.5 °/с шума против 5 °/с
 * сигнала. Остаточные рывки на малой скорости — отсюда, а не из ПИД.
 *
 * Проверки, которые это подтвердили:
 *   - масштаб датчика верен: в разомкнутом режиме (R5) shaft_angle это
 *     расчётная уставка, а не датчик, и сравнение с pulse_us в той же
 *     строке дало cpr ~924 против настроенных 914 — расхождение 1%;
 *   - там же приращения pulse_us при заведомо равномерном вращении гуляли
 *     4..8 при среднем 5.6, то есть ±1..2 отсчёта чистого шума;
 *   - предсказанный шум ±39 °/с при W1 совпал с наблюдаемым разбросом
 *     vel_filt (21..99 при уставке 50);
 *   - предел по интегратору сошёлся с задержкой: при W20 устойчиво
 *     I=5 (P/I = 40 мс), при I=10 (20 мс) автоколебания.
 *
 * Напряжение фаз поднимать бессмысленно: в работе Uq = 0.2..0.6 В из
 * доступных 3 В, запас по моменту 15%. Нагрев шёл от экспериментов
 * с колебаниями, где Uq упирался в предел. Разумнее MLU1.5.
 *
 * ИТОГ: выбран разомкнутый режим R5 — датчик не участвует в управлении,
 * поэтому весь описанный выше шум просто выпадает из контура, и вращение
 * ровное. Точности достаточно. Плата за это:
 *   - на мотор постоянно подаётся voltage_limit независимо от нагрузки,
 *     отсюда непрерывный нагрев (~3.6 Вт при 3 В и 2.5 Ом). Подбирать MLU
 *     вниз до срыва, затем удвоить: нагрев падает как квадрат напряжения;
 *   - срыв происходит молча, расчётный угол расходится с настоящим
 *     навсегда. Датчик для надзора за этим годится с запасом — ±0.4°
 *     шума не мешают заметить расхождение в градусы. Колонка sens_deg.
 *
 * Замкнутый R1 при W20 тоже работоспособен (на 30 мс успокаивается заметно
 * дольше, на 40 мс автоколебания; пара P=0.2 / I=5 предельная), но ровности
 * разомкнутого не даёт — мешает шум датчика.
 *
 * Режим R3 даёт медленные накаты: в контуре положения два интегратора
 * подряд (вал и И-звено скорости), а демпфировать их нечем, Д-звена в
 * контуре положения нет. Лечится убавлением MVI — подача вперёд сама
 * держит рабочее напряжение, интегратор нужен куда меньше, чем раньше.
 *
 * ЧТО ДЕЛАТЬ ДАЛЬШЕ, если понадобится ровнее
 *
 * 1. Аппаратный захват таймером — 4096 отсчётов (столько в PWM у AS5048A
 *    на самом деле), требуемое окно падает до 18 мс. Пайки не надо, но
 *    сигнальный провод придётся переставить с D3 на D10: D3 это PB3 =
 *    TIM2_CH2, а D6 (фаза мотора) это PB10 = TIM2_CH3 — один таймер,
 *    и режим захвата сбросом счётчика убил бы фазу. D10 = PB6 = TIM4_CH1,
 *    TIM4 свободен (мотор занимает TIM3 CH1/CH2 и TIM2 CH3).
 *    Ядро (HardwareTimer.cpp) даёт TIMER_INPUT_FREQ_DUTY_MEASUREMENT, но
 *    сброс по фронту не включает — период считать разностями в software,
 *    с обработкой переполнения 16-битного TIM4.
 *
 * 2. SPI — 16384 отсчёта, требуемое окно 4.4 мс, то есть настоящий запас,
 *    и чтение на частоте цикла вместо 1 кГц. Нужны четыре провода:
 *    D13/D12/D11 + CS. В библиотеке уже есть готовый профиль AS5048_SPI.
 *    Оговорка: D13 на Nucleo-64 сидит на светодиоде LD2.
 */

#include <SimpleFOC.h>

// ---------------- железо ----------------

MagneticSensorPWM sensor = MagneticSensorPWM(3, 7, 920);
void doPWM() { sensor.handlePWM(); }

BLDCMotor       motor  = BLDCMotor(11);
BLDCDriver3PWM  driver = BLDCDriver3PWM(9, 5, 6, 8);

// ---------------- состояние ----------------

const int MODE_STOP        = 0;
const int MODE_CONST       = 1;
const int MODE_SWEEP       = 2;
const int MODE_ANGLE_CONST = 3;
const int MODE_ANGLE_SWEEP = 4;
const int MODE_OPENLOOP    = 5;

int   mode      = MODE_STOP;
float speed_deg = 5.0f;    // град/с
float amp_deg   = 45.0f;   // ± град
float zero_rad  = 0.0f;    // точка отсчёта угла
// отдельный ноль для датчика: в R5 motor.shaft_angle это расчётная уставка,
// а не измерение, поэтому показание датчика надо брать и отсчитывать своё.
// sensor.getAngle() состояние не двигает, в отличие от getVelocity()
float sens_zero_rad = 0.0f;

// в R5 velocityOpenloop() держит shaft_angle нормализованным в 0..2PI, то есть
// расчётный угол заворачивается каждый оборот, тогда как sensor.getAngle()
// обороты накапливает. Разворачиваем расчётный, иначе сравнить их нельзя.
// В остальных режимах shaft_angle и так сквозной и разворот ничего не меняет
float cmd_prev_deg      = 0.0f;
float cmd_unwrapped_deg = 0.0f;
bool  cmd_unwrap_init   = false;

void resetUnwrap() { cmd_unwrap_init = false; cmd_unwrapped_deg = 0.0f; }
int   sweep_dir = 1;

float target_rad = 0.0f;   // уставка положения для режимов с рампой

bool     tele_on     = true;
uint32_t tele_period = 500;  // мс
uint32_t tele_last   = 0;
float    tele_prev_angle = 0.0f;

uint32_t loop_prev_us  = 0;
uint32_t loop_count    = 0;  // итераций с прошлой печати телеметрии

const float RAD2DEG = 180.0f / PI;
const float DEG2RAD = PI / 180.0f;

// ---------------- буферный лог ----------------
//
// Обычная телеметрия печатает изнутри цикла управления и тем самым и
// прореживает данные, и слегка возмущает сам цикл. Рывок с периодом
// в десятки миллисекунд через такую выборку виден как алиас — картинка
// «привязывается» к периоду печати. Поэтому захват идёт в ОЗУ на полной
// частоте цикла, а печать — уже после.

#define LOG_N 2000

struct LogRec {
  uint32_t t_us;
  uint16_t pw_us;
  float    target_dps;
  float    angle_deg;
  float    sens_deg;
  float    vel_dps;
  float    uq;
};

LogRec logbuf[LOG_N];
int      log_i         = -1;    // -1 — захват не идёт
uint32_t log_period_us = 500;   // шаг захвата, мкс
uint32_t log_last_us   = 0;

// ---------------- команды ----------------

Commander command = Commander(Serial);

void doMotor(char* cmd) { command.motor(&motor, cmd); }
void doSpeed(char* cmd) { command.scalar(&speed_deg, cmd); }
void doAmp  (char* cmd) { command.scalar(&amp_deg,   cmd); }

// вход в режим: переключить тип управления и обнулить регуляторы,
// иначе интегратор скорости переносит накопленное в новый режим
void applyMode() {
  switch (mode) {
    case MODE_ANGLE_CONST:
    case MODE_ANGLE_SWEEP:
      motor.controller = MotionControlType::angle;
      target_rad = motor.shaft_angle;
      break;
    case MODE_OPENLOOP:
      motor.controller = MotionControlType::velocity_openloop;
      break;
    default:
      motor.controller = MotionControlType::velocity;
      break;
  }
  motor.PID_velocity.reset();
  motor.P_angle.reset();
  motor.feed_forward_velocity = 0.0f;  // поле не сбрасывается само
  resetUnwrap();                       // R5 нормализует угол, остальные нет
  sweep_dir = 1;
}

void doMode(char* cmd) {
  float m = mode;
  command.scalar(&m, cmd);
  mode = (int)m;
  applyMode();
}

void doTele(char* cmd) {
  float t = tele_on ? 1.0f : 0.0f;
  command.scalar(&t, cmd);
  tele_on = (t > 0.5f);
}

void doTelePeriod(char* cmd) {
  float p = tele_period;
  command.scalar(&p, cmd);
  if (p < 5) p = 5;          // чаще 5 мс печать начинает мешать циклу
  tele_period = (uint32_t)p;
}

// окно вычисления скорости, мс.
//
// Скорость получается делением приращения угла на время. Датчик по PWM даёт
// ±1 отсчёт (±0.39°) дрожания положения, и на окне 1 мс это превращается
// в ±394 °/с шума — больше самой уставки на малых скоростях. Окно 20 мс
// делит шум на 20, добавляя 20 мс запаздывания. Обычно это выгодный обмен:
// фильтр LPF_velocity глушит слабее при большей задержке.
void doVelWindow(char* cmd) {
  float w = sensor.min_elapsed_time * 1000.0f;
  command.scalar(&w, cmd);
  if (w < 0.1f) w = 0.1f;
  sensor.min_elapsed_time = w * 0.001f;
}

void doZero(char* cmd) {
  zero_rad = motor.shaft_angle;
  sens_zero_rad = sensor.getAngle();
  resetUnwrap();
  tele_prev_angle = 0.0f;
  Serial.println(F("# ноль установлен"));
}

void doLog(char* cmd) {
  float p = log_period_us;
  command.scalar(&p, cmd);
  if (p < 20) p = 20;
  log_period_us = (uint32_t)p;
  log_last_us   = micros();
  log_i         = 0;
  Serial.print(F("# захват "));
  Serial.print(LOG_N);
  Serial.print(F(" точек, шаг "));
  Serial.print(log_period_us);
  Serial.print(F(" мкс, окно "));
  Serial.print(LOG_N * log_period_us / 1000);
  Serial.println(F(" мс"));
}

void dumpLog() {
  Serial.println(F("# --- лог ---"));
  Serial.println(F("# t_us  pulse_us  target_dps  angle_deg  sens_deg  vel_dps  Uq"));
  for (int i = 0; i < LOG_N; i++) {
    LogRec &r = logbuf[i];
    Serial.print(r.t_us - logbuf[0].t_us);  Serial.print('\t');
    Serial.print(r.pw_us);                  Serial.print('\t');
    Serial.print(r.target_dps, 2);          Serial.print('\t');
    Serial.print(r.angle_deg, 3);           Serial.print('\t');
    Serial.print(r.sens_deg, 3);            Serial.print('\t');
    Serial.print(r.vel_dps, 2);             Serial.print('\t');
    Serial.println(r.uq, 3);
  }
  Serial.println(F("# --- конец лога ---"));
  log_i = -1;
}

// ---------------- setup ----------------

void setup() {
  Serial.begin(500000);
  SimpleFOCDebug::enable(&Serial);

  sensor.init();
  sensor.enableInterrupt(doPWM);
  motor.linkSensor(&sensor);

  driver.voltage_power_supply = 12;
  driver.init();
  motor.linkDriver(&driver);

  motor.controller = MotionControlType::velocity;

  // стартовые значения — заведомо мягкие, дальше крутить командами
  motor.PID_velocity.P = 0.2f;
  motor.PID_velocity.I = 5.0f;
  motor.PID_velocity.D = 0.0f;
  motor.PID_velocity.output_ramp = 1000.0f;

  // Шум скорости давится окном (sensor.min_elapsed_time), а не фильтром:
  // окно делит шум на своё время, фильтр же при том же запаздывании глушит
  // слабее. Прежняя пара Tf=0.05 + окно 1 мс давала ±39 °/с шума при
  // уставке 5 — регулятор управлял шумом. Фильтр оставлен только чтобы
  // сгладить ступеньки, которыми окно выдаёт результат.
  //
  // W20 — подобрано на железе для замкнутых режимов: на 30 мс контур
  // успокаивается заметно дольше, на 40 мс уходит в автоколебания. Пара
  // P=0.2 / I=5 к этому окну предельная: I=10 даёт постоянную 20 мс
  // против ~20 мс задержки и срывается. На R5 не влияет никак.
  motor.LPF_velocity.Tf   = 0.005f;
  sensor.min_elapsed_time = 0.020f;   // 20 мс

  motor.P_angle.P = 20.0f;

  motor.voltage_limit   = 1.0f;
  motor.velocity_limit  = 20.0f;

  motor.init();
  motor.initFOC();

  command.add('M', doMotor,       "motor");
  command.add('R', doMode,        "mode 0stop 1const 2sweep 3angle 4angle-sweep 5openloop");
  command.add('V', doSpeed,       "speed deg/s");
  command.add('A', doAmp,         "sweep amplitude deg");
  command.add('Z', doZero,        "set zero");
  command.add('W', doVelWindow,   "velocity window ms");
  command.add('L', doLog,         "burst log, arg = decimation");
  command.add('P', doTele,        "telemetry on/off");
  command.add('F', doTelePeriod,  "telemetry period ms");

  zero_rad      = motor.shaft_angle;
  target_rad    = motor.shaft_angle;
  sens_zero_rad = sensor.getAngle();

  Serial.println(F("# готов. R1 - скорость, R3 - рампа по положению, R5 - разомкнутый, R0 - стоп"));
  Serial.println(F("# target_dps  vel_filt_dps  vel_raw_dps  angle_deg  sens_deg  Uq  pulse_us  loop_hz"));
  _delay(500);
  loop_prev_us = micros();
}

// ---------------- loop ----------------

void loop() {
  uint32_t now_us = micros();
  float dt = (now_us - loop_prev_us) * 1e-6f;
  loop_prev_us = now_us;
  if (dt < 0.0f || dt > 0.05f) dt = 0.0f;   // первый проход и переполнение
  loop_count++;

  motor.loopFOC();

  float raw_deg  = (motor.shaft_angle - zero_rad) * RAD2DEG;
  float sens_deg = (sensor.getAngle() - sens_zero_rad) * RAD2DEG;

  if (!cmd_unwrap_init) {
    cmd_prev_deg      = raw_deg;
    cmd_unwrapped_deg = raw_deg;
    cmd_unwrap_init   = true;
  }
  float step = raw_deg - cmd_prev_deg;
  cmd_prev_deg = raw_deg;
  if (step >  180.0f) step -= 360.0f;
  if (step < -180.0f) step += 360.0f;
  cmd_unwrapped_deg += step;

  float angle_deg = cmd_unwrapped_deg;

  float target_deg = 0.0f;   // для телеметрии — во всех режимах в °/с

  switch (mode) {

    case MODE_CONST:
      target_deg = speed_deg;
      motor.move(target_deg * DEG2RAD);
      break;

    case MODE_SWEEP:
      if (sweep_dir > 0 && angle_deg >=  amp_deg) sweep_dir = -1;
      if (sweep_dir < 0 && angle_deg <= -amp_deg) sweep_dir =  1;
      target_deg = fabsf(speed_deg) * sweep_dir;
      motor.move(target_deg * DEG2RAD);
      break;

    case MODE_ANGLE_CONST:
      target_deg  = speed_deg;
      target_rad += target_deg * DEG2RAD * dt;
      // скорость подаётся вперёд напрямую: контур положения тогда не обязан
      // добывать её из измерений и лишь подчищает остаточную ошибку.
      // Без этого весь ход держится на интеграторе, который питается
      // зашумлённой оценкой скорости — то есть на самом слабом звене.
      motor.feed_forward_velocity = target_deg * DEG2RAD;
      motor.move(target_rad);
      break;

    case MODE_ANGLE_SWEEP: {
      // разворот по уставке, а не по измеренному углу: измеренный
      // квантован на 0.394° и на границе дал бы дребезг направления
      float target_from_zero = (target_rad - zero_rad) * RAD2DEG;
      if (sweep_dir > 0 && target_from_zero >=  amp_deg) sweep_dir = -1;
      if (sweep_dir < 0 && target_from_zero <= -amp_deg) sweep_dir =  1;
      target_deg  = fabsf(speed_deg) * sweep_dir;
      target_rad += target_deg * DEG2RAD * dt;
      motor.feed_forward_velocity = target_deg * DEG2RAD;
      motor.move(target_rad);
      break;
    }

    case MODE_OPENLOOP:
      target_deg = speed_deg;
      motor.move(target_deg * DEG2RAD);
      break;

    default:  // MODE_STOP
      motor.move(0.0f);
      break;
  }

  // ---- буферный лог ----
  if (log_i >= 0) {
    if ((uint32_t)(now_us - log_last_us) >= log_period_us) {
      log_last_us += log_period_us;
      LogRec &r = logbuf[log_i];
      r.t_us       = now_us;
      r.pw_us      = (uint16_t)sensor.pulse_length_us;
      r.target_dps = target_deg;
      r.angle_deg  = angle_deg;
      r.sens_deg   = sens_deg;
      r.vel_dps    = motor.shaft_velocity * RAD2DEG;
      r.uq         = motor.voltage.q;
      log_i++;
      if (log_i >= LOG_N) dumpLog();
    }
  }

  // ---- телеметрия ----
  // во время захвата молчим, чтобы печать не влезала в цикл
  if (tele_on && log_i < 0) {
    uint32_t now = millis();
    if (now - tele_last >= tele_period) {
      float tdt = (now - tele_last) * 0.001f;
      tele_last = now;

      // сырая скорость считается здесь, а не через sensor.getVelocity():
      // тот вызов сдвигает внутреннее состояние датчика и испортил бы
      // оценку скорости, которой пользуется регулятор
      float vel_raw_deg = 0.0f;
      if (tdt > 0.0f && tdt < 2.0f) {
        vel_raw_deg = (angle_deg - tele_prev_angle) / tdt;
      }
      tele_prev_angle = angle_deg;

      float loop_hz = (tdt > 0.0f) ? (loop_count / tdt) : 0.0f;
      loop_count = 0;

      Serial.print(target_deg, 2);                       Serial.print('\t');
      Serial.print(motor.shaft_velocity * RAD2DEG, 2);   Serial.print('\t');
      Serial.print(vel_raw_deg, 2);                      Serial.print('\t');
      Serial.print(angle_deg, 2);                        Serial.print('\t');
      Serial.print(sens_deg, 2);                         Serial.print('\t');
      Serial.print(motor.voltage.q, 3);                  Serial.print('\t');
      Serial.print(sensor.pulse_length_us);              Serial.print('\t');
      Serial.println(loop_hz, 0);
    }
  }

  command.run();
}
