/*
 * Firmware for Lilygo T-Encoder Pro AI Agent Monitor
 *
 * Features:
 *  - SH8601 QSPI AMOLED 390x390 Round Display with Status UI
 *  - Dual Transports: USB Serial + BLE UART Service
 *  - Rotary Knob & Push Button Input -> Dispatch events back to Host
 */

#include <Arduino.h>
#include <ArduinoJson.h>
#include <NimBLEDevice.h>
#include <U8g2lib.h>
#include <driver/gpio.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include "Arduino_GFX_Library.h"
#include "pin_config.h"

// ---------- Display Setup ----------
Arduino_DataBus *bus = new Arduino_ESP32QSPI(
    SCREEN_CS, SCREEN_SCLK, SCREEN_SDIO0,
    SCREEN_SDIO1, SCREEN_SDIO2, SCREEN_SDIO3);

Arduino_GFX *display = new Arduino_SH8601(bus, SCREEN_RST, 0, false, SCREEN_WIDTH, SCREEN_HEIGHT);
Arduino_Canvas *gfx = new Arduino_Canvas(SCREEN_WIDTH, SCREEN_HEIGHT, display);

// ---------- Agent Display State ----------
String current_agent_name = "Monitor";
String current_state      = "IDLE";
String current_message    = "Waiting for connection...";
String current_phase      = "";
uint16_t current_led_color = 0xFFFF;
bool   unread_flag        = false;
int    agents_count       = 0;

// Display dirty flag - only redraw when state changes
bool   display_dirty      = true;
String prev_agent_name    = "";
String prev_state         = "";
String prev_message       = "";

// ---------- Agent Switch Animation ----------
struct DisplaySnapshot {
    String agentName;
    String state;
    String message;
    String phase;
    int agentCount;
};

int8_t pending_switch_direction = 0;
int8_t switch_animation_direction = 0;
bool switch_animation_pending = false;
DisplaySnapshot switch_from;

// ---------- Breathing Animation ----------
uint8_t animation_phase = 0;
unsigned long last_animation = 0;
const unsigned long ANIMATION_INTERVAL_MS = 75;

// ---------- Rotary Encoder ----------
volatile int16_t encoder_edge_delta = 0;
volatile uint8_t encoder_isr_state = 0;
portMUX_TYPE encoder_mux = portMUX_INITIALIZER_UNLOCKED;
DRAM_ATTR int8_t encoder_transition_table[16] = {
     0, -1,  1,  0,
     1,  0,  0, -1,
    -1,  0,  0,  1,
     0,  1, -1,  0
};
bool          button_pressed  = false;
unsigned long last_btn_check  = 0;
unsigned long button_down_at  = 0;
bool          long_press_sent = false;

// ---------- Waiting Action Menu ----------
const uint8_t MAX_ACTIONS = 6;
const unsigned long LONG_PRESS_MS = 800;
const unsigned long ACTION_PRESS_GUARD_MS = 250;
const unsigned long ACTION_MENU_TIMEOUT_MS = 15000;
bool action_menu_available = false;
bool action_menu_open = false;
bool action_menu_selection_dirty = false;
String interaction_request_id = "";
String interaction_detail = "";
String interaction_tool_name = "";
String action_ids[MAX_ACTIONS];
String action_labels[MAX_ACTIONS];
bool action_dangerous[MAX_ACTIONS];
uint8_t action_count = 0;
uint8_t selected_action = 0;
unsigned long last_menu_activity = 0;
unsigned long last_action_rotation = 0;

void closeActionMenu();

// ---------- BLE UART ----------
#define SERVICE_UUID           "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define CHARACTERISTIC_UUID_RX "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
#define CHARACTERISTIC_UUID_TX "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

BLEServer         *pServer           = NULL;
BLECharacteristic *pTxCharacteristic = NULL;
bool               bleConnected      = false;
bool               serialConnected   = false;
String             ble_rx_buffer     = "";
QueueHandle_t       ble_rx_queue      = NULL;
String             serial_rx_buffer  = "";
bool               serial_rx_overflow = false;
const size_t        SERIAL_RX_BUFFER_BYTES = 4096;

// ---------- Color Helpers ----------

// Convert 8-bit RGB to 16-bit RGB565
#define TO565(r, g, b) ((((r) & 0xF8) << 8) | (((g) & 0xFC) << 3) | ((b) >> 3))

// Get 16-bit display color for state
uint16_t getStateColor(const String &state) {
    if (state == "THINKING")         return TO565(163, 204, 218);
    if (state == "COMPLETED_UNREAD") return TO565(189, 227, 195);
    if (state == "WAITING_APPROVAL") return TO565(248, 247, 186);
    if (state == "ERROR")            return TO565(245, 210, 210);
    return TO565(255, 253, 246);
}

uint16_t getDisplayColor(const String &state, const String &phase) {
    if (
        phase == "new_approval"
        || phase == "awaiting_input"
        || phase == "approval_selected"
        || phase == "approved_running"
    ) {
        return TO565(255, 180, 84);
    }
    if (phase == "approval_rejected")  return TO565(242, 167, 167);
    return getStateColor(state);
}

uint16_t blend565(uint16_t background, uint16_t foreground, uint8_t alpha) {
    uint16_t inverse = 255 - alpha;
    uint8_t bgR = ((background >> 11) & 0x1F) << 3;
    uint8_t bgG = ((background >> 5) & 0x3F) << 2;
    uint8_t bgB = (background & 0x1F) << 3;
    uint8_t fgR = ((foreground >> 11) & 0x1F) << 3;
    uint8_t fgG = ((foreground >> 5) & 0x3F) << 2;
    uint8_t fgB = (foreground & 0x1F) << 3;

    return TO565(
        (bgR * inverse + fgR * alpha) / 255,
        (bgG * inverse + fgG * alpha) / 255,
        (bgB * inverse + fgB * alpha) / 255
    );
}

uint8_t breathingIntensity(uint8_t phase) {
    // Smooth 0..127..0 triangle into a rounded pulse without floating point.
    uint16_t x = phase < 128 ? phase : 255 - phase;
    uint32_t smooth = (uint32_t)x * x * (381 - 2 * x);
    // Keep the peak alpha below 45% so the AMOLED edge remains soft.
    return 24 + (smooth * 88UL) / (127UL * 127UL * 127UL);
}

void drawEdgeGlow(uint16_t stateColor, uint8_t phase) {
    const uint16_t bgColor = TO565(8, 8, 16);
    const uint8_t glowWidth = 36;
    const int outerRadius = SCREEN_WIDTH / 2 - 1;
    uint8_t pulse = breathingIntensity(phase);

    // One-pixel concentric rings provide a radial alpha gradient without
    // storing or decoding a full-screen transparent bitmap.
    for (uint8_t depth = 0; depth < glowWidth; depth++) {
        uint16_t falloff = glowWidth - depth;
        uint8_t alpha = (uint32_t)pulse * falloff * falloff
                      / (glowWidth * glowWidth);
        gfx->drawCircle(
            SCREEN_WIDTH / 2,
            SCREEN_HEIGHT / 2,
            outerRadius - depth,
            blend565(bgColor, stateColor, alpha)
        );
    }
}

// Get state display label
String getStateLabel(const String &state, const String &phase) {
    if (
        phase == "new_approval"
        || phase == "awaiting_input"
        || phase == "approval_selected"
        || phase == "approved_running"
    ) {
        return "WAITING";
    }
    if (phase == "approval_rejected")  return "REJECTED";
    if (state == "THINKING")         return "THINKING";
    if (state == "COMPLETED_UNREAD") return "COMPLETED";
    if (state == "WAITING_APPROVAL") return "WAITING";
    if (state == "ERROR")            return "ERROR";
    return "IDLE";
}

// Get RGB565 from hex string "#RRGGBB"
uint16_t hexToRGB565(const char *hex) {
    if (!hex || strlen(hex) < 7) return 0xFFFF;
    long n = strtol(hex + 1, NULL, 16);
    return TO565((n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF);
}

// ---------- Display Rendering ----------

void drawCenteredCurrentFontOffset(const char *text, int y, uint16_t color, int xOffset) {
    int16_t boundsX, boundsY;
    uint16_t boundsW, boundsH;
    gfx->getTextBounds(text, 0, 0, &boundsX, &boundsY, &boundsW, &boundsH);
    int x = (SCREEN_WIDTH - boundsW) / 2 - boundsX + xOffset;

    gfx->setTextColor(color);
    gfx->setCursor(x, y - boundsY);
    gfx->print(text);
}

// Draw UTF-8 text centered horizontally at the given top coordinate.
void drawCenteredTextOffset(const char *text, int y, uint8_t textSize, uint16_t color, int xOffset) {
    gfx->setFont(u8g2_font_unifont_t_chinese3);
    gfx->setTextSize(textSize);
    drawCenteredCurrentFontOffset(text, y, color, xOffset);
}

void drawCenteredText(const char *text, int y, uint8_t textSize, uint16_t color) {
    drawCenteredTextOffset(text, y, textSize, color, 0);
}

uint16_t currentTextWidth(const String &text) {
    int16_t boundsX, boundsY;
    uint16_t boundsW, boundsH;
    gfx->getTextBounds(text, 0, 0, &boundsX, &boundsY, &boundsW, &boundsH);
    return boundsW;
}

// Fit UTF-8 text without cutting through a multi-byte Chinese character.
String fitTextToWidth(const String &text, uint16_t maxWidth) {
    if (currentTextWidth(text) <= maxWidth) return text;

    String fitted;
    int byteIndex = 0;
    while (byteIndex < (int)text.length()) {
        uint8_t lead = (uint8_t)text[byteIndex];
        int charBytes = 1;
        if ((lead & 0xE0) == 0xC0) charBytes = 2;
        else if ((lead & 0xF0) == 0xE0) charBytes = 3;
        else if ((lead & 0xF8) == 0xF0) charBytes = 4;

        String candidate = fitted + text.substring(byteIndex, byteIndex + charBytes) + "...";
        if (currentTextWidth(candidate) > maxWidth) break;
        fitted += text.substring(byteIndex, byteIndex + charBytes);
        byteIndex += charBytes;
    }
    return fitted + "...";
}

int utf8CharBytes(const String &text, int byteIndex) {
    uint8_t lead = (uint8_t)text[byteIndex];
    if ((lead & 0xE0) == 0xC0) return 2;
    if ((lead & 0xF0) == 0xE0) return 3;
    if ((lead & 0xF8) == 0xF0) return 4;
    return 1;
}

String agentTitle(const String &name) {
    String title = name;
    if (title.endsWith(" Agent")) {
        title.remove(title.length() - 6);
    } else if (title.equalsIgnoreCase("Agent")) {
        title = "Monitor";
    }
    return title;
}

void drawAgentTitle(const String &name, int y, int xOffset, uint16_t color) {
    gfx->setFont(u8g2_font_helvB18_tf);
    gfx->setTextSize(1);
    String title = fitTextToWidth(agentTitle(name), SCREEN_WIDTH - 60);
    drawCenteredCurrentFontOffset(title.c_str(), y, color, xOffset);
}

void drawStatusRow(const String &label, int cx, int centerY, uint16_t color) {
    gfx->setFont(
        label.length() > 15
            ? u8g2_font_helvB12_tf
            : u8g2_font_helvB18_tf
    );
    gfx->setTextSize(1);

    int16_t boundsX, boundsY;
    uint16_t boundsW, boundsH;
    gfx->getTextBounds(label, 0, 0, &boundsX, &boundsY, &boundsW, &boundsH);

    gfx->setTextColor(color);
    gfx->setCursor(
        cx - (int)boundsW / 2 - boundsX,
        // Align the center of the glyph pixels, rather than the font baseline,
        // with the requested row center.
        centerY - boundsY - ((int)boundsH - 1) / 2
    );
    gfx->print(label);
}

void drawThickLine(
    int x1,
    int y1,
    int x2,
    int y2,
    uint16_t color,
    uint8_t thickness = 3
) {
    int8_t half = thickness / 2;
    for (int8_t offset = -half; offset <= half; offset++) {
        gfx->drawLine(x1 + offset, y1, x2 + offset, y2, color);
    }
}

void drawStatusIcon(
    const String &state,
    const String &displayPhase,
    int centerX,
    int centerY,
    uint16_t color,
    uint8_t phase
) {
    if (displayPhase == "new_approval") {
        // A strong alert glyph distinguishes an actionable approval from
        // passive "awaiting input".
        gfx->drawCircle(centerX, centerY, 16, color);
        gfx->drawCircle(centerX, centerY, 15, color);
        drawThickLine(centerX, centerY - 9, centerX, centerY + 3, color, 4);
        gfx->fillCircle(centerX, centerY + 10, 2, color);
        return;
    }

    if (
        displayPhase == "awaiting_input"
        || displayPhase == "approval_selected"
        || displayPhase == "approved_running"
    ) {
        // One stable waiting glyph after an approval is selected. Internal
        // phases remain available to Host logs without making the UI jump.
        gfx->drawCircle(centerX, centerY, 15, color);
        gfx->drawCircle(centerX, centerY, 14, color);
        gfx->fillRect(centerX - 10, centerY - 20, 8, 4, color);
        gfx->fillRect(centerX + 2, centerY - 20, 8, 4, color);
        drawThickLine(centerX - 12, centerY - 17, centerX - 17, centerY - 12, color, 2);
        drawThickLine(centerX + 12, centerY - 17, centerX + 17, centerY - 12, color, 2);
        drawThickLine(centerX, centerY, centerX, centerY - 9, color, 3);
        drawThickLine(centerX, centerY, centerX + 7, centerY + 4, color, 3);
        gfx->fillCircle(centerX, centerY, 2, color);
        return;
    }

    if (state == "THINKING") {
        static const int8_t jump[12] = {0, -2, -5, -8, -10, -8, -5, -2, 0, 0, 0, 0};
        uint8_t tick = phase / 4;
        for (uint8_t dot = 0; dot < 3; dot++) {
            uint8_t local = (tick + 32 - dot * 8) % 32;
            int8_t yOffset = local < 12 ? jump[local] : 0;
            gfx->fillCircle(centerX - 18 + dot * 18, centerY + yOffset, 4, color);
        }
        return;
    }

    if (state == "WAITING_APPROVAL") {
        // Clock face with a long minute hand and a shorter hour hand.
        gfx->drawCircle(centerX, centerY, 15, color);
        gfx->drawCircle(centerX, centerY, 14, color);
        drawThickLine(centerX, centerY, centerX, centerY - 10, color, 3);
        drawThickLine(centerX, centerY, centerX + 7, centerY + 4, color, 3);
        gfx->fillCircle(centerX, centerY, 2, color);
        return;
    }

    if (state == "ERROR") {
        drawThickLine(centerX - 10, centerY - 10, centerX + 10, centerY + 10, color, 4);
        drawThickLine(centerX + 10, centerY - 10, centerX - 10, centerY + 10, color, 4);
        return;
    }

    if (state == "COMPLETED_UNREAD") {
        drawThickLine(centerX - 12, centerY, centerX - 3, centerY + 9, color, 4);
        drawThickLine(centerX - 3, centerY + 9, centerX + 14, centerY - 10, color, 4);
        return;
    }

    // IDLE
    gfx->fillRoundRect(centerX - 14, centerY - 2, 28, 5, 2, color);
}

void drawConnectionStatus(
    const char *label,
    int dotCenterX,
    int centerY,
    uint16_t dotColor,
    uint16_t textColor
) {
    gfx->setFont(u8g2_font_helvB08_tf);
    gfx->setTextSize(1);

    int16_t boundsX, boundsY;
    uint16_t boundsW, boundsH;
    gfx->getTextBounds(label, 0, 0, &boundsX, &boundsY, &boundsW, &boundsH);

    const int dotRadius = 3;
    const int gap = 7;
    gfx->fillCircle(dotCenterX, centerY, dotRadius, dotColor);
    gfx->setTextColor(textColor);
    gfx->setCursor(
        dotCenterX + dotRadius + gap - boundsX,
        centerY - boundsY - ((int)boundsH - 1) / 2
    );
    gfx->print(label);
}

void drawCommandText(const String &message, int top, int xOffset, uint16_t color) {
    const uint8_t maxLines = 3;
    const uint16_t maxWidth = SCREEN_WIDTH - 70;
    String lines[maxLines];
    int byteIndex = 0;

    gfx->setFont(u8g2_font_unifont_t_chinese3);
    gfx->setTextSize(1);

    for (uint8_t line = 0; line < maxLines && byteIndex < (int)message.length(); line++) {
        while (byteIndex < (int)message.length()) {
            int charBytes = utf8CharBytes(message, byteIndex);
            String nextChar = message.substring(byteIndex, byteIndex + charBytes);
            if (nextChar == "\n") {
                byteIndex += charBytes;
                break;
            }

            String candidate = lines[line] + nextChar;
            String measured = candidate;
            if (line == maxLines - 1 && byteIndex + charBytes < (int)message.length()) {
                measured += "...";
            }
            if (currentTextWidth(measured) > maxWidth) break;

            lines[line] = candidate;
            byteIndex += charBytes;
        }
    }

    if (byteIndex < (int)message.length()) {
        lines[maxLines - 1] = fitTextToWidth(lines[maxLines - 1] + "...", maxWidth);
    }

    uint8_t lineCount = 0;
    while (lineCount < maxLines && lines[lineCount].length() > 0) lineCount++;
    int blockTop = top + ((maxLines - lineCount) * 9);
    for (uint8_t line = 0; line < lineCount; line++) {
        drawCenteredTextOffset(lines[line].c_str(), blockTop + line * 18, 1, color, xOffset);
    }
}

DisplaySnapshot currentSnapshot() {
    return {
        current_agent_name,
        current_state,
        current_message,
        current_phase,
        agents_count
    };
}

void drawDisplayScene(const DisplaySnapshot &snapshot, int xOffset, uint8_t phase) {
    uint16_t stateColor = getDisplayColor(snapshot.state, snapshot.phase);
    uint16_t dimColor   = TO565(100, 100, 110);   // Dim gray for secondary text
    uint16_t darkAccent = TO565(20, 20, 35);      // Slightly lighter for inner circle

    int cx = SCREEN_WIDTH / 2 + xOffset;   // 195 when centered
    int cy = 175;

    // === Status Ring ===
    // Outer ring (state color)
    gfx->fillCircle(cx, cy, 100, stateColor);
    // Inner fill (dark)
    gfx->fillCircle(cx, cy, 85, darkAccent);

    // === Agent Name (above ring) ===
    drawAgentTitle(snapshot.agentName, 34, xOffset, WHITE);

    // === State label and icon ===
    String label = getStateLabel(snapshot.state, snapshot.phase);
    drawStatusRow(label, cx, cy - 18, stateColor);
    drawStatusIcon(snapshot.state, snapshot.phase, cx, cy + 32, stateColor, phase);

    // === Message (below ring) ===
    drawCommandText(snapshot.message, 282, xOffset, WHITE);

    // === Connection indicators (bottom) ===
    const int connectionY = 346;
    const int onlineY = 356;
    // Serial indicator
    uint16_t serColor = serialConnected ? TO565(0, 200, 80) : TO565(80, 80, 80);
    drawConnectionStatus("USB", cx - 40, connectionY, serColor, dimColor);

    // BLE indicator
    uint16_t bleColor = bleConnected ? TO565(0, 136, 255) : TO565(80, 80, 80);
    drawConnectionStatus("BLE", cx + 8, connectionY, bleColor, dimColor);

    // Agent count
    char countBuf[24];
    snprintf(countBuf, sizeof(countBuf), "%d online", snapshot.agentCount);
    gfx->setFont(u8g2_font_helvB08_tf);
    gfx->setTextSize(1);
    drawCenteredCurrentFontOffset(countBuf, onlineY, dimColor, xOffset);
}

void drawActionMenu() {
    const uint16_t bgColor = TO565(8, 8, 16);
    const uint16_t dimColor = TO565(105, 105, 118);
    const uint16_t panelColor = TO565(18, 18, 30);
    const uint16_t panelBorderColor = TO565(48, 48, 64);
    gfx->fillScreen(bgColor);

    drawCenteredText("APPROVE", 12, 1, WHITE);
    drawCenteredText(agentTitle(current_agent_name).c_str(), 34, 1, dimColor);

    // The approval protocol carries a longer detail field than the normal
    // status message, so this page can show the actual command being approved.
    const int panelX = 25;
    const int panelY = 55;
    const int panelWidth = SCREEN_WIDTH - 50;
    const int panelHeight = 150;
    gfx->fillRoundRect(panelX, panelY, panelWidth, panelHeight, 12, panelColor);
    gfx->drawRoundRect(panelX, panelY, panelWidth, panelHeight, 12, panelBorderColor);

    gfx->setFont(u8g2_font_helvB08_tf);
    gfx->setTextSize(1);
    String detailLabel = interaction_tool_name.length() > 0
        ? "COMMAND - " + interaction_tool_name
        : "COMMAND";
    detailLabel = fitTextToWidth(detailLabel, panelWidth - 28);
    gfx->setTextColor(TO565(255, 180, 84));
    gfx->setCursor(panelX + 14, panelY + 20);
    gfx->print(detailLabel);

    String detail = interaction_detail.length() > 0
        ? interaction_detail
        : current_message;
    const uint8_t maxLines = 7;
    const uint16_t maxWidth = panelWidth - 28;
    String lines[maxLines];
    int byteIndex = 0;
    gfx->setFont(u8g2_font_unifont_t_chinese3);
    gfx->setTextSize(1);
    for (
        uint8_t line = 0;
        line < maxLines && byteIndex < (int)detail.length();
        line++
    ) {
        while (byteIndex < (int)detail.length()) {
            int charBytes = utf8CharBytes(detail, byteIndex);
            String nextChar = detail.substring(
                byteIndex,
                byteIndex + charBytes
            );
            if (nextChar == "\n") {
                byteIndex += charBytes;
                break;
            }
            String measured = lines[line] + nextChar;
            if (
                line == maxLines - 1
                && byteIndex + charBytes < (int)detail.length()
            ) {
                measured += "...";
            }
            if (currentTextWidth(measured) > maxWidth) break;
            lines[line] += nextChar;
            byteIndex += charBytes;
        }
    }
    if (byteIndex < (int)detail.length()) {
        lines[maxLines - 1] = fitTextToWidth(
            lines[maxLines - 1] + "...",
            maxWidth
        );
    }
    for (uint8_t line = 0; line < maxLines; line++) {
        if (lines[line].length() == 0) break;
        gfx->setTextColor(WHITE);
        gfx->setCursor(panelX + 14, panelY + 43 + line * 15);
        gfx->print(lines[line]);
    }
}

void drawActionMenuRows() {
    const uint16_t bgColor = TO565(8, 8, 16);
    const uint16_t dimColor = TO565(105, 105, 118);
    const uint16_t selectedColor = TO565(248, 247, 186);
    const uint16_t dangerColor = TO565(255, 50, 50);
    const uint8_t visibleRows = 3;
    const int actionAreaTop = 211;
    const int actionAreaHeight = 111;
    const int firstRowY = 214;
    const int rowSpacing = 35;

    gfx->fillRect(0, actionAreaTop, SCREEN_WIDTH, actionAreaHeight, bgColor);
    uint8_t first = 0;
    if (action_count > visibleRows && selected_action >= visibleRows - 1) {
        first = selected_action - (visibleRows - 2);
        if (first + visibleRows > action_count) first = action_count - visibleRows;
    }
    uint8_t rows = min((uint8_t)(action_count - first), visibleRows);
    for (uint8_t row = 0; row < rows; row++) {
        uint8_t index = first + row;
        int y = firstRowY + row * rowSpacing;
        bool selected = index == selected_action;
        uint16_t accent = action_dangerous[index] ? dangerColor : selectedColor;

        if (selected) {
            gfx->fillRoundRect(55, y, SCREEN_WIDTH - 110, 31, 11, TO565(28, 28, 42));
            gfx->drawRoundRect(55, y, SCREEN_WIDTH - 110, 31, 11, accent);
        }

        gfx->setFont(u8g2_font_helvB12_tf);
        gfx->setTextSize(1);
        String label = fitTextToWidth(action_labels[index], SCREEN_WIDTH - 140);
        drawCenteredCurrentFontOffset(
            label.c_str(),
            y + 7,
            selected ? accent : (action_dangerous[index] ? TO565(180, 45, 45) : dimColor),
            0
        );
    }
}

void drawActionMenuFooter() {
    drawCenteredText("Press: Confirm", 329, 1, TO565(150, 150, 165));
    drawCenteredText("Hold: Return", 350, 1, TO565(100, 100, 115));
}

void renderActionMenuSelection() {
    const int actionAreaTop = 211;
    const int actionAreaHeight = 111;

    drawActionMenuRows();
    // The canvas has a full-width contiguous framebuffer. Sending only this
    // strip makes knob selection feedback much faster than a 390x390 flush.
    display->draw16bitRGBBitmap(
        0,
        actionAreaTop,
        gfx->getFramebuffer() + actionAreaTop * SCREEN_WIDTH,
        SCREEN_WIDTH,
        actionAreaHeight
    );
    action_menu_selection_dirty = false;
}

void renderDisplay() {
    prev_agent_name = current_agent_name;
    prev_state      = current_state;
    prev_message    = current_message;
    display_dirty   = false;

    gfx->fillScreen(TO565(8, 8, 16));
    if (action_menu_open) {
        drawActionMenu();
        drawActionMenuRows();
        drawActionMenuFooter();
    } else {
        drawEdgeGlow(getDisplayColor(current_state, current_phase), animation_phase);
        drawDisplayScene(currentSnapshot(), 0, animation_phase);
    }
    gfx->flush();
    action_menu_selection_dirty = false;
}

void animateAgentSwitch(const DisplaySnapshot &from, const DisplaySnapshot &to, int8_t direction) {
    const uint8_t frameCount = 12;
    const uint16_t bgColor = TO565(8, 8, 16);
    int8_t slideDirection = direction >= 0 ? 1 : -1;

    for (uint8_t frame = 1; frame <= frameCount; frame++) {
        float t = (float)frame / frameCount;
        // Smoothstep easing keeps the motion soft at both ends.
        float eased = t * t * (3.0f - 2.0f * t);
        int travel = (int)(SCREEN_WIDTH * eased);
        int oldOffset = -slideDirection * travel;
        int newOffset = slideDirection * (SCREEN_WIDTH - travel);

        gfx->fillScreen(bgColor);
        drawDisplayScene(from, oldOffset, animation_phase);
        drawDisplayScene(to, newOffset, animation_phase);
        gfx->flush();
        delay(8);
    }

    // Floating-point rounding can leave the last animated frame one pixel
    // short. Always replace it with an exact, clean destination frame. Reset
    // the shared animation phase so the new agent's glow starts at its lowest
    // opacity and fades in smoothly.
    animation_phase = 0;
    gfx->fillScreen(bgColor);
    drawEdgeGlow(getDisplayColor(to.state, to.phase), animation_phase);
    drawDisplayScene(to, 0, animation_phase);
    gfx->flush();

    prev_agent_name = current_agent_name;
    prev_state = current_state;
    prev_message = current_message;
    display_dirty = false;
    // Resume the edge glow and icon animations from a fresh interval.
    last_animation = millis();
}

// ---------- JSON Protocol ----------

void processIncomingJSON(const String &line) {
    StaticJsonDocument<4096> doc;
    DeserializationError err = deserializeJson(doc, line);
    if (err) {
        Serial.print("{\"event\":\"LOG\",\"msg\":\"JSON Parse failed: ");
        Serial.print(err.c_str());
        Serial.println("\"}");
        return;
    }

    const char *cmd = doc["cmd"];
    if (cmd && strcmp(cmd, "SET_STATE") == 0) {
        JsonObject active = doc["active"];
        if (!active.isNull()) {
            DisplaySnapshot oldSnapshot = currentSnapshot();
            int8_t switchDirection = pending_switch_direction;
            pending_switch_direction = 0;

            current_agent_name = active["display_name"] | "Agent";
            current_state      = active["state"]        | "IDLE";
            current_message    = active["message"]      | "";
            current_phase      = active["phase"]        | "";
            unread_flag        = active["unread"]       | false;
            agents_count       = doc["agents_count"]    | 0;

            const char *hex_color = active["color"] | "#FFFDF6";
            current_led_color = hexToRGB565(hex_color);

            JsonObject interaction = doc["interaction"];
            String incomingRequestId = interaction["request_id"] | "";
            bool sameInteraction = (
                incomingRequestId.length() > 0
                && incomingRequestId == interaction_request_id
            );
            action_count = 0;
            action_menu_available = false;

            // Return is a firmware-owned safe item and can never be replaced
            // or reordered by an agent-provided action list.
            if (!interaction.isNull() && incomingRequestId.length() > 0) {
                interaction_detail = String(
                    (const char *)(interaction["detail"] | "")
                );
                interaction_tool_name = String(
                    (const char *)(interaction["tool_name"] | "")
                );
                action_ids[0] = "return";
                action_labels[0] = "Return";
                action_dangerous[0] = false;
                action_count = 1;

                JsonArray actions = interaction["actions"];
                for (JsonObject action : actions) {
                    String actionId = action["id"] | "";
                    String label = action["label"] | "";
                    if (
                        action_count >= MAX_ACTIONS
                        || actionId.length() == 0
                        || label.length() == 0
                        || actionId == "return"
                    ) {
                        continue;
                    }
                    action_ids[action_count] = actionId;
                    action_labels[action_count] = label;
                    action_dangerous[action_count] = action["dangerous"] | false;
                    action_count++;
                }
                interaction_request_id = incomingRequestId;
                action_menu_available = action_count > 1;
            } else {
                interaction_request_id = "";
                interaction_detail = "";
                interaction_tool_name = "";
            }

            if (!sameInteraction || !action_menu_available) {
                action_menu_open = false;
                selected_action = 0;
            } else if (selected_action >= action_count) {
                selected_action = 0;
            }

            if (switchDirection != 0 && oldSnapshot.agentName != current_agent_name) {
                switch_from = oldSnapshot;
                switch_animation_direction = switchDirection;
                switch_animation_pending = true;
            }
            display_dirty = true;
        }
    }
}

// ---------- Communication: Send to Host ----------

void sendEventToHost(const String &jsonStr) {
    Serial.println(jsonStr);
    if (bleConnected && pTxCharacteristic) {
        String framed = jsonStr + "\n";
        // Keep notifications valid even before a larger BLE MTU is negotiated.
        for (int offset = 0; offset < (int)framed.length(); offset += 20) {
            int chunkLength = min(20, (int)framed.length() - offset);
            pTxCharacteristic->setValue(
                (uint8_t *)framed.c_str() + offset,
                chunkLength
            );
            pTxCharacteristic->notify();
        }
    }
}

// ---------- BLE Callbacks ----------

class ServerCB : public BLEServerCallbacks {
    void onConnect(BLEServer *s) override   { bleConnected = true;  display_dirty = true; }
    void onDisconnect(BLEServer *s) override {
        bleConnected = false;
        if (!serialConnected && action_menu_open) closeActionMenu();
        display_dirty = true;
        BLEDevice::startAdvertising();
    }
};

class RxCB : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic *c) override {
        std::string v = c->getValue();
        if (v.length() == 0) return;
        ble_rx_buffer += String(v.c_str());
        int newline;
        while ((newline = ble_rx_buffer.indexOf('\n')) >= 0) {
            String line = ble_rx_buffer.substring(0, newline);
            ble_rx_buffer.remove(0, newline + 1);
            line.trim();
            if (line.length() > 0 && ble_rx_queue) {
                // JSON parsing uses a 4 KB document. Defer it to loop() rather
                // than consuming that stack inside NimBLE's callback task.
                String *frame = new String(line);
                if (!frame || xQueueSend(ble_rx_queue, &frame, 0) != pdTRUE) {
                    delete frame;
                }
            }
        }
        if (ble_rx_buffer.length() > 2048) {
            ble_rx_buffer = "";
        }
    }
};

void initBLE() {
    ble_rx_queue = xQueueCreate(4, sizeof(String *));
    if (!ble_rx_queue) {
        Serial.println("{\"event\":\"LOG\",\"msg\":\"BLE RX queue allocation failed\"}");
    }
    BLEDevice::init("T-Encoder-Pro");
    pServer = BLEDevice::createServer();
    pServer->setCallbacks(new ServerCB());

    BLEService *svc = pServer->createService(SERVICE_UUID);
    pTxCharacteristic = svc->createCharacteristic(CHARACTERISTIC_UUID_TX, NIMBLE_PROPERTY::NOTIFY);

    BLECharacteristic *rx = svc->createCharacteristic(CHARACTERISTIC_UUID_RX,
                                NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
    rx->setCallbacks(new RxCB());

    svc->start();
    BLEAdvertising *adv = BLEDevice::getAdvertising();
    adv->addServiceUUID(SERVICE_UUID);
    adv->start();
}

// ---------- Rotary Encoder ----------

int16_t knob_steps = 0;

void IRAM_ATTR onEncoderEdge() {
    uint8_t state = (
        (gpio_get_level((gpio_num_t)KNOB_DATA_A) << 1)
        | gpio_get_level((gpio_num_t)KNOB_DATA_B)
    );

    portENTER_CRITICAL_ISR(&encoder_mux);
    uint8_t transition = (encoder_isr_state << 2) | state;
    encoder_edge_delta += encoder_transition_table[transition];
    encoder_isr_state = state;
    portEXIT_CRITICAL_ISR(&encoder_mux);
}

void closeActionMenu() {
    action_menu_open = false;
    action_menu_selection_dirty = false;
    selected_action = 0;
    display_dirty = true;
}

void sendSelectedAction() {
    if (
        !action_menu_open
        || selected_action >= action_count
        || (!serialConnected && !bleConnected)
        || millis() - last_action_rotation < ACTION_PRESS_GUARD_MS
    ) {
        return;
    }

    // Return is local navigation, not an approval decision. Preserve the
    // interaction so the next short press can reopen this same menu.
    if (action_ids[selected_action] == "return") {
        closeActionMenu();
        return;
    }

    StaticJsonDocument<256> doc;
    doc["event"] = "KNOB_ACTION";
    doc["request_id"] = interaction_request_id;
    doc["action_id"] = action_ids[selected_action];
    String event;
    serializeJson(doc, event);
    sendEventToHost(event);

    // Prevent duplicate approval submission locally. The host also validates
    // request_id and guarantees that at most one result is accepted.
    action_menu_available = false;
    closeActionMenu();
}

void handleShortPress() {
    if (action_menu_open) {
        last_menu_activity = millis();
        sendSelectedAction();
    } else if (action_menu_available && (serialConnected || bleConnected)) {
        action_menu_open = true;
        action_menu_selection_dirty = false;
        selected_action = 0;
        knob_steps = 0;
        last_menu_activity = millis();
        display_dirty = true;
    } else {
        sendEventToHost("{\"event\":\"KNOB_PRESS\"}");
    }
}

void scanEncoder() {
    int16_t capturedEdges;
    portENTER_CRITICAL(&encoder_mux);
    capturedEdges = encoder_edge_delta;
    encoder_edge_delta = 0;
    portEXIT_CRITICAL(&encoder_mux);
    knob_steps += capturedEdges;

    // Lilygo T-Encoder Pro usually has 2 state changes per physical click
    while (knob_steps >= 2) {
        if (action_menu_open) {
            selected_action = (selected_action + 1) % action_count;
            last_menu_activity = millis();
            last_action_rotation = millis();
            action_menu_selection_dirty = true;
        } else {
            pending_switch_direction = 1;
            sendEventToHost("{\"event\":\"KNOB_ROTATE\",\"dir\":1}");
        }
        knob_steps -= 2;
    }
    while (knob_steps <= -2) {
        if (action_menu_open) {
            selected_action = (
                selected_action == 0 ? action_count - 1 : selected_action - 1
            );
            last_menu_activity = millis();
            last_action_rotation = millis();
            action_menu_selection_dirty = true;
        } else {
            pending_switch_direction = -1;
            sendEventToHost("{\"event\":\"KNOB_ROTATE\",\"dir\":-1}");
        }
        knob_steps += 2;
    }

    if (millis() - last_btn_check > 20) {
        last_btn_check = millis();
        bool btn = (digitalRead(KNOB_BTN) == LOW);
        if (btn && !button_pressed) {
            button_pressed = true;
            button_down_at = millis();
            long_press_sent = false;
        } else if (btn && button_pressed && !long_press_sent) {
            if (millis() - button_down_at >= LONG_PRESS_MS) {
                long_press_sent = true;
                if (action_menu_open) closeActionMenu();
            }
        } else if (!btn && button_pressed) {
            button_pressed = false;
            if (!long_press_sent) handleShortPress();
        }
    }
}

// ---------- Serial Connection Detection ----------
unsigned long lastSerialRx = 0;

// ---------- Setup & Loop ----------

void setup() {
    // Waiting/action-menu frames are larger than the 256-byte USB CDC
    // default. Configure the queue before begin() so frames remain intact
    // even while an AMOLED redraw briefly blocks loop().
    Serial.setRxBufferSize(SERIAL_RX_BUFFER_BYTES);
    Serial.begin(115200);
    serial_rx_buffer.reserve(SERIAL_RX_BUFFER_BYTES);

    // GPIO setup
    pinMode(KNOB_DATA_A, INPUT_PULLUP);
    pinMode(KNOB_DATA_B, INPUT_PULLUP);
    pinMode(KNOB_BTN, INPUT_PULLUP);
    encoder_isr_state = (
        (gpio_get_level((gpio_num_t)KNOB_DATA_A) << 1)
        | gpio_get_level((gpio_num_t)KNOB_DATA_B)
    );
    attachInterrupt(digitalPinToInterrupt(KNOB_DATA_A), onEncoderEdge, CHANGE);
    attachInterrupt(digitalPinToInterrupt(KNOB_DATA_B), onEncoderEdge, CHANGE);
    pinMode(SCREEN_EN, OUTPUT);
    digitalWrite(SCREEN_EN, HIGH);

    // AMOLED Display Init
    display->begin(40000000);
    if (!gfx->begin(GFX_SKIP_OUTPUT_BEGIN)) {
        Serial.println("{\"event\":\"LOG\",\"msg\":\"Display canvas allocation failed\"}");
        while (true) delay(1000);
    }
    gfx->setUTF8Print(true);
    gfx->setTextWrap(false);
    gfx->fillScreen(BLACK);

    // Fade in brightness
    for (int i = 0; i <= 255; i++) {
        display->Display_Brightness(i);
        delay(2);
    }

    // Draw initial boot screen
    drawCenteredText("MONITOR", 155, 2, WHITE);
    drawCenteredText("Initializing...", 205, 1, TO565(100, 100, 120));
    gfx->flush();
    delay(800);

    // BLE Init
    initBLE();

    // Force first render
    display_dirty = true;
    renderDisplay();

    // Tell host we are ready to receive initial state
    sendEventToHost("{\"event\":\"READY\"}");
}

void loop() {
    // Parse complete BLE frames on the main Arduino task, where the large
    // ArduinoJson document cannot overflow NimBLE's callback stack.
    if (ble_rx_queue) {
        String *frame = NULL;
        while (xQueueReceive(ble_rx_queue, &frame, 0) == pdTRUE) {
            if (frame) {
                processIncomingJSON(*frame);
                delete frame;
            }
        }
    }

    // Assemble newline-delimited USB frames without blocking. The enlarged
    // CDC queue absorbs complete menu payloads while display rendering runs.
    while (Serial.available()) {
        int incoming = Serial.read();
        if (incoming < 0) break;
        char value = static_cast<char>(incoming);
        lastSerialRx = millis();

        if (value == '\n') {
            if (serial_rx_overflow) {
                Serial.println(
                    "{\"event\":\"LOG\",\"msg\":\"Serial frame exceeded 4096 bytes\"}"
                );
            } else {
                serial_rx_buffer.trim();
                if (serial_rx_buffer.length() > 0) {
                    bool connectionChanged = !serialConnected;
                    processIncomingJSON(serial_rx_buffer);
                    serialConnected = true;
                    if (connectionChanged) {
                        display_dirty = true;
                    }
                }
            }
            serial_rx_buffer = "";
            serial_rx_overflow = false;
        } else if (value != '\r' && !serial_rx_overflow) {
            if (serial_rx_buffer.length() < SERIAL_RX_BUFFER_BYTES - 1) {
                serial_rx_buffer += value;
            } else {
                serial_rx_buffer = "";
                serial_rx_overflow = true;
            }
        }
    }

    // Serial connection timeout (5s without data = disconnected)
    if (serialConnected && millis() - lastSerialRx > 5000) {
        serialConnected = false;
        if (!bleConnected && action_menu_open) closeActionMenu();
        display_dirty = true;
    }

    scanEncoder();

    if (
        action_menu_open
        && millis() - last_menu_activity >= ACTION_MENU_TIMEOUT_MS
    ) {
        closeActionMenu();
    }

    if (switch_animation_pending) {
        switch_animation_pending = false;
        animateAgentSwitch(switch_from, currentSnapshot(), switch_animation_direction);
        switch_animation_direction = 0;
    } else if (display_dirty) {
        renderDisplay();
    } else if (action_menu_open && action_menu_selection_dirty) {
        renderActionMenuSelection();
    } else if (
        !action_menu_open
        && millis() - last_animation >= ANIMATION_INTERVAL_MS
    ) {
        last_animation = millis();
        animation_phase += 4;
        renderDisplay();
    }

    delay(10);
}
