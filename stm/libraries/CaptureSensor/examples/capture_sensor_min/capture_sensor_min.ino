/*
 * Минимальный пример: захват TIM2 как датчик SimpleFOC.
 *
 * Скетч НУЖЕН НЕ ДЛЯ ЗАЛИВКИ, а для того, чтобы библиотека собиралась под
 * плату в отрыве от боевой прошивки. Хостовый стенд проверяет логику, но не
 * проверяет, что заголовки HAL и SimpleFOC сходятся на целевой сборке.
 *
 *   ~/Android/arduino/arduino-cli compile \
 *      -b STMicroelectronics:stm32:Disco:pnum=B_G431B_ESC1 \
 *      stm/libraries/CaptureSensor/examples/capture_sensor_min
 *
 * МОТОР ЗДЕСЬ НЕ ЗАПУСКАЕТСЯ НАМЕРЕННО: пример показывает чтение датчика и
 * опрос годности, а не управление валом.
 */
#include <SimpleFOC.h>
#include <CaptureSensor.h>

CaptureSensor sensor;

void setup() {
  Serial.begin(115200);
  sensor.init();
}

void loop() {
  sensor.update();
  // ГОДНОСТЬ СПРАШИВАЕТСЯ ДО ИСПОЛЬЗОВАНИЯ УГЛА. В замкнутом контуре здесь
  // стоял бы motor.disable(): по самому числу удержание от неподвижного вала
  // не отличить.
  const CaptureStatus &st = sensor.lastStatus();
  Serial.print(sensor.getAngle());
  Serial.print(sensor.isHealthy() ? "  ok " : "  НЕГОДЕН ");
  Serial.print(sensor.badStreak());
  Serial.print("  period="); Serial.print(st.period);
  Serial.print(" high="); Serial.print(st.high);
  Serial.print(" age="); Serial.println(st.age);
  delay(100);
}
