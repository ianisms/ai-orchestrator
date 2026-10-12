# CM5 Carrier Board Design Guide

Design brief and ground truth for a custom Raspberry Pi Compute Module 5 (CM5) carrier board in KiCad. Current revision: v3.7.

- Key design:
    - Internal AC-DC PSU (Mean Well LRS-150-28) provides 28V DC to carrier board
    - HomePod-style captive AC cord (no user-facing connectors)
    - I want I2S to:
        - XVF3800 via 2x10 header connected via ribbon cable to 2x20 header pins on XVF3800, also provides power
        - JAB5 via JST PH 6-pos (2 mm pitch) header — matches JAB5 J6 I2S input connector
- I want to use quality parts
- Lean towards quality but also simple / fewer components

## Project Summary
I'm building an all-in-one CM5 carrier with an m.2 nvme slot.
The board should have:
- a GPIO header
- 2x10 pin header to connect a ribbon cable to an XVF3800 mic array https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/#pin-out
- 4 pin power connector to feed power the a **WONDOM/Sure JAB5 amplifier module** https://store.sure-electronics.com/product/756 (off-board, dual TDA7498E + ADAU1701 DSP, 4-channel Class D)
which will drive a **mono 2-way speaker**: **1× RS125-4 woofer + 1× RS100-4 tweeter**, 4 Ω each, one JAB5 channel each (2 of 4 channels used).
- a 6 pin header to connect i2s to the same jab5

The board powers a **WONDOM/Sure JAB5 amplifier module** (off-board, dual TDA7498E + ADAU1701 DSP, 4-channel Class D)
which will drive a **mono 2-way speaker**: **1× RS125-4 woofer + 1× RS100-4 tweeter**, 4 Ω each, one JAB5 channel each (2 of 4 channels used).
The board must be **JLCPCB turnkey**.
I want to power via a 140W usbc 3.1 laptop style power brick
I have DigiKey symbols and footprints for Kicad


## Key Electrical/Power Decisions
- **Power input**: Internal AC-DC PSU (Mean Well LRS-150-28) → 28V DC to carrier board via screw terminal.
- **Primary supply**: **Mean Well LRS-150-28** (28V/5.4A, 151W, open-frame AC-DC).
- **External connection**: Captive AC power cord (IEC C18 inlet or hardwired).
- **Audio target**: 30 W RMS per channel (2 channels), mono 2-way (1× RS125-4 + 1× RS100-4).
- **Total power budget**: ~97 W (2×30 W audio + CM5 + NVMe + losses). 54 W headroom at 151 W.
- **CM5 requirement**: 5 V input, **must stay ≥ 4.75 V at the CM5 pins**, no other pins powered before 5 V.
- **NVMe requirement**: 3.3 V ±5%.
- **CM5 3.3 V/1.8 V rails are limited (~600 mA)**, so NVMe must have a dedicated 3.3 V buck.

## Power Delivery Architecture
```
AC Mains → Captive cord → Internal AC-DC PSU (Mean Well LRS-150-28)
  → 28V DC → Carrier Board (screw terminal J1)
      ├── Direct 28V → Molex Micro-Fit 3.0 4-pin (J10) → JAB5
      ├── LM73606 buck → 5.02V (CM5, XVF3800 header, GPIO header)
      │     └── AP63357 buck → 3.3V (NVMe, GPIO header)
      └── Bulk cap (C11) for stability
```

## DSP / Audio
- JAB5 has ADAU1701 DSP; **limiter should be implemented in SigmaStudio**.
- RMS limiter target: 30 W into 4 Ω (~10.95 Vrms)
- Peak limiter target: 60 W into 4 Ω (~15.5 Vrms)
- Mono sum → low-pass to woofer, high-pass to tweeter.

## CM5 Carrier Features
- CM5 100-pin slot
- M.2 NVMe slot (PCIe Gen2 x1)
- 40-pin GPIO HAT header
- I2S header shared clocks (CM5 master, JAB5 + XVF3800 slaves)
- 20-pin XVF3800 header
- Matte black solder mask (JLCPCB standard)

## Manufacturing
- Target: JLCPCB turnkey assembly
- KiCad plugin: Bouni/kicad-jlcpcb-tools
- DigiKey symbols and footprints added
- CM5 symbols and footprints added (https://pip-assets.raspberrypi.com/categories/1098-design-files/documents/RP-008099-DD-1-CM5%20IO%20Board,%20revision%202,%20KiCAD%20files..zip) < CM5 + NVME
- EasyEDA2KiCad to import LCSC symbols/footprints if needed

## Open Decision
- **Fixed vs adjustable 5 V regulator**:
  - Fixed 5 V is only OK if total path resistance keeps CM5 ≥ 4.75 V.
  - Adjustable is safer (set ~5.05 V) to cover IR drop.
  - Need to decide based on actual layout resistance.

## Files
Primary spec file:
`D:/projects/smart-speaker/src/ai/speaker/hardware/pcb/theboard/specs.md`

## What to do next
If I ask a question, continue from this context and keep everything consistent with the constraints above.


## 1) Assumptions & Open Questions
- Should be able to export to JLCPCB via JLCPCB kicad plugin
- Confirm CM5 mechanical: CM5 uses two board-to-board connectors; verify the exact part number, pinout, and keepouts from the latest CM5 datasheet.
- Audio target: mono 2-way (1× RS125-4 woofer + 1× RS100-4 tweeter), 30 W RMS per channel (2 channels) with DSP limiting.
- **No external connectors**: Internal AC-DC PSU with captive AC cord. M.2, GPIO, I2S, XVF3800, and JAB5 headers are internal only.


## 2) Top-Level Block Diagram (text)
```
AC Mains → Captive AC Cord → Internal PSU (Mean Well LRS-150-28)
  └── 28V DC → Screw Terminal (J1, 2-pin)
                    │
                    ├──→ Molex Micro-Fit 3.0 4-pin (J10) → JAB5 (direct 28V)
                    │
                    ├──→ LM73606 buck → 5.02 V rail
                    │     ├── CM5 5 V pins (solid plane)
                    │     ├── 40-pin GPIO header 5 V
                    │     ├── XVF3800 header 5 V (via 2×10 ribbon)
                    │     └──→ AP63357 buck → 3.3 V rail
                    │           ├── M.2 NVMe 3.3 V
                    │           └── 40-pin GPIO header 3.3 V
                    │
                    └──→ Bulk cap (C11) for stability

CM5 Connectors (100-pin slot)
  ├── PCIe Gen2 x1 → M.2 NVMe (M-key)
  ├── I2S (master) → shared clocks → JAB5 + XVF3800 (slaves)
  └── I2C / SPI / GPIO → 40-pin HAT header + XVF3800 header
```

## 3) Power Tree & Budget
### 28V DC Input (from Internal PSU)
- Internal AC-DC PSU: Mean Well LRS-150-28 (28V/5.4A, 151W, open-frame).
- Housed inside enclosure, connected to carrier board via screw terminal (J1).
- HomePod-style captive AC cord for clean external appearance.

### 28V Rail (VSYS)
- 28V DC from internal PSU enters carrier board via 2-pin screw terminal (J1).
- 28V feeds JAB5 directly (via Molex Micro-Fit 3.0 4-pin, J10) and the LM73606 5V buck.
- Bulk capacitor (C11, 100µF) for stability on VSYS rail.

### 5 V Rail (CM5 + peripherals)
- LM73606 buck, 28 V input → 5.02 V output (adjustable via FB divider). 28 V within operating range (3.5-36 V rec, 40 V abs max).
- FB divider: R_top 100 kΩ + R_bottom 24.9 kΩ, V_REF = 1.0 V → V_OUT = 5.016 V.
- Margin to CM5 minimum (4.75 V): **266 mV** for IR drop — requires short, wide copper.
- Rating: 6 A continuous; loads are CM5 (~3 A peak), GPIO header 5 V, AP63357 input.
- Enable: LM73606 EN tied to 28V input (or via divider for enable threshold).
- Do not back-power any CM5 pin before 5 V is present.

### 3.3 V Rail (NVMe)
- AP63357 buck, 5 V input → 3.33 V output (FB divider: 31.6 kΩ / 10 kΩ, V_REF = 0.8 V).
- Rating: 3 A continuous; loads are NVMe (~2.5 A peak), GPIO header 3.3 V.
- CM5 3.3 V and 1.8 V rails are limited (~600 mA); NVMe must not draw from them.
- Enable: AP63357 EN tied to 5 V rail (powers up after 5 V stabilizes).

### JAB5 Supply (direct 28 V)
- JAB5 input range: 10-39 V. Running at 28 V — well within spec, more headroom per channel.
- Fed directly from 28V via Molex Micro-Fit 3.0 4-pin header (2×2, 3 mm pitch). Matches JAB5 J15 connector.
- JAB5 J15 pinout: **Pin 1,2 = GND / Pin 3,4 = VCC (+28 V)**. Do not reverse.
- JAB5 amp IC: dual TDA7498E (4-channel Class D). Rated 4×100 W at 6 Ω; at 4 Ω, clean power ~25-30 W/ch before THD exceeds 1%.
- JAB5 supports 4 Ω loads (confirmed by Wondom FAQ).
- 30 W/ch target at 4 Ω is achievable. DSP limiter in ADAU1701 is essential to enforce ceiling and protect drivers.
- Using 2 of 4 JAB5 channels: woofer (low-pass), tweeter (high-pass). 2 channels unused.

### Power Sequencing
1. AC power on → Internal PSU outputs 28V DC
2. 28V → LM73606 EN → 5V rises
3. 5V → AP63357 EN → 3.3V rises
4. CM5 boots. NVMe, XVF3800, GPIO all powered. CM5 is I2S master.

### System Power Budget (28V × 5.4A = 151W available)
| Load | Estimate |
|------|----------|
| JAB5 2 × 30 W output / ~90% eff | ~67 W |
| CM5 (peak) | ~15 W |
| NVMe (peak) | ~8 W |
| XVF3800 | ~2 W |
| Conversion + path losses | ~5 W |
| **Total** | **~97 W** |

- 54 W headroom at sustained full power on both channels. Average music power well below 30 W RMS per channel, so real-world headroom is much larger.

## 4) Internal PSU (Mean Well LRS-150-28)
### Specifications
- Model: LRS-150-28
- Input: 85-264 VAC, 47-63 Hz
- Output: 28V DC, 5.4A, 151.2W
- Form factor: Open-frame, 159 × 97 × 30 mm
- Efficiency: ~89%
- Protections: OVP, OLP, SCP, OTP

### Enclosure Integration
- PSU mounted inside enclosure, separate from carrier board
- Captive AC cord (IEC C18 inlet or hardwired) — HomePod-style aesthetic
- 28V DC output wired to carrier board screw terminal (J1)
- Ensure adequate ventilation for PSU cooling

## 5) CM5 Power & Control
- 5 V input pins: feed all required CM5 5 V pins with a solid plane. Target ≥ 4.75 V at pins under full load.
- Enable/Reset: wire RUN/RESET and any required boot configuration pins to test pads or header.
- Power sequencing: 5 V rail only rises after 28V present → LM73606 EN chain. No CM5 pins powered before 5 V is stable.

## 6) PCIe Gen2 x1 -> M.2 NVMe
- Differential pair routing: controlled impedance (typically 85 ohm diff), length-matched.
- Clock: keep PCIe ref clock clean and short; follow CM5 design rules.
- Power: NVMe 3.3 V only; add bulk + high-frequency decoupling close to M.2.
- Reset/CLKREQ/PEWAKE: connect per CM5 requirements and M.2 spec.

## 7) I2S Audio Topology
- CM5 as I2S master; JAB5 + XVF3800 as slaves.
- Shared clocks: MCLK, BCLK, LRCLK routed as a short, matched-length star or daisy chain with minimal stubs.
- Data lines: separate SDOUT (CM5 -> amps) and SDIN (mics -> CM5).
- Level shifting if any module is not 3.3 V logic.

## 8) DSP Limiter (JAB5 ADAU1701)
- Implement a limiter in the DSP project (SigmaStudio) to protect the drivers.
- Target RMS limiter per channel: 30 W into 4 ohm (~10.95 Vrms).
- Target peak limiter per channel: 60 W into 4 ohm (~15.5 Vrms) for brief transients.
- Mono sum → 2-way crossover: low-pass to woofer (RS125-4), high-pass to tweeter (RS100-4). Crossover point (~2-3 kHz) TBD in SigmaStudio.

## 9) Speaker Loads (JAB5)
- Planned drivers: Dayton Audio 1× RS125-4 (woofer) + 1× RS100-4 (tweeter).
- Mono 2-way: each driver on its own JAB5 channel (2 of 4 channels used), 4 Ω nominal per driver.
- JAB5 in 4.0 mode (4 independent channels). Channels 3 and 4 unused.
- 30 W/ch at 4 Ω — at the edge of clean operation for TDA7498E. DSP limiter enforces ceiling.

## 10) Headers & IO
### 28V DC Input — Screw Terminal, 2-pin
- Receives 28V DC from internal Mean Well LRS-150-28 PSU.
- Simple, reliable connection. No polarity protection on carrier (rely on correct wiring).

### JAB5 Power Header — Molex Micro-Fit 3.0, 4-pin vertical (2×2, 3 mm pitch)
- Matches JAB5 J15 connector (Molex 4Pos-3mm). **NOT Mini-Fit Jr (4.2 mm pitch).**
- **Pin 1, 2: GND. Pin 3, 4: VCC (+28 V).** *(Matches JAB5 J15 pinout — do not reverse.)*
- Each Micro-Fit 3.0 pin rated 5 A; two pins per rail provides margin for ~3.5 A at 28 V (97 W / 28 V ≈ 3.5 A total system draw).
- Place close to input screw terminal to minimize trace resistance.

### 40-pin GPIO Header
- Provide 3.3 V, 5 V, GND, and standard I2C/SPI/UART as per RPi HAT spec.
- Internal-only; use low-profile connector or DNP if not needed for development.

### XVF3800 20-pin Header (2×10, 2.54 mm)
- Power: 5 V + GND from LM73606 rail (via 2×10 header).
- I2S: MCLK, BCLK, LRCLK (shared clocks from CM5), SDIN (XVF3800 → CM5), SDOUT (CM5 → XVF3800).
- Control: I2C (SDA/SCL), RST, BOOT, INT.
- Remaining pins: GND fill / reserved.

### JAB5 I2S Header (6-pin, carrier board side)
- JAB5 I2S input is **J6, JST PH 6-pos, 2 mm pitch** — not a standard 2.54 mm shrouded header.
- **JAB5 J6 I2S input pinout:**

| Pin | Signal |
|-----|--------|
| 1 | LRCLK |
| 2 | BCLK |
| 3 | DATA1 (I2S data in) |
| 4 | GND |
| 5 | +5 V |
| 6 | MCLK |

- Carrier board options: place a JST PH 6-pos (2 mm) to match directly, or use a 6-pin 2.54 mm shrouded header with an adapter cable (PH↔IDC). Decide based on cable routing in enclosure.
- Note: J6 pin 5 expects **+5 V** (not 3.3 V). Feed from 5 V rail.
- Add series damping (22-47 Ω) near CM5 on MCLK/BCLK/LRCLK if signal integrity requires it.

### Dedicated I2S Debug Header (1×7, 2.54 mm) — optional
- Pinout: MCLK, BCLK, LRCLK, SDOUT, SDIN, 3.3 V, GND.
- Directly parallels the I2S signals. For development/debug use.

## 11) Layout & Stackup
- Layer count: 4-layer minimum; 6-layer recommended for PCIe + high-current power.
- Power planes: dedicate internal planes for 5 V and GND; use wide pours for 28V high-current path to JAB5 header.
- High-current paths: short, wide copper, multiple vias; keep away from sensitive audio/I2S lines.
- PCIe: keep diff pairs on inner layers with continuous reference plane.

## 12) DFM / JLCPCB Manufacturing
- **Board fabrication**: JLCPCB. Matte black solder mask, standard stackup (4- or 6-layer).
- **Parts sourcing**: not restricted to LCSC. Use the best-fit part; source from LCSC, Mouser, DigiKey, etc. as needed.
- **Assembly**: JLCPCB SMT assembly for LCSC-stocked passives/ICs where possible; hand-solder or second-source anything not on LCSC.
- **Footprints**: must be KiCad-compatible. Sources:
  - **CM5 slot + M.2 NVMe socket**: official RPi design files (already downloaded).
  - Other parts: KiCad built-in libs, EasyEDA2KiCad plugin, SnapEDA, Ultra Librarian, or manual creation.
- Keep passives standard (0603/0805). Use KiCad JLCPCB plugin (Bouni/kicad-jlcpcb-tools) for BOM/CPL export when using LCSC parts.

## 13) Internal PSU Selection
**Mean Well LRS-150-28** selected for:
- 28V output matches JAB5 optimal voltage range (30W/ch clean power)
- 151W capacity with 54W headroom for 97W system load
- Open-frame design fits inside speaker enclosure
- Industrial reliability, built-in protections
- Universal AC input (85-264V)

Alternative if more power needed: LRS-200-24 (24V/200W) with slight reduction in JAB5 headroom.

## 14) Power BOM (Simplified - No USB-C PD)
Parts listed with LCSC numbers where available; non-LCSC parts sourced from Mouser/DigiKey. Footprints via KiCad libs, EasyEDA2KiCad, or SnapEDA.

| Name / Purpose | Value / Notes | LCSC Part # |
|---|---|---|
| DC input connector (J1) | Screw terminal, 2-pin, 5.08mm pitch | **TBD** |
| 5 V buck controller (U2) | TI LM73606Q1 (6 A, 3.5-36 V operating, 40 V abs max) | C3198272 |
| 5 V inductor (L1) | 3.3 µH, >=10 A | C19268642 |
| 5 V output cap (C2) | 47 µF, 10 V, 1206 | C1859 |
| 5 V input cap (C1) | 22 µF, 50 V, 1210 | C172721 |
| 5 V bootstrap cap (C5) | 0.1 µF | C14663 |
| 5 V VCC cap (C3) | 2.2 µF | C1607 |
| 5 V BIAS cap (C4) | 0.1 µF | C14663 |
| 5 V RT resistor (R10) | 39.2 kΩ (sets ~1 MHz) | C137734 |
| 5 V FB divider (R6, R7) | R_top 100 kΩ + R_bottom 24.9 kΩ → V_OUT ≈ 5.02 V (1% 0603) | C14675 (100 kΩ) + C93643 (24.9 kΩ) |
| 3.3 V buck controller (U3) | Diodes AP63357 (3 A) | C2158014 |
| 3.3 V inductor (L2) | 4.7 µH, >=4 A | C524595 |
| 3.3 V input cap (C6) | 10 µF, 16-50 V | C172721 |
| 3.3 V output cap (C7) | 22 µF, 10 V, 1206 | C1859 |
| 3.3 V bootstrap cap (C8) | 0.1 µF | C14663 |
| 3.3 V FB divider (R8, R9) | 31.6 kΩ / 10 kΩ → ~3.3 V | C185339 + C98220 |
| VSYS bulk cap (C11) | 100 µF, 50 V electrolytic | C445060 |
| JAB5 power header (J10) | Molex Micro-Fit 3.0, 4-pin vertical (2×2, 3 mm pitch). Pin 1,2=GND / Pin 3,4=VCC | **TBD** (verify LCSC) |
| JAB5 I2S header (J8) | JST PH 6-pos, 2 mm pitch — matches JAB5 J6 I2S input connector | **TBD** (verify LCSC) |

Notes:
- LM73606 FB divider: 100 kΩ / 24.9 kΩ → 5.016 V (266 mV margin to CM5 4.75 V min).
- LM73606 abs max is **40 V** (not 36 V). 36 V is the recommended operating range. 28 V input is within operating range.
- TBD part numbers: screw terminal, Molex Micro-Fit 3.0 4-pin, JST PH 6-pos. Check LCSC first; fall back to Mouser/DigiKey.

## 15) Bring-Up & Test Plan (minimal)
- 28V input: verify Mean Well PSU outputs 28V DC. Connect to carrier board screw terminal.
- 5 V rail: verify 5.0 V ± 50 mV at CM5 pins under load (use scope, not just DMM).
- 3.3 V rail: verify 3.3 V ± 5% at M.2 and XVF3800 header under load.
- CM5 boot: confirm clean boot from cold plug (no brown-out, no back-power).
- NVMe: run PCIe link training and storage detection.
- Audio: verify I2S clocks and basic playback on both JAB5 channels; check JAB5 28 V rail under load.
- Thermal: monitor LM73606 and JAB5 temperatures at sustained 2-channel playback.

## Critical Design Decisions
- **Internal AC-DC PSU (not USB-C PD)**: Simplified power architecture. Mean Well LRS-150-28 provides 28V/5.4A (151W) from AC mains. 54W headroom for 97W system. HomePod-style captive AC cord.
- **Adjustable 5 V (5.02 V)**: covers IR drop to CM5. If layout analysis shows > 266 mV drop, increase R_top or decrease R_bottom in FB divider.
- **Direct 28V to JAB5**: JAB5 runs directly from 28V (within its 10-39 V range). More headroom per channel.
- **JAB5 amp IC is TDA7498E ×2** (not TPA3255): 4-channel Class D. Rated 4×100 W at 6 Ω. At 4 Ω, clean power ~25-30 W/ch. DSP limiter essential.
- **Simplified carrier board**: No USB-C PD controller, TVS, N-FET, or associated circuitry. Just buck converters and headers. Fewer components, simpler layout, lower cost.

## Next Steps
- ✅ Power simplified — Internal AC-DC PSU (Mean Well LRS-150-28), no USB-C PD
- ✅ Schematic updated — PD components removed, screw terminal DC input added
- ⏳ Complete schematic wiring in KiCad GUI (connect IC pins, add GND symbols, run ERC)
- ⏳ Look up part numbers for TBD items: screw terminal, Molex Micro-Fit 3.0 4-pin, JST PH 6-pos.
- ⏳ Pin-by-pin signal checklist for CM5 ↔ M.2 ↔ headers (once CM5 pinout source is confirmed).
- ⏳ Layout guide for high-current 28V path + PCIe on a 4/6-layer stackup.
- ⏳ Enclosure design for internal PSU + carrier board + JAB5

## KiCad Schematic Progress
**File**: `theboard.kicad_sch`

### Components Placed (Simplified - No USB-C PD)
| Designator | Part | Value/Description |
|------------|------|-------------------|
| U2 | LM73606RNPT | 5V buck (28V→5.02V) |
| U3 | AP63357DV-7 | 3.3V buck (5V→3.3V) |
| J1 | Screw terminal 2-pin | 28V DC input (from internal PSU) |
| J5 | CM5 slot | Raspberry Pi CM5 socket |
| J6 | M.2 M-key | NVMe socket |
| J7 | 2x20 header | GPIO header |
| J8 | JST PH 6-pos | JAB5 I2S out |
| J9 | 2x10 header | XVF3800 header |
| J10 | Molex 4-pin | JAB5 power (28V) |
| R6, R7 | 100kΩ / 24.9kΩ | 5V FB divider |
| R8, R9 | 31.6kΩ / 10kΩ | 3.3V FB divider |
| R10 | 39.2kΩ | RT (1MHz switching) |
| L1 | 3.3µH | LM73606 inductor |
| L2 | 4.7µH | AP63357 inductor |
| C1 | 22µF | LM73606 input cap |
| C2 | 47µF | LM73606 output cap |
| C3 | 2.2µF | LM73606 VCC cap |
| C4 | 0.1µF | LM73606 BIAS cap |
| C5 | 0.1µF | LM73606 CBOOT cap |
| C6 | 10µF | AP63357 input cap |
| C7 | 22µF | AP63357 output cap |
| C8 | 0.1µF | AP63357 BST cap |
| C11 | 100µF | VSYS bulk cap |

**Removed (USB-C PD eliminated):** U1 (AP33771C), J1 USB-C (replaced with screw terminal), D1 (TVS), Q1 (N-FET), R1-R5, R11, R12, C9, C10

### Power Symbols Added
- GND at FB divider grounds
- +5V at 5V output rail
- +3V3 at 3.3V output rail
- PWR_FLAG on VSYS
- Global label: VSYS

### Internal PSU (External to Carrier Board)
- Mean Well LRS-150-28 (28V/5.4A, 151W, open-frame)
- Captive AC cord (HomePod-style)

### Next Steps in KiCad GUI
1. Connect IC pins to passive components
2. Add more GND symbols near input caps
3. Run ERC to find unconnected pins
4. Add text annotations for power rails
