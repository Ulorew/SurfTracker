#include <AccelStepper.h>

// --- Пины под CNC Shield V3 (ось X) ---
const uint8_t EN_PIN   = 8;   // EN
const uint8_t STEP_PIN = 2;   // X.STEP
const uint8_t DIR_PIN  = 5;   // X.DIR
const bool EN_ACTIVE_LOW = true;

// --- Движок AccelStepper в режиме DRIVER ---
AccelStepper stepper(AccelStepper::DRIVER, STEP_PIN, DIR_PIN);

// --- Пределы и лимиты ---
const long MIN_POS = -10000L;
const long MAX_POS =  10000L;
const long V_MAX   =  2000L;   // макс скорость в шагах/с (подстрой)

// --- П-регулятор по позиции: v_corr = (KP_NUM/KP_DEN)*err ---
const int32_t KP_NUM = 1;       // уменьшить реакцию -> увеличить DEN
const int32_t KP_DEN = 2;       // 0.5*ошибка

// --- Полиномиальные коэффициенты (int32) ---
volatile int32_t Pos0 = 0;  // шаги
volatile int32_t Vel0 = 0;  // шаг/с
volatile int32_t Acc0 = 0;  // шаг/с^2
volatile int32_t Thd0 = 0;  // шаг/с^3
volatile uint32_t T0_ms = 0;

char lastVarId='-';
String inputBuffer;

// goal_pos(t) в int (твой безопасный шаблон)
long compute_goal_int32(int32_t dt_ms, int32_t p0, int32_t v0, int32_t a0, int32_t t0) {
  int32_t goal = p0 + ((v0 + ((a0 + (t0 * dt_ms) / (int32_t)3000) * dt_ms) / (int32_t)2000) * dt_ms) / (int32_t)1000;
  if (goal < MIN_POS) goal = MIN_POS;
  if (goal > MAX_POS) goal = MAX_POS;
  return (long)goal;
}

// v_poly(t) = 3*T*t^2/1e6 + 2*A*t/1e3 + V  (в шагах/с)
long compute_velocity_sps(int32_t dt_ms, int32_t v0, int32_t a0, int32_t t0) {
  int32_t v = v0 + ((a0 + (t0 * dt_ms) / (int32_t)2000) * dt_ms) / (int32_t)1000;
  if (v > INT32_MAX) v = INT32_MAX;
  if (v < INT32_MIN) v = INT32_MIN;
  return (long)v;
}

void processSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c >= 'A' && c <= 'Z') { lastVarId = c; inputBuffer = ""; continue; }
    if (c == '\n' || c == '\r') {
      if (inputBuffer.length()>0) {
        long val = inputBuffer.toInt();
        switch (lastVarId) {
          case 'P': noInterrupts(); Pos0 = val; T0_ms = millis(); interrupts(); Serial.print("P->"); Serial.println(val); break;
          case 'V': noInterrupts(); Vel0 = val; interrupts(); Serial.print("V->"); Serial.println(val); break;
          case 'A': noInterrupts(); Acc0 = val; interrupts(); Serial.print("A->"); Serial.println(val); break;
          case 'T': noInterrupts(); Thd0 = val; interrupts(); Serial.print("T->"); Serial.println(val); break;
          case 'E':
            if (val==0) { digitalWrite(EN_PIN, EN_ACTIVE_LOW?HIGH:LOW); Serial.println("DIS"); }
            else        { digitalWrite(EN_PIN, EN_ACTIVE_LOW?LOW:HIGH);  Serial.println("ENA"); }
            break;
          default: break;
        }
      }
      inputBuffer = ""; continue;
    }
    if ((c>='0' && c<='9') || c=='-') inputBuffer += c;
  }
}

long lastComputeMs = 0, lastDbgMs = 0;

void setup() {
  pinMode(EN_PIN, OUTPUT);
  digitalWrite(EN_PIN, EN_ACTIVE_LOW?LOW:HIGH); // ENA по умолчанию

  stepper.setMaxSpeed(5000);    // не участвует в runSpeed, но пусть будет «безлимит»
  stepper.setAcceleration(1000);     // не участвует в runSpeed
  stepper.setCurrentPosition(0);

  Serial.begin(115200);
  while (!Serial && millis() < 500) {}
  Serial.println("Vel-follow with AccelStepper.runSpeed()");
  Serial.println("Cmds: E1/E0, P<int>, V<int>, A<int>, T<int>");
}

void loop() {
  processSerial();
  long nowMs = millis();
  if (nowMs - lastComputeMs >= 4) {  // каждые ~4ms — обновляем скорость
    noInterrupts();
    uint32_t t0 = T0_ms;
    int32_t p0 = Pos0, v0 = Vel0, a0 = Acc0, t3 = Thd0;
    interrupts();

    int32_t dt_ms = nowMs - t0;

    long goal = compute_goal_int32(dt_ms, p0, v0, a0, t3);
    long vpoly = compute_velocity_sps(dt_ms, v0, a0, t3);

    long cur  = stepper.currentPosition();     // позиция AccelStepper
    long err  = goal - cur;                    // ошибка по позиции
    long vcorr = (long)(((int64_t)KP_NUM * err) / KP_DEN);

    long vcmd = vpoly + vcorr;
    if (vcmd >  V_MAX) vcmd =  V_MAX;
    if (vcmd < -V_MAX) vcmd = -V_MAX;

    // AccelStepper принимает float steps/s
    stepper.setSpeed((float)vcmd);

    lastComputeMs = nowMs;
  }

  // ГЕНЕРАЦИЯ ШАГОВ ПО СКОРОСТИ (не останавливается)
  stepper.runSpeed();

  // Отладка раз в 200мс
  if (nowMs - lastDbgMs >= 20) {
    noInterrupts();
    uint32_t t0 = T0_ms; int32_t p0 = Pos0, v0 = Vel0, a0 = Acc0, t3 = Thd0;
    interrupts();
    uint32_t dt_ms = nowMs - t0;
    long goal = compute_goal_int32(dt_ms, p0, v0, a0, t3);
    long vpoly = compute_velocity_sps(dt_ms, v0, a0, t3);
    long cur  = stepper.currentPosition();
    long err  = goal - cur;
    float vset = stepper.speed(); // что мы ему задали
//    Serial.print("t="); Serial.print(nowMs);
//    Serial.print(" cur=");
    Serial.println(cur);
//    Serial.print(" goal="); Serial.print(goal);
//    Serial.print(" err="); Serial.print(err);
//    Serial.print(" vpoly="); Serial.print(vpoly);
//    Serial.print(" vcmd="); Serial.print((long)vset);
//    Serial.print(" P,V,A,T="); Serial.print(p0); Serial.print(","); Serial.print(v0);
//    Serial.print(","); Serial.print(a0); Serial.print(","); Serial.println(t3);
    lastDbgMs = nowMs;
  }
}
