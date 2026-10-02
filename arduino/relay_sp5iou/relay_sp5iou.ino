/*Serial interface controller relay switch
   Emulating SDR220 relay set
   By Marcin Boboli SP5IOU

   This file ships with HAMCTRL as the reference firmware for the
   ADMIN -> "PRZEKAZNIKI ARDUINO (SP5IOU SDR220)" panel (relay_controller.py
   on the HAMCTRL/Python side). Original work by Marcin Boboli (SP5IOU) -
   credit preserved. HAMCTRL only uses the relay subset of the protocol
   below (SKn/RKn/RPK/PK/RPKn); the digital-input, analog-input, event-
   counter and interrupt commands documented below are part of the
   original SP5IOU design but are not driven by HAMCTRL's own UI.

   FIX (bundled with HAMCTRL, see commit history): the original loop()
   matched SK0.."RPK7" OUTSIDE the "line fully received" (CR/LF) check,
   and never cleared `inString` after acting on a command. In practice
   that meant every command after the FIRST one received just kept
   appending onto the old string forever ("SK0" -> "SK0RK1" -> ...),
   which never matches any of the fixed command strings again - the
   board went permanently unresponsive to new commands until it was
   power-cycled or the serial port was reopened (reopening a port
   resets an Arduino via DTR, which is why this stayed unnoticed for a
   while - every reconnect "fixed" it by accident). Moved the matching
   block inside the CR/LF branch and added `inString = "";` at the end
   of it, so each line starts clean. No other behavior changed.
*/

/*
  RELAY OUTPUT COMMAND SUMMARY
  SKn Sets ( closes contact ) Relay specified by n ( n = 0 to 7 )
  RKn Resets ( opens contact ) Relay specified by n ( n = 0 to 7 )
  SPKxxxxxxxx Output binary data to Relay PORT K. ( x=1 or 0 )
  MKddd Outputs decimal data (ddd) to Relay PORT K. (ddd= 0 to 255 )
  RPK Returns status of all Relays in PORT K in binary format.
  RPKn Returns status of Relay specified by n in PORT K ( n = 0 to 7 )
  PK Returns status of Relay PORT K in decimal format.
  DIGITAL INPUT COMMAND SUMMARY
  RPA Returns status of all I/O lines in PORT A in binary format.
  RPAn Returns status of I/O line specified by n. (n= 0 to 3 )
  PA Returns status of PORT A in decimal format.
  EVENT COUNTER COMMAND SUMMARY
  CE Clear event counter.
  RE Returns present count of event counter.
  REC Returns present count of event counter and clears event counter.
  INTERRUPT COMMAND SUMMARY
  IE Enable Interrupts.
  ID Disable Interrupts
  IS Returns Interrupt Status ( 1 if enabled, 0 if disabled )
  TLnnnnn Loads Event Counter Trigger (nnnnn=0 to 65535 )
  TS Returns Event Counter Trigger Value
  ID COMMAND
  IDN? Returns 4 digit product identifier code.
  Feature added by SP5IOU - analog inputs
  RNn Returns decimal value of analog input n. (n=0 to 3)
  RNA Returns decimal values of all analog inputs AP0 to AP3 separated by <cr>
*/

#define FILENAME ADR2200_serial_ant_switch_SP5IOU_1_Beta_20160117_21_30
#define VER 2_Beta_20160117_21_30
//#define DEBUG true

//Product identifier
#define ProductID "SP5IOU"
//debuging LED
#define LED 13

//Relays output pins
#define REL0 2
#define REL1 3
#define REL2 4
#define REL3 5
#define REL4 6
#define REL5 7
#define REL6 8
#define REL7 9

//Input pins
#define IN0 10
#define IN1 11
#define IN2 12
#define IN3 A5

//Analog ports
#define AP0 A0
#define AP1 A1
#define AP2 A2
#define AP3 A3

String inString = "";    // string to hold input
String outString = "";   // string to hold output
uint16_t EventCounter = 0;
uint16_t EventCounterTrigger = 0;
boolean InterruptsStat = false;
byte commandNumber;

void setup() {
  //Set up LED and relay pins as output.
  pinMode(LED, OUTPUT);
  pinMode(REL0, OUTPUT);
  pinMode(REL1, OUTPUT);
  pinMode(REL2, OUTPUT);
  pinMode(REL3, OUTPUT);
  pinMode(REL4, OUTPUT);
  pinMode(REL5, OUTPUT);
  pinMode(REL6, OUTPUT);
  pinMode(REL7, OUTPUT);
  //Setting initial relays state
  digitalWrite(LED, 0);
  digitalWrite(REL0, 0);
  digitalWrite(REL1, 0);
  digitalWrite(REL2, 0);
  digitalWrite(REL3, 0);
  digitalWrite(REL4, 0);
  digitalWrite(REL5, 0);
  digitalWrite(REL6, 0);
  digitalWrite(REL7, 0);
  //Set up input pins
  pinMode(IN0, INPUT);
  pinMode(IN1, INPUT);
  pinMode(IN2, INPUT);
  pinMode(IN3, INPUT);
  //setting pullup resistors for input pins
  digitalWrite(IN0, 1);
  digitalWrite(IN1, 1);
  digitalWrite(IN2, 1);
  digitalWrite(IN3, 1);
  //Setting analog ports as input
  pinMode(AP0, INPUT);
  pinMode(AP1, INPUT);
  pinMode(AP2, INPUT);
  pinMode(AP3, INPUT);
  //Disconnecting pullup resistors from analog ports
  digitalWrite(AP0, 0);
  digitalWrite(AP1, 0);
  digitalWrite(AP2, 0);
  digitalWrite(AP3, 0);

  // Open serial communications and wait for port to open:
  Serial.begin(9600);
  while (!Serial) {
    ; // wait for serial port to connect. Needed for native USB port only
  }
}

void loop() {
  while (Serial.available() > 0) {   // when something read from serial
    int inChar = Serial.read();
    if (isPrintable(inChar)) {
      // convert the incoming byte to a char and add it to the string:
      inString += (char)inChar;
    }

    // Only act once a full line (CR or LF terminated) has arrived -
    // FIX: this used to be split across two blocks, with the command
    // matching running on every single incoming byte instead of only
    // here. isPrintable() already excludes CR/LF from being appended
    // above, so inString at this point holds exactly one command with
    // no trailing line terminator.
    if ((inChar == '\r') || (inChar == '\n')) {
      digitalWrite(LED, 1);
      //Make it accept commands in either lower or upper case
      inString.toUpperCase();
#if DEBUG
      Serial.print("String: ");
      Serial.println(inString);
#endif

      //Relays switching on commands
      if (inString == "SK0") {
        digitalWrite(REL0, 1);
      } else if (inString == "SK1") {
        digitalWrite(REL1, 1);
      } else if (inString == "SK2") {
        digitalWrite(REL2, 1);
      } else if (inString == "SK3") {
        digitalWrite(REL3, 1);
      } else if (inString == "SK4") {
        digitalWrite(REL4, 1);
      } else if (inString == "SK5") {
        digitalWrite(REL5, 1);
      } else if (inString == "SK6") {
        digitalWrite(REL6, 1);
      } else if (inString == "SK7") {
        digitalWrite(REL7, 1);
      } else if (inString == "RK0") {
        digitalWrite(REL0, 0);
      } else if (inString == "RK1") {
        digitalWrite(REL1, 0);
      } else if (inString == "RK2") {
        digitalWrite(REL2, 0);
      } else if (inString == "RK3") {
        digitalWrite(REL3, 0);
      } else if (inString == "RK4") {
        digitalWrite(REL4, 0);
      } else if (inString == "RK5") {
        digitalWrite(REL5, 0);
      } else if (inString == "RK6") {
        digitalWrite(REL6, 0);
      } else if (inString == "RK7") {
        digitalWrite(REL7, 0);
      }
      //Relay status reading commands
      else if (inString == "RPK") {
        Serial.print(digitalRead(REL7), BIN);
        Serial.print(digitalRead(REL6), BIN);
        Serial.print(digitalRead(REL5), BIN);
        Serial.print(digitalRead(REL4), BIN);
        Serial.print(digitalRead(REL3), BIN);
        Serial.print(digitalRead(REL2), BIN);
        Serial.print(digitalRead(REL1), BIN);
        Serial.print(digitalRead(REL0), BIN);
        Serial.print("\r");
      } else if (inString == "PK") {
        byte Portk_stat = digitalRead(REL0) + 2 * digitalRead(REL1) + 4 * digitalRead(REL2) + 8 * digitalRead(REL3) + 16 * digitalRead(REL4) + 32 * digitalRead(REL5) + 64 * digitalRead(REL6) + 128 * digitalRead(REL7);
        if (Portk_stat < 10) Serial.print('0');
        if (Portk_stat < 100) Serial.print('0');
        Serial.print(Portk_stat, DEC);
        Serial.print("\r");
      } else if (inString == "RPK0") {
        Serial.print(digitalRead(REL0), BIN);
        Serial.print('\r');
      } else if (inString == "RPK1") {
        Serial.print(digitalRead(REL1), BIN);
        Serial.print('\r');
      } else if (inString == "RPK2") {
        Serial.print(digitalRead(REL2), BIN);
        Serial.print('\r');
      } else if (inString == "RPK3") {
        Serial.print(digitalRead(REL3), BIN);
        Serial.print('\r');
      } else if (inString == "RPK4") {
        Serial.print(digitalRead(REL4), BIN);
        Serial.print('\r');
      } else if (inString == "RPK5") {
        Serial.print(digitalRead(REL5), BIN);
        Serial.print('\r');
      } else if (inString == "RPK6") {
        Serial.print(digitalRead(REL6), BIN);
        Serial.print('\r');
      } else if (inString == "RPK7") {
        Serial.print(digitalRead(REL7), BIN);
        Serial.print('\r');
      }
#if DEBUG
      else {
        Serial.print("Unknown command: ");
        Serial.println(inString);
      }
#endif

      // FIX: reset for the next line - without this, every command
      // after the first just kept appending onto the old string and
      // never matched anything again (see the FIX note at the top of
      // this file).
      inString = "";
      digitalWrite(LED, 0);
    }
  } // end while Serial.available
} // end main loop
