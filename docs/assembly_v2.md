# Assembly parts list (PCB v2)

Reference names match the silkscreen on `motorized-faders/motorized-faders.kicad_pcb`.
Counts are per board. Solder top to bottom: lowest parts first.

| Step | Reference on PCB | Part | MPN | Qty | Notes |
|---|---|---|---|---|---|
| 1 | `R_BTN_FADER1` `R_BTN_GEN_PU1` `R_DISP_RES1` `R_F1_WIPER1` `R_F2_WIPER1` `R_F3_WIPER1` `R_F4_WIPER1` `R_PD_SENSE_TOP1` `R_SW_F4` `R_SW_G4` `R_VM_SENSE_TOP1` | 10 kΩ 0805 | `RC0805FR-0710KL` | 11 | |
| 1 | `R_SC_BAL1` `R_SC_BAL2` `R_SW_F3` `R_SW_G3` | 4.7 kΩ 0805 | `RC0805FR-074K7L` | 4 | |
| 1 | `R_PD_SENSE_BOT1` `R_VM_SENSE_BOT1` | 3.3 kΩ 0805 | `RC0805FR-073K3L` | 2 | |
| 1 | `R_SW_F2` `R_SW_G2` | 2.2 kΩ 0805 | `RC0805FR-072K2L` | 2 | |
| 2 | `C_VCC_A1` `C_VCC_B1` `C_VM_A_HF1` `C_VM_B_HF1` `C_BTN_FADER1` `C_BTN_GEN1` `C_DISP_RES1` `C_MCP3208_VDD_GND1` `C_MCP3208_VREF_GND1` `C_PD_SENSE1` | 100 nF 50 V X7R 0805 | `CL21B104KBCNNNC` | 10 | |
| 2 | `C_F1_WIPER1` `C_F2_WIPER1` `C_F3_WIPER1` `C_F4_WIPER1` | 10 nF 50 V X7R 0805 | `CL21B103KBANNNC` | 4 | |
| 3 | `D_BOOST1` `D_PD1` | Schottky 40 V 5 A, SMC | `B540C-13-F` | 2 | **Polarity.** Cathode bar on the +9V side: right-hand pad on both, board viewed with the Pico at the top |
| 4 | `U_MOT_A1` `U_MOT_B1` | TB6612FNG SSOP-24 | `TB6612FNG,C,8,EL` | 2 | **Either** this **or** step 5, never both. Pin 1 to the silkscreen dot |
| 5 | `J_MOT_BOB_A_P1` `J_MOT_BOB_A_P2` `J_MOT_BOB_B_P1` `J_MOT_BOB_B_P2` | Female header 8-pos 2.54 mm | `PPTC081LFBN-RC` | 4 | Only with SparkFun-style breakouts instead of step 4 |
| 6 | `U_ADC1` | MCP3208 DIP-16 | `MCP3208-CI/P` | 1 | Notch to pin 1 mark |
| 7 | `A1` | Female header 20-pos 2.54 mm (for the Pico) | `PPTC201LFBN-RC` | 2 | USB end toward the top board edge |
| 8 | `SW_F1` `SW_F2` `SW_F3` `SW_F4` `SW_G1` `SW_G2` `SW_G3` `SW_G4` | JST-XH 2-pin header | `B2B-XH-A(LF)(SN)` | 8 | Connector on the board; button is off-board. `SW_G3/G4` optional |
| 8 | `J_F1` `J_F2` `J_F3` `J_F4` | JST-XH 5-pin header | `B5B-XH-A(LF)(SN)` | 4 | |
| 8 | `J_DISP1` `J_DISP2` `J_DISP3` `J_DISP4` | JST-XH 8-pin header | `B8B-XH-A(LF)(SN)` | 4 | |
| 8 | `J_BOOST1` `J_PD1` | JST-XH 4-pin header | `B4B-XH-A(LF)(SN)` | 2 | `J_PD1` optional |
| 9 | `C_BOOST_IN1` `C_VM_A1` `C_VM_B1` | 100 µF 25 V electrolytic | `EEU-FR1E101B` | 3 | **Polarity.** + at the marked pad. Must have the `B` suffix (5 mm pitch) |
| 10 | `C_SC1` `C_SC2` | Supercap 2.5 F 5.4 V | `PHV-5R4V255-R` | 2 | **Polarity.** Square pad and `+` on silkscreen = positive |
| 10 | `C_SC3` `C_SC4` | Supercap 2.5 F 5.4 V | `PHV-5R4V255-R` | 0 or 2 | DNP by default. **Fit both or neither**: one alone overvolts the other cell |

Nothing to fit: `NT1` (copper net tie), `TP_*` (bare test pads), the 4 M3 holes.

## Off-board

| Connects to | Part | Qty |
|---|---|---|
| `J_F1..4` | Alps RSA0N11M9 fader, on 5-wire harness | 4 |
| `J_DISP1..4` | Adafruit 326 OLED, SPI jumpers cut | 4 |
| `SW_F1..4`, `SW_G1..4` | Omron `B3F-4050` (`SW_G3/G4` optional) | 6-8 |
| `J_BOOST1` | MT3608 module (set to 11 V **before** connecting), `LM317KCSE3`, 12 Ω `CF14JT12R0`, `1N4007`, wired in the harness | 1 each |
| `J_PD1` | Adafruit 5807 HUSB238, 9 V jumper (optional) | 0-1 |
| `A1` | Raspberry Pi Pico, on the sockets | 1 |

## Harness pinouts (pin 1 first)

| Connector | Pins |
|---|---|
| `J_F1..4` | 1 `MOTOR_A`, 2 `MOTOR_B`, 3 `GND`, 4 `3V3`, 5 `WIPER` |
| `J_BOOST1` | 1 `+5V` (MT3608 VIN+), 2 `GNDPWR` (VIN−), 3 `+9V_BOOST` (LM317 stage), 4 `GNDPWR` (VOUT−) |
| `J_PD1` | 1 `+9V_PD`, 2 `GNDPWR`, 3 `I2C_SDA`, 4 `I2C_SCL` |
| `J_DISP1` | 1 `SPI_MOSI`, 2 `SPI_SCK`, 3 `DISP_DC`, 4 `DISP_RES`, 5 `DISP1_CS`, 6 `Display_1/3Vo`, 7 `+5V`, 8 `GND` |

Housings: `XHP-2`, `XHP-4`, `XHP-5`, `XHP-8`. Contacts: `SXH-001T-P0.6` (28-22 AWG), 76 per board.

## Boost harness (`J_BOOST1`)

MT3608 at 11 V, then an LM317 current source (~104 mA). No board change.

LM317 pins, text facing you, legs down: left `ADJ`, middle `OUT`, right `IN`.

1. Power the MT3608 from 5 V alone and set VOUT to **11 V** with its trimmer.
2. `J_BOOST1` pin 1 → MT3608 VIN+, pin 2 → VIN−.
3. MT3608 VOUT+ → LM317 `IN`. VOUT− → `J_BOOST1` pin 4.
4. 12 Ω between LM317 `OUT` and `ADJ`.
5. LM317 `ADJ` → `J_BOOST1` pin 3. Take the wire from `ADJ`, not `OUT`.
6. `1N4007` across the LM317: stripe to `IN`, other end to `OUT`.

**The LM317 tab is connected to `OUT` and is live.** Insulate it so it can't touch
the module, the board or the case. It dissipates up to ~1 W with empty caps, so
leave it in free air.

After the first full charge, check `+9V_BOOST` ≤ 9.8 V and each cell ≤ 5.4 V.
Trim the MT3608 down if higher.
