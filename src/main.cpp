#include <Arduino.h>
#include <ArduinoJson.h>
#include <Arduino_GFX_Library.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

#include "monitor_config.h"

namespace {

constexpr int16_t kMargin = 14;
constexpr int kBacklightPin = 27;
constexpr int kTouchCsPin = 33;
constexpr int kTouchIrqPin = 36;
constexpr int kTouchRawMin = 220;
constexpr int kTouchRawMax = 3900;
constexpr int kTouchPressureMin = 150;
constexpr unsigned long kTouchToggleCooldownMs = 600;
constexpr int kPanelCornerRadius = 16;
constexpr int kPanelPadding = 10;
constexpr int kPanelGap = 10;
constexpr int kRowGap = 8;
constexpr int kBarHeight = 14;
constexpr int kHeaderHeight = 40;
constexpr int kFooterHeight = 0;

constexpr uint16_t kColorBackground = 0x0841;
constexpr uint16_t kColorPanel = 0x18C3;
constexpr uint16_t kColorPanelBorder = 0x2965;
constexpr uint16_t kColorText = 0xFFFF;
constexpr uint16_t kColorDim = 0x9CF3;
constexpr uint16_t kColorAccent = 0x051D;
constexpr uint16_t kColorGreen = 0x3666;
constexpr uint16_t kColorGreenDim = 0x1B43;  // ~50% brightness of kColorGreen
constexpr uint16_t kColorAmber = 0xFD20;
constexpr uint16_t kColorRed = 0xD104;
constexpr uint16_t kColorBarTrack = 0x3186;
constexpr uint16_t kColorButtonFill = 0x10A2;
constexpr uint16_t kColorClaude = 0xFC60;  // warm orange (255, 140, 0)
constexpr uint16_t kColorCodex  = 0x05B6;  // teal (0, 180, 176)

Arduino_DataBus *bus = new Arduino_ESP32SPI(
    2 /* DC */, 15 /* CS */, 14 /* SCK */, 13 /* MOSI */, 12 /* MISO */, HSPI);
Arduino_GFX *gfx =
    new Arduino_ST7796(bus, GFX_NOT_DEFINED /* RST */, monitor_config::kDisplayRotation);
SPIClass touchSpi(HSPI);

struct UsagePayload {
  int sessionPct = 0;
  char sessionResetAt[40] = "";
  int weeklyPct = 0;
  char weeklyResetAt[40] = "";
  int weeklyBudgetPct = 0;
  char status[24] = "waiting";
  char updatedAt[40] = "";
  bool ok = false;
  bool valid = false;
  unsigned long receivedAtMs = 0;
};

UsagePayload claudeUsage;
UsagePayload codexUsage;

struct WifiProfile {
  const char *label;
  const char *ssid;
  const char *password;
};

#if defined(HOME_WIFI_SSID) && defined(HOME_WIFI_PASSWORD)
constexpr WifiProfile kHomeWifiProfile = {"HOME", HOME_WIFI_SSID, HOME_WIFI_PASSWORD};
#else
constexpr WifiProfile kHomeWifiProfile = {"HOME", monitor_config::kWifiSsid,
                                          monitor_config::kWifiPassword};
#endif

#if defined(OFFICE_WIFI_SSID) && defined(OFFICE_WIFI_PASSWORD)
constexpr WifiProfile kOfficeWifiProfile = {"OFFICE", OFFICE_WIFI_SSID, OFFICE_WIFI_PASSWORD};
#else
constexpr WifiProfile kOfficeWifiProfile = {"OFFICE", "", ""};
#endif

constexpr WifiProfile kWifiProfiles[] = {kHomeWifiProfile, kOfficeWifiProfile};

enum class WifiState {
  MissingConfig,
  Connecting,
  Connected,
};

WifiState wifiState = WifiState::MissingConfig;

bool needsFullRender = true;
bool needsValueRender = false;
bool touchWasPressed = false;
unsigned long lastWifiAttemptMs = 0;
unsigned long wifiAttemptStartedMs = 0;
unsigned long lastClaudeFirestorePollMs = 0;
unsigned long lastCodexFirestorePollMs = 0;
unsigned long lastTouchToggleMs = 0;
size_t activeWifiProfileIndex = 0;

struct PanelLayout {
  int16_t x = 0;
  int16_t y = 0;
  int16_t w = 0;
  int16_t h = 0;
};

PanelLayout claudeSessionPanel;
PanelLayout claudeWeeklyPanel;
PanelLayout codexSessionPanel;
PanelLayout codexWeeklyPanel;
PanelLayout wifiButtonRect;

void beginWifiConnection();

void setPanelLayout(PanelLayout &panel, int16_t x, int16_t y, int16_t w, int16_t h) {
  panel.x = x;
  panel.y = y;
  panel.w = w;
  panel.h = h;
}

const WifiProfile &activeWifiProfile() {
  return kWifiProfiles[activeWifiProfileIndex];
}

bool profileConfigured(const WifiProfile &profile) {
  return profile.ssid != nullptr && profile.password != nullptr && strlen(profile.ssid) > 0;
}

bool wifiConfigured() {
  return profileConfigured(activeWifiProfile());
}

bool alternateWifiConfigured() {
  return profileConfigured(kWifiProfiles[(activeWifiProfileIndex + 1) % 2]);
}

bool firestoreConfigured() {
  return strlen(monitor_config::kFirestoreProjectId) > 0 &&
         strlen(monitor_config::kFirestoreDocumentPath) > 0;
}

bool dataIsStale() {
  const unsigned long now = millis();
  const auto stale = [&](const UsagePayload &u) {
    return u.valid && (now - u.receivedAtMs > monitor_config::kDataStaleAfterMs);
  };
  return stale(claudeUsage) || stale(codexUsage);
}

uint16_t colorForPercent(int pct) {
  if (pct >= 80) {
    return kColorRed;
  }
  if (pct >= 50) {
    return kColorAmber;
  }
  return kColorGreen;
}

int textWidth(const String &text, uint8_t size) {
  return static_cast<int>(text.length()) * 6 * size;
}

void drawText(int16_t x, int16_t y, const String &text, uint16_t color, uint8_t size) {
  gfx->setTextColor(color);
  gfx->setTextSize(size);
  gfx->setCursor(x, y);
  gfx->print(text);
}

void drawCenteredText(int16_t centerX, int16_t y, const String &text, uint16_t color,
                      uint8_t size) {
  drawText(centerX - textWidth(text, size) / 2, y, text, color, size);
}

String weekdayLabel(int year, int month, int day) {
  if (month < 3) {
    month += 12;
    year -= 1;
  }

  const int k = year % 100;
  const int j = year / 100;
  const int h =
      (day + ((13 * (month + 1)) / 5) + k + (k / 4) + (j / 4) + (5 * j)) % 7;
  static const char *kWeekdays[] = {"Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri"};
  return String(kWeekdays[h]);
}

String formatResetTime(const char *timestamp, bool weeklyStyle) {
  if (timestamp == nullptr || timestamp[0] == '\0') {
    return "Reset: --";
  }

  bool looksLikeMinutes = true;
  for (size_t i = 0; timestamp[i] != '\0'; ++i) {
    const char ch = timestamp[i];
    if (i == 0 && ch == '-') {
      continue;
    }
    if (ch == 'm' && timestamp[i + 1] == '\0') {
      break;
    }
    if (!isDigit(ch)) {
      looksLikeMinutes = false;
      break;
    }
  }
  if (looksLikeMinutes && strchr(timestamp, 'm') != nullptr) {
    return "Reset: " + String(timestamp);
  }

  int year = 0;
  int month = 0;
  int day = 0;
  int hour = 0;
  int minute = 0;
  int second = 0;
  if (sscanf(timestamp, "%d-%d-%dT%d:%d:%d", &year, &month, &day, &hour, &minute, &second) !=
      6) {
    return "Reset: --";
  }

  char buf[24];
  if (weeklyStyle) {
    snprintf(buf, sizeof(buf), "Reset: %s %02d%02d", weekdayLabel(year, month, day).c_str(), hour,
             minute);
  } else {
    const bool isPm = hour >= 12;
    int hour12 = hour % 12;
    if (hour12 == 0) {
      hour12 = 12;
    }
    snprintf(buf, sizeof(buf), "Reset: %d:%02d%s", hour12, minute, isPm ? "pm" : "am");
  }
  return String(buf);
}

bool shouldUseLandscapePanels() {
  return gfx->width() >= gfx->height();
}

const char *connectionLabel() {
  if (!wifiConfigured()) {
    return "CONFIG WIFI";
  }
  if (!firestoreConfigured()) {
    return "CONFIG DATA";
  }
  if (wifiState == WifiState::Connecting) {
    return "CONNECTING";
  }
  if (!claudeUsage.valid && !codexUsage.valid) {
    return "POLLING";
  }
  if (dataIsStale()) {
    return "STALE";
  }
  if ((claudeUsage.valid && !claudeUsage.ok) || (codexUsage.valid && !codexUsage.ok)) {
    return "ERROR";
  }
  return "LIVE";
}

uint16_t connectionColor() {
  if (!wifiConfigured() || !firestoreConfigured()) {
    return kColorAmber;
  }
  if (wifiState == WifiState::Connecting) {
    return kColorAccent;
  }
  if (!claudeUsage.valid && !codexUsage.valid) {
    return kColorAccent;
  }
  if (dataIsStale()) {
    return kColorRed;
  }
  if ((claudeUsage.valid && !claudeUsage.ok) || (codexUsage.valid && !codexUsage.ok)) {
    return kColorRed;
  }
  return kColorGreen;
}

void drawStatusPill(int16_t x, int16_t y, int16_t w, int16_t h) {
  gfx->fillRoundRect(x, y, w, h, h / 2, kColorPanel);
  gfx->drawRoundRect(x, y, w, h, h / 2, connectionColor());
  drawCenteredText(x + w / 2, y + (h - 16) / 2, connectionLabel(), connectionColor(), 2);
}

void drawWifiButton(int16_t x, int16_t y, int16_t w, int16_t h) {
  const bool readyToToggle = alternateWifiConfigured();
  const uint16_t borderColor = readyToToggle ? kColorAccent : kColorPanelBorder;

  gfx->fillRoundRect(x, y, w, h, h / 2, kColorButtonFill);
  gfx->drawRoundRect(x, y, w, h, h / 2, borderColor);
  drawCenteredText(x + w / 2, y + (h - 16) / 2, String(activeWifiProfile().label) + " WIFI",
                   kColorText, 2);
}

void clearRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t color) {
  gfx->fillRect(x, y, w, h, color);
}

void drawUsagePanelFrame(int16_t x, int16_t y, int16_t w, int16_t h, const char *title,
                         uint16_t accentColor) {
  const int innerX = x + kPanelPadding;
  const int innerY = y + kPanelPadding;

  gfx->fillRoundRect(x, y, w, h, kPanelCornerRadius, kColorPanel);
  gfx->drawRoundRect(x, y, w, h, kPanelCornerRadius, accentColor);
  drawText(innerX, innerY, title, accentColor, 2);
}

void drawUsagePanelValues(const PanelLayout &panel, int pct, const char *resetAt,
                          bool weeklyStyle, int budgetPct = 0) {
  // For weekly panels with a budget: green while under, red when over.
  // For session panels (or no budget yet): fall back to absolute thresholds.
  const bool useBudgetColor = weeklyStyle && budgetPct > 0;
  const uint16_t pctColor = useBudgetColor
                                 ? (pct > budgetPct ? kColorRed : kColorGreen)
                                 : colorForPercent(pct);
  const int innerX = panel.x + kPanelPadding;
  const int innerY = panel.y + kPanelPadding;
  const int barW = panel.w - (2 * kPanelPadding);
  const int barY = panel.y + panel.h - kBarHeight - 5;
  const int fillW = map(constrain(pct, 0, 100), 0, 100, 0, barW);
  const int valueAreaY = innerY + 20;
  const int valueAreaH = barY - valueAreaY;

  clearRect(innerX, valueAreaY, barW, valueAreaH, kColorPanel);
  clearRect(innerX, barY, barW, kBarHeight, kColorBarTrack);

  drawText(innerX, innerY + 26, String(pct) + "%", pctColor, 2);
  drawText(innerX, innerY + 56, formatResetTime(resetAt, weeklyStyle), kColorText, 2);

  // Orange budget underlay drawn first so the actual bar paints over it.
  if (useBudgetColor) {
    const int budgetW = map(constrain(budgetPct, 0, 100), 0, 100, 0, barW);
    if (budgetW > 0) {
      gfx->fillRoundRect(innerX, barY, budgetW, kBarHeight, kBarHeight / 2, kColorGreenDim);
    }
  }
  if (fillW > 0) {
    gfx->fillRoundRect(innerX, barY, fillW, kBarHeight, kBarHeight / 2, pctColor);
  }
}

void computePanelLayout() {
  const int16_t screenW = gfx->width();
  const int16_t screenH = gfx->height();

  // WiFi button sits left of the status pill in the top-right corner.
  // Pill=128px, gap=8px, button=136px → total 272px from right margin.
  setPanelLayout(wifiButtonRect, screenW - kMargin - 272, kMargin + 4, 136, 28);

  if (shouldUseLandscapePanels()) {
    const int16_t panelW = (screenW - 2 * kMargin - kPanelGap) / 2;
    const int16_t rowH =
        (screenH - 2 * kMargin - kHeaderHeight - kFooterHeight - kRowGap) / 2;
    const int16_t row1Y = kMargin + kHeaderHeight;
    const int16_t row2Y = row1Y + rowH + kRowGap;

    setPanelLayout(claudeSessionPanel, kMargin, row1Y, panelW, rowH);
    setPanelLayout(claudeWeeklyPanel, kMargin + panelW + kPanelGap, row1Y, panelW, rowH);
    setPanelLayout(codexSessionPanel, kMargin, row2Y, panelW, rowH);
    setPanelLayout(codexWeeklyPanel, kMargin + panelW + kPanelGap, row2Y, panelW, rowH);
  } else {
    // Portrait: 4 rows stacked
    const int16_t panelW = screenW - 2 * kMargin;
    const int16_t rowH =
        (screenH - 2 * kMargin - kHeaderHeight - kFooterHeight - 3 * kRowGap) / 4;
    const int16_t row1Y = kMargin + kHeaderHeight;

    setPanelLayout(claudeSessionPanel, kMargin, row1Y, panelW, rowH);
    setPanelLayout(claudeWeeklyPanel, kMargin, row1Y + rowH + kRowGap, panelW, rowH);
    setPanelLayout(codexSessionPanel, kMargin, row1Y + 2 * (rowH + kRowGap), panelW, rowH);
    setPanelLayout(codexWeeklyPanel, kMargin, row1Y + 3 * (rowH + kRowGap), panelW, rowH);
  }
}

void renderStaticChrome() {
  gfx->fillScreen(kColorBackground);
  drawText(kMargin, kMargin + 10, "Usage Monitor", kColorText, 2);
  drawUsagePanelFrame(claudeSessionPanel.x, claudeSessionPanel.y, claudeSessionPanel.w,
                      claudeSessionPanel.h, "Claude Session", kColorClaude);
  drawUsagePanelFrame(claudeWeeklyPanel.x, claudeWeeklyPanel.y, claudeWeeklyPanel.w,
                      claudeWeeklyPanel.h, "Claude Weekly", kColorClaude);
  drawUsagePanelFrame(codexSessionPanel.x, codexSessionPanel.y, codexSessionPanel.w,
                      codexSessionPanel.h, "Codex Session", kColorCodex);
  drawUsagePanelFrame(codexWeeklyPanel.x, codexWeeklyPanel.y, codexWeeklyPanel.w,
                      codexWeeklyPanel.h, "Codex Weekly", kColorCodex);
}

void renderHeaderDynamic() {
  const int16_t screenW = gfx->width();
  // Clear from just before the WiFi button to the right screen edge.
  clearRect(wifiButtonRect.x - 2, wifiButtonRect.y - 2,
            screenW - (wifiButtonRect.x - 2), wifiButtonRect.h + 4, kColorBackground);
  drawWifiButton(wifiButtonRect.x, wifiButtonRect.y, wifiButtonRect.w, wifiButtonRect.h);
  drawStatusPill(screenW - kMargin - 128, kMargin + 4, 128, 28);
}

void renderDynamicValues() {
  drawUsagePanelValues(claudeSessionPanel, claudeUsage.sessionPct, claudeUsage.sessionResetAt,
                       false);
  drawUsagePanelValues(claudeWeeklyPanel, claudeUsage.weeklyPct, claudeUsage.weeklyResetAt, true,
                       claudeUsage.weeklyBudgetPct);
  drawUsagePanelValues(codexSessionPanel, codexUsage.sessionPct, codexUsage.sessionResetAt, false);
  drawUsagePanelValues(codexWeeklyPanel, codexUsage.weeklyPct, codexUsage.weeklyResetAt, true,
                       codexUsage.weeklyBudgetPct);
}

void renderDashboard(bool fullRender) {
  if (fullRender) {
    computePanelLayout();
    renderStaticChrome();
  }

  renderHeaderDynamic();
  renderDynamicValues();
}

void drawStartupFrame() {
  gfx->fillScreen(kColorBackground);
  drawCenteredText(gfx->width() / 2, gfx->height() / 2 - 24, "Usage Monitor", kColorText, 3);
  drawCenteredText(gfx->width() / 2, gfx->height() / 2 + 20, "Starting...", kColorDim, 2);
}

int intFieldValue(JsonVariantConst field, int fallback) {
  if (field.isNull()) {
    return fallback;
  }
  if (field["integerValue"].is<const char *>()) {
    return atoi(field["integerValue"]);
  }
  if (field["doubleValue"].is<float>()) {
    return static_cast<int>(roundf(field["doubleValue"].as<float>()));
  }
  return fallback;
}

bool boolFieldValue(JsonVariantConst field, bool fallback) {
  if (field.isNull()) {
    return fallback;
  }
  if (field["booleanValue"].is<bool>()) {
    return field["booleanValue"].as<bool>();
  }
  return fallback;
}

const char *stringFieldValue(JsonVariantConst field, const char *fallback) {
  if (field.isNull()) {
    return fallback;
  }
  if (field["stringValue"].is<const char *>()) {
    return field["stringValue"];
  }
  if (field["timestampValue"].is<const char *>()) {
    return field["timestampValue"];
  }
  return fallback;
}

// Convert a parsed ISO date/time (treated as UTC, ignoring timezone offset) to a
// pseudo-epoch in seconds.  Because both weeklyResetAt and updatedAt carry the same
// offset, the offset cancels when we take the difference, so we can safely ignore it.
long isoToPseudoEpoch(int y, int m, int d, int h, int mn, int s) {
  static const int kDaysBeforeMonth[] = {0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334};
  const bool leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
  const int prevY = y - 1;
  const long leapDays = prevY / 4 - prevY / 100 + prevY / 400 -
                        (1969 / 4 - 1969 / 100 + 1969 / 400);
  long days = (long)(y - 1970) * 365 + leapDays + kDaysBeforeMonth[m - 1] + d - 1;
  if (m > 2 && leap) days++;
  return days * 86400L + (long)h * 3600L + (long)mn * 60L + s;
}

// Compute what % of the 7-day window has elapsed, from the two ISO strings already
// stored in the payload (both written by the same host, same timezone offset).
int computeWeeklyBudgetPct(const char *weeklyResetAt, const char *updatedAt) {
  if (!weeklyResetAt || weeklyResetAt[0] == '\0' || !updatedAt || updatedAt[0] == '\0') return 0;
  int ry, rm, rd, rh, rmn, rs, uy, um, ud, uh, umn, us;
  if (sscanf(weeklyResetAt, "%d-%d-%dT%d:%d:%d", &ry, &rm, &rd, &rh, &rmn, &rs) != 6) return 0;
  if (sscanf(updatedAt,     "%d-%d-%dT%d:%d:%d", &uy, &um, &ud, &uh, &umn, &us) != 6) return 0;
  constexpr long kWeeklySecs = 604800L;
  const long secsRemaining = isoToPseudoEpoch(ry, rm, rd, rh, rmn, rs) -
                             isoToPseudoEpoch(uy, um, ud, uh, umn, us);
  if (secsRemaining <= 0) return 100;
  const long secsElapsed = kWeeklySecs - secsRemaining;
  return (int)constrain(secsElapsed * 100L / kWeeklySecs, 0L, 100L);
}

bool parseFirestorePayload(const String &body, UsagePayload &outPayload) {
  JsonDocument doc;
  const DeserializationError error = deserializeJson(doc, body);
  if (error) {
    Serial.printf("Failed to parse Firestore JSON: %s\n", error.c_str());
    return false;
  }

  JsonVariantConst fields = doc["fields"];
  if (fields.isNull()) {
    Serial.println("Firestore document missing fields");
    return false;
  }

  const char *sessionResetAt = stringFieldValue(fields["sessionResetAt"], "");
  const char *weeklyResetAt = stringFieldValue(fields["weeklyResetAt"], "");

  outPayload.sessionPct = intFieldValue(fields["sessionPct"], 0);
  outPayload.weeklyPct = intFieldValue(fields["weeklyPct"], 0);
  outPayload.ok = boolFieldValue(fields["ok"], false);
  strlcpy(outPayload.status, stringFieldValue(fields["status"], "unknown"),
          sizeof(outPayload.status));
  strlcpy(outPayload.updatedAt, stringFieldValue(fields["updatedAt"], ""),
          sizeof(outPayload.updatedAt));

  if (sessionResetAt[0] != '\0') {
    strlcpy(outPayload.sessionResetAt, sessionResetAt, sizeof(outPayload.sessionResetAt));
  } else {
    const int legacyMins = intFieldValue(fields["sessionResetMins"], -1);
    if (legacyMins >= 0) {
      snprintf(outPayload.sessionResetAt, sizeof(outPayload.sessionResetAt), "%dm", legacyMins);
    } else {
      outPayload.sessionResetAt[0] = '\0';
    }
  }

  if (weeklyResetAt[0] != '\0') {
    strlcpy(outPayload.weeklyResetAt, weeklyResetAt, sizeof(outPayload.weeklyResetAt));
  } else {
    const int legacyMins = intFieldValue(fields["weeklyResetMins"], -1);
    if (legacyMins >= 0) {
      snprintf(outPayload.weeklyResetAt, sizeof(outPayload.weeklyResetAt), "%dm", legacyMins);
    } else {
      outPayload.weeklyResetAt[0] = '\0';
    }
  }

  outPayload.weeklyBudgetPct = computeWeeklyBudgetPct(outPayload.weeklyResetAt, outPayload.updatedAt);
  outPayload.valid = true;
  outPayload.receivedAtMs = millis();
  return true;
}

int median3(int a, int b, int c) {
  if ((a <= b && b <= c) || (c <= b && b <= a)) {
    return b;
  }
  if ((b <= a && a <= c) || (c <= a && a <= b)) {
    return a;
  }
  return c;
}

bool readTouchRaw(int &rawX, int &rawY) {
  if (digitalRead(kTouchIrqPin) != LOW) {
    return false;
  }

  const auto readAxis = [](uint8_t command) -> int {
    touchSpi.beginTransaction(SPISettings(2500000, MSBFIRST, SPI_MODE0));
    digitalWrite(kTouchCsPin, LOW);
    touchSpi.transfer(command);
    const int value = touchSpi.transfer16(0x00) >> 3;
    digitalWrite(kTouchCsPin, HIGH);
    touchSpi.endTransaction();
    return value & 0x0FFF;
  };

  const int z1 = readAxis(0xB0);
  const int z2 = readAxis(0xC0);
  if ((z1 + (4095 - z2)) < kTouchPressureMin) {
    return false;
  }

  const int x1 = readAxis(0xD0);
  const int x2 = readAxis(0xD0);
  const int x3 = readAxis(0xD0);
  const int y1 = readAxis(0x90);
  const int y2 = readAxis(0x90);
  const int y3 = readAxis(0x90);

  rawX = median3(x1, x2, x3);
  rawY = median3(y1, y2, y3);
  return true;
}

bool pointInRect(int16_t x, int16_t y, const PanelLayout &rect) {
  return x >= rect.x && x < (rect.x + rect.w) && y >= rect.y && y < (rect.y + rect.h);
}

bool touchHitsWifiButton(int rawX, int rawY) {
  const int16_t width = gfx->width();
  const int16_t height = gfx->height();
  const int clampedX = constrain(rawX, kTouchRawMin, kTouchRawMax);
  const int clampedY = constrain(rawY, kTouchRawMin, kTouchRawMax);
  const int16_t normX = map(clampedX, kTouchRawMin, kTouchRawMax, 0, width - 1);
  const int16_t normY = map(clampedY, kTouchRawMin, kTouchRawMax, 0, height - 1);

  const int16_t candidates[4][2] = {
      {normX, normY},
      {static_cast<int16_t>(width - 1 - normY), normX},
      {static_cast<int16_t>(width - 1 - normX), static_cast<int16_t>(height - 1 - normY)},
      {normY, static_cast<int16_t>(height - 1 - normX)},
  };

  for (const auto &candidate : candidates) {
    if (pointInRect(candidate[0], candidate[1], wifiButtonRect)) {
      return true;
    }
  }
  return false;
}

void updateUsageError(UsagePayload &target, const char *status) {
  const bool changed = !target.valid || target.ok ||
                       (strncmp(target.status, status, sizeof(target.status)) != 0);
  target.ok = false;
  target.valid = true;
  strlcpy(target.status, status, sizeof(target.status));
  target.receivedAtMs = millis();
  if (changed) {
    needsValueRender = true;
  }
}

void selectInitialWifiProfile() {
  if (wifiConfigured()) {
    return;
  }

  if (profileConfigured(kOfficeWifiProfile)) {
    activeWifiProfileIndex = 1;
  }
}

void switchWifiProfile() {
  if (!alternateWifiConfigured()) {
    return;
  }

  activeWifiProfileIndex = (activeWifiProfileIndex + 1) % 2;
  Serial.printf("Switching Wi-Fi profile to %s (%s)\n", activeWifiProfile().label,
                activeWifiProfile().ssid);
  lastClaudeFirestorePollMs = 0;
  lastCodexFirestorePollMs = 0;
  WiFi.disconnect(true, false);
  beginWifiConnection();
  needsFullRender = true;
}

void handleTouchInput() {
  int rawX = 0;
  int rawY = 0;
  const bool pressed = readTouchRaw(rawX, rawY);
  const unsigned long now = millis();

  if (pressed && !touchWasPressed && now - lastTouchToggleMs > kTouchToggleCooldownMs &&
      touchHitsWifiButton(rawX, rawY)) {
    lastTouchToggleMs = now;
    switchWifiProfile();
  }

  touchWasPressed = pressed;
}

void pollFirestoreDoc(const char *documentPath, UsagePayload &target,
                      unsigned long &lastPollMs) {
  if (WiFi.status() != WL_CONNECTED || !firestoreConfigured()) {
    return;
  }

  const unsigned long now = millis();
  if (lastPollMs != 0 &&
      now - lastPollMs < monitor_config::kFirestorePollIntervalMs) {
    return;
  }
  lastPollMs = now;

  WiFiClientSecure client;
  client.setInsecure();

  HTTPClient http;
  http.setTimeout(monitor_config::kHttpTimeoutMs);

  String url = "https://firestore.googleapis.com/v1/projects/";
  url += monitor_config::kFirestoreProjectId;
  url += "/databases/(default)/documents/";
  url += documentPath;
  if (strlen(monitor_config::kFirestoreApiKey) > 0) {
    url += "?key=";
    url += monitor_config::kFirestoreApiKey;
  }

  if (!http.begin(client, url)) {
    Serial.printf("Failed to begin Firestore request for %s\n", documentPath);
    updateUsageError(target, "http_begin_failed");
    return;
  }

  Serial.printf("Polling Firestore: %s\n", documentPath);
  const int statusCode = http.GET();
  Serial.printf("Firestore response for %s: %d\n", documentPath, statusCode);

  if (statusCode == 200) {
    const String responseBody = http.getString();
    Serial.println("Firestore response body:");
    Serial.println(responseBody);

    UsagePayload incoming;
    if (parseFirestorePayload(responseBody, incoming)) {
      const bool changed =
          !target.valid || target.sessionPct != incoming.sessionPct ||
          target.weeklyPct != incoming.weeklyPct || target.ok != incoming.ok ||
          target.weeklyBudgetPct != incoming.weeklyBudgetPct ||
          strncmp(target.sessionResetAt, incoming.sessionResetAt,
                  sizeof(target.sessionResetAt)) != 0 ||
          strncmp(target.weeklyResetAt, incoming.weeklyResetAt,
                  sizeof(target.weeklyResetAt)) != 0 ||
          strncmp(target.status, incoming.status, sizeof(target.status)) != 0;
      if (changed) {
        Serial.printf(
            "Usage payload updated: ok=%s status=%s session=%d%% weekly=%d%% updatedAt=%s\n",
            incoming.ok ? "true" : "false", incoming.status, incoming.sessionPct,
            incoming.weeklyPct, incoming.updatedAt[0] != '\0' ? incoming.updatedAt : "--");
      }
      target = incoming;
      if (changed) {
        needsValueRender = true;
      }
    } else {
      updateUsageError(target, "invalid_document");
    }
  } else if (statusCode == 404) {
    updateUsageError(target, "document_missing");
  } else if (statusCode == 401 || statusCode == 403) {
    updateUsageError(target, "rules_or_auth");
  } else if (statusCode > 0) {
    updateUsageError(target, "firestore_error");
  } else {
    updateUsageError(target, "network_error");
  }

  http.end();
}

void beginWifiConnection() {
  if (!wifiConfigured()) {
    wifiState = WifiState::MissingConfig;
    return;
  }

  wifiState = WifiState::Connecting;
  lastWifiAttemptMs = millis();
  wifiAttemptStartedMs = lastWifiAttemptMs;

  WiFi.mode(WIFI_STA);
  WiFi.setHostname("usage-monitor");
  WiFi.begin(activeWifiProfile().ssid, activeWifiProfile().password);
  Serial.printf("Connecting to Wi-Fi profile '%s' on SSID '%s'...\n", activeWifiProfile().label,
                activeWifiProfile().ssid);
}

void maintainWifi() {
  if (!wifiConfigured()) {
    wifiState = WifiState::MissingConfig;
    return;
  }

  if (WiFi.status() == WL_CONNECTED) {
    if (wifiState != WifiState::Connected) {
      Serial.printf("Wi-Fi connected: %s\n", WiFi.localIP().toString().c_str());
      needsValueRender = true;
    }
    wifiState = WifiState::Connected;
    return;
  }

  if (wifiState != WifiState::Connecting) {
    beginWifiConnection();
    needsValueRender = true;
    return;
  }

  const unsigned long now = millis();
  const bool timedOut = now - wifiAttemptStartedMs > monitor_config::kWifiConnectTimeoutMs;
  const bool retryDue = now - lastWifiAttemptMs > monitor_config::kWifiRetryIntervalMs;
  if (timedOut || retryDue) {
    WiFi.disconnect();
    beginWifiConnection();
    needsValueRender = true;
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("Booting usage monitor...");

  pinMode(kBacklightPin, OUTPUT);
  digitalWrite(kBacklightPin, HIGH);

  gfx->begin();
  drawStartupFrame();

  touchSpi.begin(14, 12, 13, kTouchCsPin);
  pinMode(kTouchCsPin, OUTPUT);
  digitalWrite(kTouchCsPin, HIGH);
  pinMode(kTouchIrqPin, INPUT);

  selectInitialWifiProfile();
  beginWifiConnection();
}

void loop() {
  handleTouchInput();
  maintainWifi();
  pollFirestoreDoc(monitor_config::kFirestoreDocumentPath, claudeUsage,
                   lastClaudeFirestorePollMs);
  pollFirestoreDoc(monitor_config::kCodexDocumentPath, codexUsage, lastCodexFirestorePollMs);

  if (needsFullRender) {
    renderDashboard(true);
    needsFullRender = false;
    needsValueRender = false;
  } else if (needsValueRender) {
    renderDashboard(false);
    needsValueRender = false;
  }

  delay(10);
}
