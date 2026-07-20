const uint8_t LDR_PIN = A7;

uint16_t readAverageADC() {
  const uint8_t sampleCount = 16;
  uint32_t total = 0;

  for (uint8_t i = 0; i < sampleCount; i++) {
    total += analogRead(LDR_PIN);
    delay(5);
  }

  return total / sampleCount;
}

void setup() {
  Serial.begin(9600);
}

void loop() {
  uint16_t adcValue = readAverageADC();

  Serial.print("ADC:");
  Serial.println(adcValue);

  delay(1000);
}
