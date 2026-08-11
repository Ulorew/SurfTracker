/*
 * B-G431B-ESC1: ПОВТОРЯЕМОСТЬ замера дрожания.
 *
 * Критерий объявлен ДО прогона (tools/stand/jerk_analyze.py): СКО остатка угла
 * в полосе 1–20 Гц за вычетом шумового пола, в градусах. Полоса снизу режет
 * дрейф и артефакты снятия тренда, сверху — широкополосный шум датчика.
 *
 * Пишутся СЫРЫЕ отсчёты угла на 200 Гц, вся обработка на ноутбуке. Так
 * критерий можно пересчитать задним числом, не повторяя прогон, — но объявлен
 * он заранее, чтобы метрика не выбиралась под понравившийся результат.
 *
 * Верхнее напряжение 3.0 В, а не 3.5: Hero предупреждал, что на верхней части
 * диапазона мотор ощутимо греется. Каждая ячейка идёт 11 секунд, на 3.0 В
 * набегает меньше минуты суммарно — но лезть выше без нужды незачем.
 *
 * СИГНАЛЫ: перед ячейкой светодиод мигает номер напряжения, во время записи
 * горит ровно, при выгрузке гаснет.
 */
#include <SimpleFOC.h>

// ПОВТОРЯЕМОСТЬ. Четыре ячейки по пять повторов каждая.
//
// Матрица напряжение x скорость дала числа, которые местами похожи на тренд,
// а местами скачут. Отличить одно от другого можно только зная разброс
// повторов той же ячейки. Если он сопоставим с разницей между напряжениями,
// матрица не значит ничего — и тогда её надо гонять с усреднением, а не
// толковать как есть.
//
// Ячейки выбраны так: две на скорости 1.0 рад/с, где виден чистый тренд по
// напряжению, и две на 1.5, где сидят выбросы. Если разброс велик именно на
// 1.5 — вопрос закрыт без новых гипотез.
static const float CELL_V[] = {1.5f, 2.5f, 2.0f, 2.5f};
static const float CELL_W[] = {1.0f, 1.0f, 1.5f, 1.5f};
static const uint8_t NCELL  = 4;
static const uint8_t REPEATS = 5;

static const uint16_t FS_HZ    = 200;
static const uint16_t N_SAMP   = 1600;          // 8 с
static const uint32_t SETTLE_MS = 2500;

static const uint8_t POLE_PAIRS = 11;
static const float   SUPPLY_V   = 12.0f;
static const unsigned long SENS_MIN_US = 3;
static const unsigned long SENS_MAX_US = 919;

MagneticSensorPWM sensor = MagneticSensorPWM(PB8, SENS_MIN_US, SENS_MAX_US);
void doPWM() { sensor.handlePWM(); }

BLDCMotor      motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver6PWM driver = BLDCDriver6PWM(A_PHASE_UH, A_PHASE_UL,
                                       A_PHASE_VH, A_PHASE_VL,
                                       A_PHASE_WH, A_PHASE_WL);

static float buf[N_SAMP];

static void blink(uint8_t n, uint16_t ms) {
  for (uint8_t i = 0; i < n; i++) {
    digitalWrite(LED_BUILTIN, HIGH); delay(ms);
    digitalWrite(LED_BUILTIN, LOW);  delay(ms);
  }
}

static void record(float w) {
  const uint32_t step_us = 1000000UL / FS_HZ;
  static float w_ramp = 0.0f;                 // сохраняется между ячейками:
                                              // между скоростями вал не
                                              // останавливается полностью, но
                                              // рампа всё равно ведёт плавно
  uint32_t prev = micros();
  uint32_t t0 = millis();
  while (millis() - t0 < SETTLE_MS || fabsf(w_ramp - w) > 1e-4f) {
    uint32_t now = micros();
    float dt = (now - prev) * 1e-6f; prev = now;
    if (dt < 0 || dt > 0.05f) dt = 0;
    float st = 1.0f * dt;
    if (w_ramp < w) w_ramp = min(w_ramp + st, w);
    else if (w_ramp > w) w_ramp = max(w_ramp - st, w);
    motor.loopFOC(); motor.move(w_ramp);
    sensor.update();
  }
  uint32_t next = micros();
  for (uint16_t i = 0; i < N_SAMP; i++) {
    while ((int32_t)(micros() - next) < 0) {
      motor.loopFOC(); motor.move(w_ramp); sensor.update();
    }
    next += step_us;
    buf[i] = sensor.getAngle();
  }
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  blink(5, 80);

  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) { }
  Serial.println();
  Serial.println(F("=== матрица дрожания: напряжение x скорость ==="));
  Serial.print(F("ячеек ")); Serial.print((int)(NCELL * REPEATS));
  Serial.print(F(", по ")); Serial.print(N_SAMP / (float)FS_HZ, 1);
  Serial.println(F(" с записи на 200 Гц"));

  sensor.init();
  sensor.enableInterrupt(doPWM);

  driver.voltage_power_supply = SUPPLY_V;
  driver.voltage_limit = SUPPLY_V;
  driver.init();
  motor.linkDriver(&driver);
  motor.controller = MotionControlType::velocity_openloop;
  motor.voltage_limit = CELL_V[0];
  motor.init();

  for (uint8_t rep = 0; rep < REPEATS; rep++) {
    // Повторы идут ВНЕШНИМ циклом, а ячейки внутренним. Так между двумя
    // повторами одной ячейки проходит весь круг, и медленный дрейф (нагрев
    // обмоток, смещение вала) размазывается по всем ячейкам поровну вместо
    // того, чтобы целиком осесть в последнем повторе.
    for (uint8_t c = 0; c < NCELL; c++) {
      motor.voltage_limit = CELL_V[c];
      digitalWrite(LED_BUILTIN, LOW); delay(250);
      blink(c + 1, 180); delay(200);
      digitalWrite(LED_BUILTIN, HIGH);
      record(CELL_W[c]);
      digitalWrite(LED_BUILTIN, LOW);

      Serial.print(F("#НАЧАЛО w=")); Serial.print(CELL_W[c], 4);
      Serial.print(F(" volts=")); Serial.print(CELL_V[c], 2);
      Serial.print(F(" rep=")); Serial.print(rep);
      Serial.print(F(" fs=")); Serial.print(FS_HZ);
      Serial.print(F(" n=")); Serial.println(N_SAMP);
      for (uint16_t i = 0; i < N_SAMP; i++) Serial.println(buf[i], 6);
      Serial.println(F("#КОНЕЦ"));
    }
  }
  motor.move(0.0f);
  motor.disable();
  Serial.println(F("#ВСЁ"));
  while (1) blink(1, 60);
}

void loop() { }
