# XVF3800 New Board Setup Gameplan

**Date:** January 15, 2026
**Status:** Pi software ready ✅ — Waiting for new XVF3800 board

---

## 🎯 Goal

Make the Raspberry Pi CM5 (Geekworm X1500) capture audio from the Seeed ReSpeaker XVF3800 via I2S.

**Configuration:**
- XVF3800 = **I2S Master** (provides BCLK/LRCLK clocks)
- Raspberry Pi = **I2S Slave** (receives clocks from XVF3800)
- Firmware: `respeaker_xvf3800_i2s_master_dfu_firmware_v1.0.x_48k.bin`

---

## ✅ Pi Software Status (COMPLETE)

| Component | Status | Details |
|-----------|--------|---------|
| Device Tree Overlay | ✅ Installed | `/boot/firmware/overlays/xvf3800-i2s-capture.dtbo` |
| config.txt | ✅ Updated | `dtoverlay=xvf3800-i2s-capture` |
| ALSA Card | ✅ Visible | `card 0: XVF3800I2SCaptu [XVF3800-I2S-Capture]` |
| GPIO Mux | ✅ Configured | GPIO 18-21 → `alt4 (i2s1)` |
| rp1_i2s0 | ✅ Disabled | Prevents pin conflict |
| rp1_i2s1 | ✅ Enabled | Using GPIO 18-21 |

**No additional Pi software setup needed.**

---

## 📌 Complete XVF3800 I2S Pinout (from Seeed Wiki)

### I2S Header Pins (for audio)

| XVF3800 Pad | Signal | Direction | Connect To |
|-------------|--------|-----------|------------|
| **5V** | Power | Input | Pi Pin 2 or 4 (5V) |
| **GND** | Ground | Common | Pi Pin 6 (GND) |
| **X0D37** | BCLK (Bit Clock) | XVF → Pi | Pi GPIO18 (Pin 12) |
| **X0D38** | LRCLK/FS (Frame Sync) | XVF → Pi | Pi GPIO19 (Pin 35) |
| **X1D00** | DOUT (Audio Out) | XVF → Pi | Pi GPIO20 (Pin 38) |
| **X1D01** | DIN (Audio In) | Pi → XVF | Pi GPIO21 (Pin 40) — *optional* |

### GPIO Pins (optional control)

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

## 🔌 Required Wiring (Minimum for Audio Capture)

```
XVF3800                           Raspberry Pi CM5
┌──────────┐                      ┌──────────────────────┐
│  5V      │◄───── 🔴 Red ───────►│ Pin 2 or 4 (5V)      │
│  GND     │◄───── ⚫ Black ──────►│ Pin 6 (GND)          │
│  X0D37   │────── 🟡 Yellow ────►│ Pin 12 (GPIO18 BCLK) │
│  X0D38   │────── 🟢 Green ─────►│ Pin 35 (GPIO19 LRCLK)│
│  X1D00   │────── 🔵 Blue ──────►│ Pin 38 (GPIO20 DOUT) │
│  X1D01   │◄───── 🟣 Purple ─────│ Pin 40 (GPIO21 DIN)  │ *optional*
└──────────┘                      └──────────────────────┘
```

### Pi 40-Pin Header Reference

```
                        Pi 40-Pin Header (top view)
    ┌─────────────────────────────────────────────────────────────┐
    │                                                             │
    │   3V3  (1)  ●  ●  (2)  5V    ◄── 🔴 Red                     │
    │  GPIO2 (3)  ●  ●  (4)  5V                                   │
    │  GPIO3 (5)  ●  ●  (6)  GND   ◄── ⚫ Black                    │
    │  GPIO4 (7)  ●  ●  (8)  GPIO14                               │
    │   GND  (9)  ●  ●  (10) GPIO15                               │
    │ GPIO17 (11) ●  ●  (12) GPIO18 ◄── 🟡 Yellow (BCLK)          │
    │ GPIO27 (13) ●  ●  (14) GND                                  │
    │ GPIO22 (15) ●  ●  (16) GPIO23                               │
    │   3V3  (17) ●  ●  (18) GPIO24                               │
    │ GPIO10 (19) ●  ●  (20) GND                                  │
    │  GPIO9 (21) ●  ●  (22) GPIO25                               │
    │ GPIO11 (23) ●  ●  (24) GPIO8                                │
    │   GND  (25) ●  ●  (26) GPIO7                                │
    │  GPIO0 (27) ●  ●  (28) GPIO1                                │
    │  GPIO5 (29) ●  ●  (30) GND                                  │
    │  GPIO6 (31) ●  ●  (32) GPIO12                               │
    │ GPIO13 (33) ●  ●  (34) GND                                  │
    │ GPIO19 (35) ●  ●  (36) GPIO16 ◄── 🟢 Green (LRCLK) on 35    │
    │ GPIO26 (37) ●  ●  (38) GPIO20 ◄── 🔵 Blue (DOUT)            │
    │   GND  (39) ●  ●  (40) GPIO21 ◄── 🟣 Purple (DIN) optional  │
    │                                                             │
    └─────────────────────────────────────────────────────────────┘
```

### XVF3800 I2S Header (from board silkscreen)

```
    XVF3800 I2S Header
    ┌──────────────┐
    │     5V       │ ◄── 🔴 Red (Power)
    │     5V       │
    │     GND      │ ◄── ⚫ Black (Ground)
    │    X1D13     │     (unused)
    │    X0D37     │ ◄── 🟡 Yellow (BCLK out)
    │    X0D38     │ ◄── 🟢 Green (LRCLK out)
    │     GND      │
    │    X1D00     │ ◄── 🔵 Blue (DOUT - audio data to Pi)
    │    X1D01     │ ◄── 🟣 Purple (DIN - audio from Pi, optional)
    │    X1D11     │     (unused)
    └──────────────┘
```

---

## 🔧 Step-by-Step Setup for New Board

### Step 1: Flash Firmware (on PC first)

⚠️ **Flash BEFORE connecting to Pi** — USB DFU doesn't work after I2S firmware is loaded.

1. Download firmware from [ReSpeaker XVF3800 GitHub](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY)
2. Connect XVF3800 to PC via USB-C (use the XMOS port near 3.5mm jack)
3. Flash the I2S master firmware:

```bash
# Linux/macOS
sudo dfu-util -R -e -a 1 -D respeaker_xvf3800_i2s_master_dfu_firmware_v1.0.x_48k.bin

# Windows (in Command Prompt)
dfu-util -R -e -a 1 -D respeaker_xvf3800_i2s_master_dfu_firmware_v1.0.x_48k.bin
```

4. Verify: Red LED should flash during DFU, then board restarts

### Step 2: Wire to Pi

1. **Disconnect Pi power**
2. Wire as shown above (5 wires minimum: 5V, GND, BCLK, LRCLK, DOUT)
3. Double-check connections before powering on

### Step 3: Power On & Verify

1. Power on Pi (USB-C or barrel jack to X1500)
2. XVF3800 should power from Pi's 5V header
3. Check XVF3800 LEDs:
   - ✅ **LEDs stay on/breathing** = Board is running
   - ❌ **LEDs flash once then off** = Power issue (see troubleshooting)

### Step 4: Validate Audio Capture

```bash
# Run validation script
sudo /ai/speaker/xvf3800/xvf3800_boot_overlay_install.sh validate

# Or manually test:
arecord -D hw:0,0 -c 2 -r 48000 -f S32_LE -d 5 /tmp/test.wav

# Check for actual audio (not just zeros)
hexdump -C /tmp/test.wav | head -n 30
```

---

## 🩺 Troubleshooting

### XVF3800 LEDs flash once then turn off

**Symptoms:** LEDs briefly illuminate then go dark
**Cause:** Insufficient power or board not booting

**Solutions:**
1. Try powering via USB instead of 5V header (connect USB while I2S wires attached)
2. Check Pi's 5V rail: `vcgencmd measure_volts` — should be close to 5.0V
3. Use shorter/thicker wires for 5V/GND
4. Try a different 5V source (USB charger via XVF's USB-C port)

### arecord: I/O error

**Symptoms:** ALSA card visible, but arecord fails immediately
**Cause:** No I2S clocks reaching Pi

**Check:**
```bash
# Check if clocks are running
cat /sys/kernel/debug/clk/clk_summary | grep -A2 clk_i2s

# Should see enable_count > 0 when XVF is providing clocks
```

**Solutions:**
1. Verify BCLK (X0D37 → GPIO18) and LRCLK (X0D38 → GPIO19) wires
2. Verify XVF3800 is actually running (LEDs on)
3. Check GPIO states: `raspi-gpio get 18-20`

### No ALSA card visible

**Cause:** Overlay not loaded or wrong overlay

**Check:**
```bash
# Verify overlay is in config.txt
grep xvf3800 /boot/firmware/config.txt

# Should show:
# dtoverlay=xvf3800-i2s-capture
```

**Solution:** Re-run installer and reboot:
```bash
sudo /ai/speaker/xvf3800/xvf3800_boot_overlay_install.sh install
sudo reboot
```

### Silent audio (WAV file is all zeros)

**Cause:** DOUT wire not connected or wrong pin

**Check:**
- Blue wire connects X1D00 to GPIO20 (Pin 38)
- Try speaking near XVF3800 mics during recording

---

## 🔄 Recovery: Enter Safe Mode

If XVF3800 firmware is corrupted or unresponsive:

1. **Power off** completely (disconnect all power)
2. **Press and hold** the **Mute button**
3. **While holding Mute**, reconnect power (USB to PC)
4. **Red LED blinks** = Safe Mode entered
5. Flash firmware via USB DFU from PC

---

## 📋 Firmware Options

| Firmware | Channels | Output |
|----------|----------|--------|
| `respeaker_xvf3800_i2s_dfu_firmware_v1.0.x.bin` | 2 | Ch0: Conference, Ch1: ASR |
| `respeaker_xvf3800_i2s_master_dfu_firmware_v1.0.x_48k.bin` | 2 | Ch0: ASR, Ch1: Wake word |

**Current choice:** `respeaker_xvf3800_i2s_master_dfu_firmware_v1.0.x_48k.bin`
- 48kHz sample rate
- XVF3800 provides I2S clocks (master mode)
- Channel 0: ASR-optimized audio (beamforming, noise reduction)
- Channel 1: Wake word detection stream

---

## 📝 Quick Reference Commands

```bash
# Check ALSA cards
arecord -l

# Test capture (2 seconds)
arecord -D hw:0,0 -c 2 -r 48000 -f S32_LE -d 2 /tmp/test.wav

# Full validation
sudo /ai/speaker/xvf3800/xvf3800_boot_overlay_install.sh validate

# Check overlay status
sudo /ai/speaker/xvf3800/xvf3800_boot_overlay_install.sh status

# Check GPIO pin functions
raspi-gpio get 18-21

# Check device tree
ls /proc/device-tree/xvf3800-i2s-capture/

# Check dmesg for I2S/ASoC messages
dmesg | grep -iE 'i2s|asoc|simple.*card'
```

---

## ✅ Checklist for New Board

- [ ] Flash I2S master firmware from PC (before connecting to Pi)
- [ ] Verify LEDs stay on after USB disconnect
- [ ] Wire 5V, GND, BCLK, LRCLK, DOUT to Pi
- [ ] Power on Pi, verify XVF3800 LEDs are active
- [ ] Run `sudo /ai/speaker/xvf3800/xvf3800_boot_overlay_install.sh validate`
- [ ] Verify arecord captures audio data (not zeros)
- [ ] Test with actual speech and playback

---

*Gameplan created January 15, 2026*
