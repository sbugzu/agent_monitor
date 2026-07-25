/*
 * Hardware Pin definitions for Lilygo T-Encoder Pro (ESP32-S3)
 */
#pragma once

// AMOLED Display Pins (SH8601 QSPI DXQ120MYB2416A)
#define SCREEN_CS      10
#define SCREEN_SCLK    12
#define SCREEN_SDIO0   11
#define SCREEN_SDIO1   13
#define SCREEN_SDIO2   7
#define SCREEN_SDIO3   14
#define SCREEN_RST     4
#define SCREEN_EN      3
#define SCREEN_WIDTH   390
#define SCREEN_HEIGHT  390

// Touch Screen CHSC5816 (I2C)
#define IIC_SDA        5
#define IIC_SCL        6
#define TOUCH_RST      8
#define TOUCH_INT      9
#define CHSC5816_SLAVE_ADDRESS 0x2E

// Rotary Encoder & Button Pins
#define KNOB_DATA_A    1
#define KNOB_DATA_B    2
#define KNOB_BTN       0

// Buzzer
#define BUZZER_DATA    17
