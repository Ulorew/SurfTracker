long lo = 999999, hi = 0;

void setup() {
  Serial.begin(115200);
  pinMode(3, INPUT);
}

void loop() {
  long v = pulseIn(3, HIGH, 50000);
  if (v > 0) {
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  Serial.print(lo);
  Serial.print("\t");
  Serial.println(hi);
  delay(50);
}