#include <Servo.h>

Servo kaattMotor;
const int motorPin = 9;

void setup() {
  Serial.begin(115200);
  kaattMotor.attach(motorPin);
  Serial.println("Ain't it hot, have some kaatt");
}

void loop() {
  for (int angle = 20; angle <= 160; angle += 2) {
    kaattMotor.write(angle);
    delay(20);
  }
  for (int angle = 160; angle >= 20; angle -= 2) {
    kaattMotor.write(angle);
    delay(20);
  }
}
