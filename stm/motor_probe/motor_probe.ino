/*
 * Проверка: крутится ли мотор в разомкнутом режиме.
 *
 * ГЛАВНОЕ, ради чего скетч остаётся в репозитории. В SimpleFOC 2.4.0
 * loopFOC() ОБЯЗАТЕЛЕН и в разомкнутом контуре: именно он считает
 * электрический угол для open-loop и подаёт напряжение на фазы, тогда как
 * move() лишь обновляет shaft_angle и вычисляет current_sp. В 2.3.x
 * напряжение подавалось внутри velocityOpenloop, поэтому старый скетч
 * работал без loopFOC.
 *
 * Симптом ошибки: driver.init()=1, motor.init()=1, enabled=1, shaft_angle
 * растёт ровно на заданной скорости — а на фазах 0 В и вал стоит.
 */
#include <SimpleFOC.h>

MagneticSensorPWM sensor = MagneticSensorPWM(3, 7, 920);
void doPWM() { sensor.handlePWM(); }

BLDCMotor      motor  = BLDCMotor(11);
BLDCDriver3PWM driver = BLDCDriver3PWM(9, 5, 6, 8);

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}
  delay(300);
  Serial.println(F("\n=== разомкнутый режим с loopFOC ==="));
  sensor.init();
  sensor.enableInterrupt(doPWM);
  driver.voltage_power_supply = 12;
  Serial.print(F("driver.init() -> ")); Serial.println(driver.init());
  motor.linkDriver(&driver);
  motor.controller = MotionControlType::velocity_openloop;
  motor.voltage_limit  = 1.0f;      // как в боевой прошивке
  motor.velocity_limit = 6.0f;
  Serial.print(F("motor.init() -> ")); Serial.println(motor.init());
  Serial.print(F("enabled=")); Serial.println(motor.enabled);
  Serial.println(F("цель 0.5 рад/с, датчик обязан пойти"));
}

void loop() {
  static uint32_t last = 0;
  motor.loopFOC();          // БЕЗ ЭТОГО на фазах ноль (см. шапку)
  motor.move(0.5f);
  if (millis() - last > 1000) {
    last = millis();
    sensor.update();
    Serial.print(F("shaft_angle ")); Serial.print(motor.shaft_angle, 3);
    Serial.print(F(" | ДАТЧИК ")); Serial.println(sensor.getAngle(), 3);
  }
}
