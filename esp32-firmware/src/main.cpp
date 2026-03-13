/*
 * Roast Printer — ESP32-C6 WiFi-to-Serial Bridge
 *
 * Receives raw ESC/POS data over TCP port 9100 (standard RAW
 * printing / JetDirect protocol) and forwards every byte to
 * the Epson TM-T88V receipt printer through:
 *
 *   ESP32-C6 UART1 TX → MAX3232 T1IN → T1OUT → TM-T88V RX
 *   ESP32-C6 UART1 RX ← MAX3232 R1OUT ← R1IN ← TM-T88V TX
 *
 * Also runs a tiny HTTP server on port 80 for status and a
 * test-print button.
 *
 * Wiring (Seeed XIAO ESP32-C6):
 *   D0 (GPIO2)  →  MAX3232 T1IN   (TX to printer)
 *   D1 (GPIO3)  ←  MAX3232 R1OUT  (RX from printer)
 *   3V3         →  MAX3232 VCC
 *   GND         →  MAX3232 GND + TM-T88V GND
 */

#include <WiFi.h>
#include <WebServer.h>
#include <HardwareSerial.h>

// ---------- build-time config (from platformio.ini) ----------
#ifndef WIFI_SSID
  #define WIFI_SSID "YOUR_WIFI_SSID"
#endif
#ifndef WIFI_PASS
  #define WIFI_PASS "YOUR_WIFI_PASSWORD"
#endif
#ifndef PRINTER_BAUD
  #define PRINTER_BAUD 38400
#endif
#ifndef PRINTER_TX
  #define PRINTER_TX 2
#endif
#ifndef PRINTER_RX
  #define PRINTER_RX 3
#endif
#ifndef LED_BUILTIN_PIN
  #define LED_BUILTIN_PIN 15
#endif

// ---------- globals ----------
HardwareSerial PrinterSerial(1);   // UART1
WiFiServer     printServer(9100);  // RAW print port
WebServer      httpServer(80);
volatile bool  printing = false;
uint32_t       jobCount = 0;
uint32_t       byteCount = 0;

// ---------- helpers ----------
void ledOn()  { digitalWrite(LED_BUILTIN_PIN, HIGH); }
void ledOff() { digitalWrite(LED_BUILTIN_PIN, LOW);  }

void blinkLed(int times, int ms = 100) {
  for (int i = 0; i < times; i++) {
    ledOn(); delay(ms); ledOff(); delay(ms);
  }
}

// ---------- RAW TCP print handler ----------
void handlePrintClient(WiFiClient &client) {
  printing = true;
  ledOn();

  unsigned long deadline = millis() + 30000;   // 30 s max per job
  size_t total = 0;
  uint8_t buf[512];

  while (client.connected() && millis() < deadline) {
    int avail = client.available();
    if (avail > 0) {
      size_t toRead = min((size_t)avail, sizeof(buf));
      size_t got = client.readBytes(buf, toRead);
      PrinterSerial.write(buf, got);
      total += got;
      deadline = millis() + 5000;   // reset timeout on activity
    } else {
      delay(1);
    }
  }

  PrinterSerial.flush();
  client.stop();

  jobCount++;
  byteCount += total;
  Serial.printf("[job %u] %u bytes forwarded to printer\n", jobCount, total);

  printing = false;
  ledOff();
}

// ---------- HTTP handlers ----------
void handleRoot() {
  String html = "<!DOCTYPE html><html><head><title>Roast Printer</title>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<style>body{font-family:monospace;max-width:480px;margin:2em auto;padding:0 1em}"
    "h1{border-bottom:2px solid #000}pre{background:#f4f4f4;padding:1em;overflow-x:auto}"
    ".btn{display:inline-block;padding:.6em 1.2em;background:#222;color:#fff;"
    "text-decoration:none;border:none;cursor:pointer;font-size:1em;margin:.5em 0}"
    "</style></head><body>"
    "<h1>&#x1F525; Roast Printer Bridge</h1>"
    "<pre>"
    "Status   : " + String(printing ? "PRINTING" : "IDLE") + "\n"
    "Jobs     : " + String(jobCount) + "\n"
    "Bytes    : " + String(byteCount) + "\n"
    "Uptime   : " + String(millis() / 1000) + " s\n"
    "WiFi RSSI: " + String(WiFi.RSSI()) + " dBm\n"
    "IP       : " + WiFi.localIP().toString() + "\n"
    "</pre>"
    "<p><a class='btn' href='/test'>&#x1F5A8; Print Test Page</a></p>"
    "<p>Send raw ESC/POS data to <b>TCP port 9100</b>.</p>"
    "</body></html>";
  httpServer.send(200, "text/html", html);
}

void handleStatus() {
  String json = "{";
  json += "\"status\":\"" + String(printing ? "printing" : "idle") + "\",";
  json += "\"jobs\":" + String(jobCount) + ",";
  json += "\"bytes\":" + String(byteCount) + ",";
  json += "\"uptime\":" + String(millis() / 1000) + ",";
  json += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
  json += "\"rssi\":" + String(WiFi.RSSI());
  json += "}";
  httpServer.send(200, "application/json", json);
}

void handleTestPrint() {
  if (printing) {
    httpServer.send(503, "text/plain", "Printer busy");
    return;
  }
  printing = true;
  ledOn();

  // ESC @ — initialise
  PrinterSerial.write(0x1B); PrinterSerial.write(0x40);
  // ESC a 1 — centre
  PrinterSerial.write(0x1B); PrinterSerial.write(0x61); PrinterSerial.write(0x01);
  // GS ! 0x11 — double size
  PrinterSerial.write(0x1D); PrinterSerial.write(0x21); PrinterSerial.write(0x11);
  PrinterSerial.println("ROAST PRINTER");
  // GS ! 0x00 — normal size
  PrinterSerial.write(0x1D); PrinterSerial.write(0x21); PrinterSerial.write(0x00);
  PrinterSerial.println("================================");
  PrinterSerial.println("WiFi-to-Serial bridge is working");
  PrinterSerial.print("IP : "); PrinterSerial.println(WiFi.localIP().toString());
  PrinterSerial.print("Up : "); PrinterSerial.print(millis() / 1000); PrinterSerial.println(" s");
  PrinterSerial.println("================================");
  // Feed + partial cut
  PrinterSerial.write(0x0A); PrinterSerial.write(0x0A); PrinterSerial.write(0x0A);
  PrinterSerial.write(0x1D); PrinterSerial.write(0x56); PrinterSerial.write(0x01);
  PrinterSerial.flush();

  printing = false;
  ledOff();
  httpServer.send(200, "text/plain", "Test page sent to printer");
}

// ---------- setup ----------
void setup() {
  Serial.begin(115200);
  Serial.println("\n=== Roast Printer Bridge ===");

  pinMode(LED_BUILTIN_PIN, OUTPUT);
  ledOff();

  // Printer UART
  PrinterSerial.begin(PRINTER_BAUD, SERIAL_8N1, PRINTER_RX, PRINTER_TX);
  Serial.printf("Printer UART: %d baud  TX=GPIO%d  RX=GPIO%d\n",
                PRINTER_BAUD, PRINTER_TX, PRINTER_RX);

  // WiFi
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("Connecting to %s ", WIFI_SSID);

  for (int i = 0; i < 60 && WiFi.status() != WL_CONNECTED; i++) {
    delay(500);
    Serial.print(".");
    digitalWrite(LED_BUILTIN_PIN, !digitalRead(LED_BUILTIN_PIN));
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\nWiFi failed — restarting");
    ESP.restart();
  }
  Serial.printf("\nConnected  IP: %s\n", WiFi.localIP().toString().c_str());

  // Send ESC @ to initialise printer on boot
  PrinterSerial.write(0x1B); PrinterSerial.write(0x40);
  PrinterSerial.flush();

  // TCP RAW print server
  printServer.begin();
  Serial.println("RAW print server listening on :9100");

  // HTTP status server
  httpServer.on("/",      HTTP_GET, handleRoot);
  httpServer.on("/status", HTTP_GET, handleStatus);
  httpServer.on("/test",   HTTP_GET, handleTestPrint);
  httpServer.begin();
  Serial.println("HTTP server listening on :80");

  blinkLed(3);
}

// ---------- loop ----------
void loop() {
  httpServer.handleClient();

  // Accept incoming RAW print jobs
  WiFiClient client = printServer.available();
  if (client) {
    Serial.println("Print client connected");
    handlePrintClient(client);
  }

  // Auto-reconnect WiFi
  static unsigned long lastWifiCheck = 0;
  if (millis() - lastWifiCheck > 10000) {
    lastWifiCheck = millis();
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("WiFi lost — reconnecting");
      WiFi.reconnect();
    }
  }
}
