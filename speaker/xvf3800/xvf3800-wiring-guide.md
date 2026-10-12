# XVF3800 I2S Wiring Guide

## Raspberry Pi CM5 (Geekworm X1500) ↔ Seeed XVF3800

The XVF3800 is running **I2S Slave** firmware, so the Pi is the **I2S Master**.

**Firmware:** XVF3800 I2S *slave* firmware (Pi provides BCLK/LRCLK).

---

## Required Connections (Minimum for Audio Capture)

| Wire Color | Pi Physical Pin | Pi GPIO | Signal | XVF3800 Pad | Direction |
|------------|----------------:|--------:|--------|-------------|-----------|
| 🔴 **Red** | Pin 2 or 4 | — | **5V** | **5V** | Power → XVF |
| ⚫ **Black** | Pin 6 (any GND) | — | **GND** | **GND** | Common ground |
| 🟡 **Yellow** | **Pin 12** | **GPIO18** | **BCLK** | **X0D37** | Pi → XVF |
| 🟢 **Green** | **Pin 35** | **GPIO19** | **LRCLK/FS** | **X0D38** | Pi → XVF |
| 🔵 **Blue** | **Pin 38** | **GPIO20** | **DOUT** | **X1D00** | XVF → Pi (audio) |
| 🟣 **Purple** *(optional)* | Pin 40 | GPIO21 | DIN | X1D01 | Pi → XVF |

---

## Complete XVF3800 Pin Reference

### GPIO Pins (optional control via I2C)

| XVF3800 Pad | Type | Function |
|-------------|------|----------|
| X1D09 | Input | Mute button status (high = released) |
| X1D13 | Input | Floating |
| X1D34 | Input | Floating |
| X0D11 | Output | Floating |
| X0D30 | Output | Mute LED + mic mute (high = mute) |
| X0D31 | Output | Amplifier enable (low = enabled) |
| X0D33 | Output | WS2812 LED power (high = on) |
| X0D39 | Output | Floating |

### I2C Pins (for control/configuration)

| XVF3800 Pad | Signal |
|-------------|--------|
| X0D14 | I2C SCL |
| X0D15 | I2C SDA |

---

## Pi 40-Pin Header (Relevant Pins)

| Wire | Pi Pin | Pi GPIO | Signal | Notes |
|------|-------:|--------:|--------|-------|
| 🔴 Red | 2 or 4 | — | 5V | Power to XVF3800 |
| ⚫ Black | 6 | — | GND | Ground |
| 🟡 Yellow | 12 | GPIO18 | BCLK | Bit clock (Pi → XVF) |
| 🟢 Green | 35 | GPIO19 | LRCLK | Frame sync (Pi → XVF) |
| 🔵 Blue | 38 | GPIO20 | DOUT | Audio data to Pi |
| 🟣 Purple | 40 | GPIO21 | DIN | Optional audio to XVF |

---

## XVF3800 I2S Header (from board silkscreen)

| XVF3800 Pad | Wire | Signal | Notes |
|-------------|------|--------|-------|
| 5V | 🔴 Red | 5V | Power in |
| GND | ⚫ Black | GND | Ground |
| X0D37 | 🟡 Yellow | BCLK | Bit clock (from Pi) |
| X0D38 | 🟢 Green | LRCLK | Frame sync (from Pi) |
| X1D00 | 🔵 Blue | DOUT | Audio data to Pi |
| X1D01 | 🟣 Purple | DIN | Optional audio to XVF |
| X1D13 | — | — | Unused |
| X1D11 | — | — | Unused |

---

## Signal Descriptions

| Signal | Description |
|--------|-------------|
| **5V** | Power supply for XVF3800 (USB doesn't work after I2S firmware) |
| **GND** | Common ground reference (essential!) |
| **BCLK** | Bit clock - drives data timing (48kHz × 32bits × 2ch = 3.072 MHz) |
| **LRCLK/FS** | Left/Right clock (frame sync) - 48kHz |
| **DOUT** | Audio data output from XVF3800 to Pi (the mic audio!) |
| **DIN** | Audio data input to XVF3800 from Pi (optional, for playback) |

---

## Verification Commands

After wiring, run:
```bash
# Check if ALSA card appears
arecord -l

# Validate full setup
sudo /ai/speaker/xvf3800/xvf3800_boot_overlay_install.sh validate

# Test capture (should work once wired)
arecord -D hw:0,0 -c 2 -r 48000 -f S32_LE -d 5 /tmp/test.wav
aplay /tmp/test.wav  # Play back on HDMI or other output

# Check GPIO pin states
raspi-gpio get 18-21
```

---

## Troubleshooting

| Issue | Check |
|-------|-------|
| No ALSA card | Reboot required after overlay install |
| I/O error on arecord | Wires not connected / XVF not powered |
| Silent audio | DOUT (blue) not connected to X1D00 |
| `clk_i2s enable_count=0` | BCLK/LRCLK not connected or Pi not clocking I2S |
| XVF LEDs flash then off | Power issue - try USB power instead |

---

## Safe Mode Recovery

If XVF3800 firmware is corrupted:

1. Power off completely
2. Press and hold **Mute button**
3. While holding, reconnect power via USB to PC
4. Red LED blinks = Safe Mode
5. Flash firmware with `dfu-util`

---

*Updated: January 15, 2026*

---

## Playback With JAB5

For full-duplex playback + capture wiring (XVF3800 + JAB5) and DAC+ Lite options, see:
`/ai/speaker/xvf3800/xvf3800-jab5-playback.md`

DAC+ Lite overlay installer:
`/ai/speaker/xvf3800/xvf3800_dacplus_duplex_overlay_install.sh`
