/*
 * B-G431B-ESC1: развёртка скорости для ГЛАЗА.
 *
 * Вопрос, который она решает: с какой скорости тряска становится заметной, и
 * попадают ли туда боевые режимы. Реальное слежение идёт при |w| < 0.1 рад/с
 * в 86% времени (по логам live1/live3), а тряску Hero видел на 0.3.
 *
 * Прибор здесь — глаз, и он для вопроса «видно ли» законнее любого спектра.
 * Задача прошивки — только дать плавную развёртку и разметить её так, чтобы
 * наблюдатель мог назвать МОМЕНТ, а не гадать о скорости.
 *
 * РАЗМЕТКА СВЕТОДИОДОМ: каждые 15 секунд короткая вспышка = новая отметка.
 * Отметок девять, скорость на отметке k равна 0.05 + 0.05*k рад/с.
 * Значит наблюдателю достаточно назвать номер отметки, а не угадывать число.
 *
 * Развёртка идёт ВВЕРХ и потом ВНИЗ: если порог «стало видно» и порог «стало
 * не видно» разойдутся, это скажет о гистерезисе раскачки, чего одна развёртка
 * не покажет.
 */
#include <SimpleFOC.h>

static const float    U_BASE = 2.0f;
static const float    W0 = 0.05f, W1 = 0.50f;
static const uint32_t LEG_MS = 135000;      // 9 отметок по 15 с
static const uint8_t  POLE_PAIRS = 11;
static const float    SUPPLY_V = 12.0f;
static const unsigned long SENS_MIN_US = 3, SENS_MAX_US = 919;
static const float TWO_PI_F = 6.28318530718f;

MagneticSensorPWM sensor = MagneticSensorPWM(PB8, SENS_MIN_US, SENS_MAX_US);
void doPWM() { sensor.handlePWM(); }
BLDCMotor      motor  = BLDCMotor(POLE_PAIRS);
BLDCDriver6PWM driver = BLDCDriver6PWM(A_PHASE_UH, A_PHASE_UL,
                                       A_PHASE_VH, A_PHASE_VL,
                                       A_PHASE_WH, A_PHASE_WL);

static void leg(bool up) {
  uint32_t t0 = millis(), next = micros(), mark = 0;
  Serial.print(F("#РАЗВЁРТКА ")); Serial.println(up ? F("ВВЕРХ") : F("ВНИЗ"));
  Serial.println(F("t_ms,theta,w_cmd"));
  while (millis() - t0 < LEG_MS) {
    float u = (millis() - t0) / (float)LEG_MS;
    float w = up ? (W0 + (W1 - W0) * u) : (W1 - (W1 - W0) * u);
    motor.loopFOC(); motor.move(w); sensor.update();
    uint32_t m = (millis() - t0) / 15000;
    if (m != mark) {                      // отметка: короткая вспышка
      mark = m;
      digitalWrite(LED_BUILTIN, LOW); delay(120);
      digitalWrite(LED_BUILTIN, HIGH);
      Serial.print(F("#ОТМЕТКА ")); Serial.print(mark);
      Serial.print(F(" w=")); Serial.println(w, 3);
    }
    if ((int32_t)(micros() - next) >= 0) {
      next += 20000;                       // 50 Гц
      Serial.print(millis() - t0); Serial.print(',');
      Serial.print(sensor.getAngle(), 5); Serial.print(',');
      Serial.println(w, 4);
    }
  }
  Serial.println(F("#КОНЕЦ"));
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  for (uint8_t i=0;i<5;i++){digitalWrite(LED_BUILTIN,HIGH);delay(80);digitalWrite(LED_BUILTIN,LOW);delay(80);}
  Serial.begin(115200);
  uint32_t t0=millis(); while(!Serial && millis()-t0<1500){}
  Serial.println(); Serial.println(F("=== развёртка скорости для глаза ==="));
  Serial.print(F("от ")); Serial.print(W0,2); Serial.print(F(" до "));
  Serial.print(W1,2); Serial.println(F(" рад/с, отметка каждые 15 с"));
  sensor.init(); sensor.enableInterrupt(doPWM);
  driver.voltage_power_supply=SUPPLY_V; driver.voltage_limit=SUPPLY_V; driver.init();
  motor.linkDriver(&driver);
  motor.controller=MotionControlType::velocity_openloop;
  motor.voltage_limit=U_BASE; motor.init();
  digitalWrite(LED_BUILTIN,HIGH);
  leg(true);
  leg(false);
  motor.move(0.0f); motor.voltage_limit=0.0f; motor.disable();
  Serial.println(F("#ВСЁ напряжение снято"));
  while(1){digitalWrite(LED_BUILTIN,HIGH);delay(60);digitalWrite(LED_BUILTIN,LOW);delay(60);}
}
void loop(){}
