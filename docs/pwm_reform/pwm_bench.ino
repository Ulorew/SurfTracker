// pwm_bench.ino — стенд для сравнения трактов чтения AS5048A по PWM.
// Плата: B-G431B-ESC1, STM32duino.
//
// Пишет в Serial двоичные записи PwmRec с частотой кадра энкодера (~1 кГц).
// Оба тракта опрашиваются в одном проходе, поэтому отсчёты парные.
//
// Команды по Serial (один байт):
//   '0' стоп мотора      '1' удержание позиции      '2' медленное вращение
//   's' развёртка разомкнутого контура (тест 2)
//   'g' пошаговый подъём усиления (тест 5)
//   'r' сброс счётчиков

#include "AS5048A_PWM.h"

BLDCMotor       motor  = BLDCMotor(11);          // GM4108: 11 пар полюсов
BLDCDriver6PWM  driver = BLDCDriver6PWM(A_PHASE_UH, A_PHASE_UL,
                                        A_PHASE_VH, A_PHASE_VL,
                                        A_PHASE_WH, A_PHASE_WL);
AS5048A_PWM sensor(MODE_CAPTURE);   // рабочий тракт для контура

uint16_t seq  = 0;
float    ref  = 0;
uint8_t  mode = 0;

// --- развёртка разомкнутого контура ---------------------------------------
// Электрический угол задаётся кварцем, поэтому это независимый эталон.
// 11 электрических оборотов = 1 механический. 60 с на оборот — ротор
// успевает идти за полем без раскачки.
bool     sweep_on   = false;
float    sweep_el   = 0;
uint32_t sweep_t0   = 0;
const float SWEEP_SEC  = 60.0f;
const float SWEEP_VOLT = 1.5f;      // хватает, чтобы держать ротор

void setup() {
  Serial.begin(2000000);

  // PB8 нужен как обычный вход для ISR-тракта; PA15 займёт TIM2.
  sensor.init();
  pwm_isr::begin(PB8);              // второй тракт слушает тот же сигнал

  driver.voltage_power_supply = 12;
  driver.init();
  motor.linkDriver(&driver);
  motor.linkSensor(&sensor);

  motor.foc_modulation    = FOCModulationType::SinePWM;
  motor.torque_controller = TorqueControlType::voltage;
  motor.controller        = MotionControlType::angle;
  motor.voltage_limit     = 3;
  motor.velocity_limit    = 7;      // рад/с, чуть выше 1 об/с

  motor.P_angle.P = 8;
  motor.PID_velocity.P = 0.3;
  motor.PID_velocity.I = 3;
  motor.LPF_velocity.Tf = 0.02;     // 20 мс — то же окно для всех трактов

  motor.init();
  motor.initFOC();
  motor.disable();
}

void handleCommand() {
  if (!Serial.available()) return;
  char c = Serial.read();
  switch (c) {
    case '0': motor.disable(); sweep_on = false; mode = 0; break;
    case '1': motor.enable();  sweep_on = false; mode = 1;
              motor.target = sensor.getAngle(); break;
    case '2': motor.enable();  sweep_on = false; mode = 2; break;
    case 's': motor.enable();  sweep_on = true;  mode = 3;
              sweep_el = 0; sweep_t0 = millis(); break;
    case 'r': sensor.bad_frames = 0; sensor.stale = 0; seq = 0; break;
  }
}

void loop() {
  handleCommand();

  if (sweep_on) {
    float t = (millis() - sweep_t0) / 1000.0f;
    if (t > SWEEP_SEC) { sweep_on = false; motor.disable(); mode = 0; }
    sweep_el = _2PI * 11.0f * (t / SWEEP_SEC);       // 11 эл. оборотов
    motor.setPhaseVoltage(SWEEP_VOLT, 0, sweep_el);
    ref = sweep_el;                                   // эталон в лог
  } else {
    motor.loopFOC();
    if (mode == 2) motor.move(motor.shaft_angle + 1.2f);  // ~0.2 об/с
    else if (mode == 1) motor.move();
    ref = motor.shaft_angle;
  }

  // ---- логирование ----
  static uint32_t last_log = 0;
  uint32_t now = micros();
  if (now - last_log < 900) return;      // чуть чаще кадра энкодера
  last_log = now;

  PwmRec r;
  r.sync = 0xA55A;
  r.seq  = seq++;
  r.t_us = now;
  r.ref  = ref;
  r.flags = 0;

  bool fresh;
  uint32_t p, h;
  if (!pwm_cap::read(p, h, fresh)) r.flags |= 1;
  if (!fresh)                      r.flags |= 4;
  r.cap_period = p;
  r.cap_high   = h;

  noInterrupts();
  r.isr_period = pwm_isr::period_us;
  r.isr_high   = pwm_isr::high_us;
  interrupts();
  if (r.isr_period < FRAME_US_MIN || r.isr_period > FRAME_US_MAX) r.flags |= 2;
  if (mode != 0) r.flags |= 8;

  r.crc = pwm_crc(r);
  Serial.write((uint8_t*)&r, sizeof(r));
}
