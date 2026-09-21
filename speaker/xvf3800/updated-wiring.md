updated wiring....

| **Wire Color**             | **Pi Physical Pin** | **Pi GPIO** | **Signal Name**         | **XVF3800 Pin (silkscreen)** | **Notes**                   |
| -------------------------- | ------------------: | ----------: | ----------------------- | ---------------------------- | --------------------------- |
| 🔴 **Red**                 |        Pin 2 (or 4) |           — | **5V**                  | **5V**                       | Power (header-powered only) |
| ⚫ **Black**                |     Pin 6 (any GND) |           — | **GND**                 | **GND**                      | Common ground               |
| 🟡 **Yellow**              |          **Pin 12** |  **GPIO18** | **I2S BCLK**            | **X0D31** *(your clock pin)* | Bit clock                   |
| 🟢 **Green**               |          **Pin 35** |  **GPIO19** | **I2S LRCLK / FS**      | **X0D38**                    | Frame sync                  |
| 🔵 **Blue**                |          **Pin 38** |  **GPIO20** | **I2S DOUT (XVF → Pi)** | **X1D00**                    | **Required** (audio data)   |
| 🟣 **Purple** *(optional)* |              Pin 40 |      GPIO21 | I2S DIN (Pi → XVF)      | X1D01                        | Optional / can omit         |
