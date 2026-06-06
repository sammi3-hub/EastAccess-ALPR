/*
  EastAccess ESP32 Two-Servo Boom Barrier Controller

  Setup:
  - Entry/Entrance servo signal pin: GPIO 13
  - Exit servo signal pin: GPIO 25
  - Servo external 5V power supply recommended
  - IMPORTANT: ESP32 GND and external servo power GND must be connected together

  Install in Arduino IDE:
  Tools > Manage Libraries > search "ESP32Servo" > Install
*/

#include <ESP32Servo.h>

Servo entryServo;
Servo exitServo;

// Change ENTRY_SERVO_PIN if your entrance servo is not on GPIO 13.
const int ENTRY_SERVO_PIN = 13;
const int EXIT_SERVO_PIN = 25;

// Adjust these angles if your barrier direction is reversed.
const int ENTRY_CLOSED_ANGLE = 0;
const int ENTRY_OPEN_ANGLE = 90;

const int EXIT_CLOSED_ANGLE = 0;
const int EXIT_OPEN_ANGLE = 90;

// Automatic close delay for ALPR accepted vehicles.
const unsigned long AUTO_CLOSE_DELAY_MS = 10000;

unsigned long entryAutoCloseAt = 0;
unsigned long exitAutoCloseAt = 0;

String inputCommand = "";

void openEntry(bool autoClose) {
  entryServo.write(ENTRY_OPEN_ANGLE);

  if (autoClose) {
    entryAutoCloseAt = millis() + AUTO_CLOSE_DELAY_MS;
  } else {
    entryAutoCloseAt = 0;
  }

  Serial.println(autoClose ? "ENTRY AUTO OPENED" : "ENTRY MANUAL OPENED");
}

void closeEntry() {
  entryServo.write(ENTRY_CLOSED_ANGLE);
  entryAutoCloseAt = 0;
  Serial.println("ENTRY CLOSED");
}

void openExit(bool autoClose) {
  exitServo.write(EXIT_OPEN_ANGLE);

  if (autoClose) {
    exitAutoCloseAt = millis() + AUTO_CLOSE_DELAY_MS;
  } else {
    exitAutoCloseAt = 0;
  }

  Serial.println(autoClose ? "EXIT AUTO OPENED" : "EXIT MANUAL OPENED");
}

void closeExit() {
  exitServo.write(EXIT_CLOSED_ANGLE);
  exitAutoCloseAt = 0;
  Serial.println("EXIT CLOSED");
}

void processCommand(String command) {
  command.trim();
  command.toUpperCase();

  if (command.length() == 0) {
    return;
  }

  Serial.print("Received: ");
  Serial.println(command);

  // Automatic ALPR commands from Python
  if (command == "AUTO_OPEN_ENTRY") {
    openEntry(true);
  }
  else if (command == "AUTO_OPEN_EXIT") {
    openExit(true);
  }

  // Manual dashboard commands
  else if (command == "OPEN_ENTRY") {
    openEntry(false);
  }
  else if (command == "CLOSE_ENTRY") {
    closeEntry();
  }
  else if (command == "OPEN_EXIT") {
    openExit(false);
  }
  else if (command == "CLOSE_EXIT") {
    closeExit();
  }

  // Legacy support
  else if (command == "CLOSE") {
    closeEntry();
    closeExit();
  }

  // Denied commands: no barrier movement
  else if (command == "DENIED_ENTRY") {
    Serial.println("ENTRY DENIED - barrier remains closed");
  }
  else if (command == "DENIED_EXIT") {
    Serial.println("EXIT DENIED - barrier remains closed");
  }
  else {
    Serial.println("Unknown command");
  }
}

void setup() {
  Serial.begin(115200);

  entryServo.setPeriodHertz(50);
  exitServo.setPeriodHertz(50);

  entryServo.attach(ENTRY_SERVO_PIN, 500, 2400);
  exitServo.attach(EXIT_SERVO_PIN, 500, 2400);

  closeEntry();
  closeExit();

  Serial.println("EastAccess Two-Servo Controller Ready");
  Serial.println("Commands: AUTO_OPEN_ENTRY, AUTO_OPEN_EXIT, OPEN_ENTRY, CLOSE_ENTRY, OPEN_EXIT, CLOSE_EXIT");
}

void loop() {
  while (Serial.available() > 0) {
    char incomingChar = Serial.read();

    if (incomingChar == '\n' || incomingChar == '\r') {
      processCommand(inputCommand);
      inputCommand = "";
    } else {
      inputCommand += incomingChar;
    }
  }

  unsigned long currentTime = millis();

  if (entryAutoCloseAt > 0 && currentTime >= entryAutoCloseAt) {
    closeEntry();
  }

  if (exitAutoCloseAt > 0 && currentTime >= exitAutoCloseAt) {
    closeExit();
  }
}
