# XVF3800 + DAC+ Lite + JAB5 Playback

This guide assumes:
- XVF3800 is running **I2S slave** firmware.
- Raspberry Pi CM5 is the **I2S clock master**.
- Capture already works on GPIO18/19/20.
- Playback path is: **CM5 -> HiFiBerry DAC+ Lite (analog out) -> JAB5 analog input -> speakers**.

The JAB5 amplifier will be driven from the **HiFiBerry DAC+ Lite analog output**. The XVF3800 does **not** provide analog line-out. It exposes **I2S DOUT** (mic data) and **I2S DIN** (optional playback input for AEC), but no DAC/line-level output.

---

## 1) Wiring Plan (Explicit)

### A) Pi CM5 <-> XVF3800 (Capture)
This is the existing capture wiring (repeat here for completeness):

| Wire Color | Pi Pin | Pi GPIO | Signal | XVF3800 Pad | Direction |
|------------|-------:|--------:|--------|-------------|-----------|
| 🔴 Red | 2 or 4 | - | 5V | 5V | Pi -> XVF |
| ⚫ Black | 6 | - | GND | GND | Common |
| 🟡 Yellow | 12 | GPIO18 | BCLK | X0D37 | Pi -> XVF |
| 🟢 Green | 35 | GPIO19 | LRCLK/FS | X0D38 | Pi -> XVF |
| 🔵 Blue | 38 | GPIO20 | I2S DIN (Pi) | X1D00 (DOUT) | XVF -> Pi |
| 🟣 Purple | 40 | GPIO21 | I2S DOUT (Pi) | X1D01 (DIN) | Pi -> XVF (optional, for AEC) |

### B) CM5 <-> HiFiBerry DAC+ Lite (I2S)
Mount the DAC+ Lite on the Pi header (or wire it off-board). It uses the same I2S pins as XVF3800:

| Wire Color | Pi Pin | Pi GPIO | Signal | DAC+ Lite Pin | Direction |
|------------|-------:|--------:|--------|---------------|-----------|
| 🟡 Yellow | 12 | GPIO18 | BCLK | BCLK | Pi -> DAC |
| 🟢 Green | 35 | GPIO19 | LRCLK/WS | LRCLK | Pi -> DAC |
| 🟣 Purple | 40 | GPIO21 | I2S DOUT (Pi) | DIN | Pi -> DAC |
| ⚫ Black | 6 | - | GND | GND | Common |

Notes:
- The DAC+ Lite sits on the same I2S bus as XVF3800 capture; both share GPIO18/19/21.
- If you also want XVF3800 AEC, split GPIO21 to **XVF3800 DIN** as well.

### B1) DAC+ Lite Header Map (40-Pin Pass-Through)
The DAC+ Lite is a HAT that passes the Pi's 40-pin header straight through. If the DAC headers are unlabeled, wire by **Pi physical pin numbers**:

| Pi Physical Pin | Signal | Use |
|----------------:|--------|-----|
| 2 or 4 | 5V | DAC+ Lite power (main supply) |
| 1 | 3.3V | DAC+ Lite 3.3V (I2C pullups/EEPROM) |
| 6 | GND | Ground |
| 3 | GPIO2 (SDA) | I2C1 SDA (codec control) |
| 5 | GPIO3 (SCL) | I2C1 SCL (codec control) |
| 12 | GPIO18 | I2S BCLK |
| 35 | GPIO19 | I2S LRCLK/WS |
| 40 | GPIO21 | I2S DOUT (Pi TX) |

If you're mounting the DAC+ Lite on a breadboard, bring these pins out to the breadboard rails and fan out from there.

### B2) Breadboard Fan-Out (Temporary)
If you only have single jumper leads, use the breadboard as a splitter:

1) Run short jumpers from the Pi to the breadboard rows for **BCLK (Pin 12)**, **LRCLK (Pin 35)**, **DOUT (Pin 40)**, **GND (Pin 6)**, **5V (Pin 2/4)**, and **3.3V (Pin 1)**.
2) From those same rows, run a second set of jumpers to the XVF3800.
3) From the I2S rows, run another set of jumpers to the DAC+ Lite header pins.
4) Keep all I2S wires short (<10 cm) and twisted/paired with GND when possible.

Mini breadboard layout (Elegoo 170) example:

```
Top rail (Row A-E)            Bottom rail (Row F-J)

Row 1:  BCLK  (Pin 12)  🟡 o o o o | o o o o 🟡  -> XVF BCLK + DAC BCLK
Row 2:  LRCLK (Pin 35)  🟢 o o o o | o o o o 🟢  -> XVF LRCLK + DAC LRCLK
Row 3:  DOUT  (Pin 40)  🟣 o o o o | o o o o 🟣  -> XVF DIN + DAC DIN
Row 4:  GND   (Pin 6)   ⚫ o o o o | o o o o ⚫  -> XVF GND + DAC GND
Row 5:  5V    (Pin 2/4) 🔴 o o o o | o o o o 🔴  -> DAC 5V (and XVF 5V if needed)
Row 6:  3.3V  (Pin 1)   ⚪ o o o o | o o o o ⚪  -> DAC 3.3V (I2C pullups/EEPROM)

Pi jumpers land on the left side of each row.
Fan-out jumpers (XVF/DAC) land on the right side of the same row.
```

### C) DAC+ Lite analog out -> JAB5 analog in (screw terminals)
Use the DAC+ Lite **line-out** to feed the JAB5 **line-in** screw terminals (often labeled `LIN`, `RIN`, `GND`).

| Wire Color | DAC+ Lite Line-Out | JAB5 Line-In Terminal |
|------------|--------------------|-----------------------|
| 🔴 Red | R | RIN |
| ⚪ White | L | LIN |
| ⚫ Black | GND | GND |

### D) JAB5 Speaker Outputs
Wire speakers directly to the JAB5 outputs:

| Wire Color | JAB5 Output | Speaker Terminal |
|------------|-------------|------------------|
| ⚪ White | L+ | Left speaker + |
| ⚫ Black | L- | Left speaker - |
| ⚪ White | R+ | Right speaker + |
| ⚫ Black | R- | Right speaker - |

Caution:
- JAB5 outputs are typically **BTL (bridge-tied load)**. **Do not connect either speaker terminal to ground**.
- Do not share ground between speaker negatives.

---

## 2) Power Considerations

- **XVF3800**: 5V from the Pi header is OK (already working).
- **JAB5**: Use a **separate power supply** (do **not** power from the Pi 5V rail).
  - Typical JAB5 boards expect **12-24V DC**. Size the PSU to your speakers (often **3-6A** depending on load and volume).
  - **Common ground is required**: connect JAB5 GND to Pi GND.
- Power up order is not critical, but avoiding brownouts helps stability.

---

## 3) HiFiBerry DAC+ Lite Notes (Current Path)

Important constraints:
- Do **not** enable `dtoverlay=hifiberry-dacplus` separately; the DAC codec is embedded in the custom overlay used below.
- Disable any prior XVF3800 capture-only overlay before installing the DAC+ Lite overlay.

---

## 4) Device Tree Overlay (Capture + DAC Playback)

This path needs **two audio links** (XVF3800 capture + DAC+ Lite playback). Use the dedicated installer script:

```bash
sudo I2S_CPU_DAI=i2s_clk_producer I2S_MASTER=cpu \
  /ai/speaker/xvf3800/xvf3800_dacplus_duplex_overlay_install.sh install

sudo reboot
```

Post-reboot validation:

```bash
sudo /ai/speaker/xvf3800/xvf3800_dacplus_duplex_overlay_install.sh validate
```

Notes:
- If you previously installed `xvf3800_boot_overlay_install.sh`, comment it out in `config.txt` or run its `remove` command before installing the DAC+ Lite overlay.
- Do not enable `dtoverlay=hifiberry-dacplus` separately; the new overlay embeds the PCM512x codec node.

---

## 5) ALSA Playback Routing (DAC Output)

### Direct (recommended for testing)
Find the card + device numbers:

```bash
aplay -l
arecord -l
```

Then play to the **DAC playback device**:

```bash
aplay -D hw:<card>,<playback_device> /usr/share/sounds/alsa/Front_Center.wav
```

### Optional default device
If you want the DAC card as the default ALSA output, add:

```text
# /etc/asound.conf
pcm.!default {
  type hw
  card <card>
  device <playback_device>
}

ctl.!default {
  type hw
  card <card>
}
```

Replace `<card>` with the number shown by `aplay -l`.

---

## 6) Test Plan (Ordered)

1) **Power**
   - Power the JAB5 from its dedicated PSU.
   - Confirm Pi/XVF3800 are powered and capture still works.

2) **Check ALSA**
   ```bash
   aplay -l
   arecord -l
   ```
   Verify the `XVF3800-DACplus` card appears. Note the playback and capture device numbers.

3) **Playback Smoke Test**
   ```bash
   aplay -D hw:<card>,<playback_device> /usr/share/sounds/alsa/Front_Center.wav
   ```
   Replace `<card>` and `<playback_device>` with values from `aplay -l`.

4) **Capture While Playing (Full-Duplex)**
   ```bash
   arecord -D hw:<card>,<capture_device> -c 2 -r 48000 -f S32_LE -d 5 /tmp/jab5_test.wav
   aplay -D hw:<card>,<playback_device> /tmp/jab5_test.wav
   ```
   Use the DAC playback device for `aplay` and the capture device from `arecord -l`.

5) **Physical Verification**
   - You should hear the test audio from the speakers.
   - If you have a multimeter, you can confirm JAB5 supply voltage under load.

---

## 7) Troubleshooting

- **No sound**: Verify JAB5 power, speaker wiring, and that DAC line-out is wired to JAB5 line-in.
- **Playback only works on HDMI**: Ensure `aplay` targets the I2S card, not the default.
- **Noise/garbled audio**: Double-check BCLK/LRCLK wiring and that Pi is I2S master.
- **AEC not working**: Ensure GPIO21 also feeds XVF3800 DIN if AEC is required.
