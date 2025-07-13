//https://www.aranacorp.com/en/using-an-arduino-cnc-shield-v3/
#include <AccelStepper.h>

// Define motor interface type
#define MOTOR_INTERFACE_TYPE AccelStepper::DRIVER

const int enPin=8;
const int stepXPin = 2; //X.STEP
const int dirXPin = 5; // X.DIR
const int stepYPin = 3; //Y.STEP
const int dirYPin = 6; // Y.DIR
const int stepZPin = 4; //Z.STEP
const int dirZPin = 7; // Z.DIR

int stepPin=stepXPin;
int dirPin=dirXPin;

const long stepsPerRev=1600; // stepper
int stepsPerChase=10;
int pulseWidthMicros = 100;  // microseconds
long millisBtwnSteps = 1000;

const long stepsPerRot=8800; // camera

AccelStepper stepper(MOTOR_INTERFACE_TYPE, stepPin, dirPin);


void setup() {
  Serial.begin(115200);
  Serial.setTimeout(2);
  pinMode(enPin, OUTPUT);
  digitalWrite(enPin, LOW);
  pinMode(stepPin, OUTPUT);
  pinMode(dirPin, OUTPUT);
  stepper.setMaxSpeed(15000);  // Set maximum speed (steps/second)
  stepper.setAcceleration(30000);  // Set acceleration (steps/second^2)
 
  Serial.println(F("CNC Shield Initialized"));
}

long goal=0;
bool curDir=0; // 0-> CCW, 1-> CW  -10000
long lastWrite=-10000;

const long minPos=-stepsPerRot/4, maxPos=stepsPerRot/4;

void sendPos(){
  long curtime=millis();
  if (abs(curtime-lastWrite)>=2){
    Serial.println(stepper.currentPosition());
    lastWrite=curtime;
  }
}

void wiggle(long period_len, long amplitude){
  long st_time=millis();
  while (millis()<st_time+period_len){
    sendPos();
    double arg=(millis()-st_time+0.0)/period_len;
    long goal=round(sin(arg*2*PI)*amplitude);
    stepper.moveTo(goal);
    stepper.run();
    //Serial.println(stepper.currentPosition());
    //delay(10);32123 1600
  }
}

bool wiggling=false;

String inputBuffer;

void loop() {
  sendPos();

  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      long val = inputBuffer.toInt();
      goal = val;        
      inputBuffer = "";
    } else if ((c >= '0' && c <= '9') || c == '-') {
      inputBuffer += c;
      // ignore any other characters
    }
  }

  
  if (goal==32123){
    wiggling=true;
    goal=0;
  }
  else{
    //Serial.println("Got "+String(goal));32123
    wiggling=false;
    goal=min(max(goal, minPos), maxPos);
    stepper.moveTo(goal);
  }

  if (wiggling){
    wiggle(10000, stepsPerRot/2);
    wiggling=false;
  }
    
  stepper.run();
}