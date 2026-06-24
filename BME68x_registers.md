# BME68x / BME690 register reference

Quick-lookup distilled from the BME688 datasheet (rev 1.7). The BME690
shares chip ID `0x61` and the same register map for T/P/H/gas; the parts
where the two diverge are flagged. Addresses are the 7-bit I²C view
(SPI dev: same offsets, MSB clear = write).

I²C address: `0x76` (SDO low) or `0x77` (SDO high). The APP3.1 shuttle
wires SDO low, so we use `0x76`.

## Identification / control

| Reg  | Name           | Access | Notes                                                 |
|------|----------------|--------|-------------------------------------------------------|
| 0xD0 | `chip_id`      | R      | `0x61` on BME688 & BME690.                            |
| 0xF0 | `variant_id`   | R      | `0x00`=BME680, `0x01`=BME688/690 (selects gas calc).  |
| 0xE0 | `reset`        | W      | Write `0xB6` → soft reset (≈10 ms to settle).         |
| 0x73 | `status`       | R/W    | `mem_page` (SPI), `spi_3w_int_en`.                    |
| 0x74 | `ctrl_meas`    | R/W    | `osrs_t[7:5] | osrs_p[4:2] | mode[1:0]`.              |
| 0x72 | `ctrl_hum`     | R/W    | `osrs_h[2:0]`.                                        |
| 0x75 | `config`       | R/W    | IIR filter, SPI 3-wire.                               |
| 0x71 | `ctrl_gas_1`   | R/W    | `run_gas[5/4] | nb_conv[3:0]` — gas enable + last step idx. |
| 0x70 | `ctrl_gas_0`   | R/W    | `heat_off[3]` to bypass heater.                       |
| 0x6E | `gas_wait_shared` | R/W | Parallel mode only: shared wait between heater steps (same encoding as `gas_wait_x`). |
| 0x64+i| `gas_wait_i`  | R/W    | i=0..9. Heater duration code per profile step.        |
| 0x5A+i| `res_heat_i`  | R/W    | i=0..9. Heater resistance code per step.              |

### `mode` codes (ctrl_meas[1:0])
- `00` sleep
- `01` forced (single measurement, returns to sleep)
- `10` parallel (continuous, runs heater profile)

### Parallel-mode setup sequence
1. Soft reset, wait 10 ms.
2. For each step `i` (0..N-1): write `res_heat_i = calc_res_heat(T_target, T_amb)`
   and `gas_wait_i = calc_gas_wait(dur_ms)`. Up to 10 steps.
3. Write `gas_wait_shared` (0x6E) — shared wait between steps (~140 ms typical).
4. Set `ctrl_gas_0 = 0x00` (heater on), `ctrl_gas_1 = run_gas_bit | (N - 1)`.
5. Set `ctrl_hum`, then `ctrl_meas = (osrs_t<<5) | (osrs_p<<2) | 0b10`.
6. Sensor runs continuously. Each measurement carries `gas_meas_index_0`
   (low 4 bits of `meas_status_0`) tagging which profile step it came from.

### Oversampling codes (osrs_*)
`000`=skip, `001`=×1, `010`=×2, `011`=×4, `100`=×8, `101`=×16.
`skip` zeros the corresponding ADC output.

## Measurement results (read as a contiguous block)

| Reg  | Name              | Notes                                                 |
|------|-------------------|-------------------------------------------------------|
| 0x1D | `meas_status_0`   | b7 `new_data_0`, b6 `gas_measuring`, b5 `measuring`, b3:0 `gas_meas_index_0`. |
| 0x1E | `meas_index_0`    | Sample counter.                                       |
| 0x1F | `press_msb`       | High 8 bits of 20-bit raw pressure.                   |
| 0x20 | `press_lsb`       | Middle 8 bits.                                        |
| 0x21 | `press_xlsb`      | Low 4 bits in upper nibble.                           |
| 0x22 | `temp_msb`        | High 8 bits of 20-bit raw temperature.                |
| 0x23 | `temp_lsb`        | Middle 8 bits.                                        |
| 0x24 | `temp_xlsb`       | Low 4 bits in upper nibble.                           |
| 0x25 | `hum_msb`         | High 8 bits of 16-bit raw humidity.                   |
| 0x26 | `hum_lsb`         | Low 8 bits.                                           |
| 0x2A | `gas_r_msb`       | High 8 bits of 10-bit gas ADC.                        |
| 0x2B | `gas_r_lsb`       | b7:6 low 2 bits of gas ADC, b5 `gas_valid_r`, b4 `heat_stab_r`, b3:0 `gas_range_r`. |

Field assembly:
```
raw_p = (P_MSB << 12) | (P_LSB << 4) | (P_XLSB >> 4)
raw_t = (T_MSB << 12) | (T_LSB << 4) | (T_XLSB >> 4)
raw_h = (H_MSB << 8)  | H_LSB
raw_g = (G_MSB << 2)  | (G_LSB >> 6)              # 10-bit
gas_range = G_LSB & 0x0F
```

## Calibration coefficients

Two clusters in non-volatile memory, plus a third gas-cal block. Read once
at startup. Signedness shown matters for compensation.

### Cluster A — 0x8A..0xA0

| Addr        | Name      | Type  |
|-------------|-----------|-------|
| 0x8A / 0x8B | par_t2    | int16 |
| 0x8C        | par_t3    | int8  |
| 0x8E / 0x8F | par_p1    | uint16|
| 0x90 / 0x91 | par_p2    | int16 |
| 0x92        | par_p3    | int8  |
| 0x94 / 0x95 | par_p4    | int16 |
| 0x96 / 0x97 | par_p5    | int16 |
| 0x99        | par_p6    | int8  |
| 0x98        | par_p7    | int8  |
| 0x9C / 0x9D | par_p8    | int16 |
| 0x9E / 0x9F | par_p9    | int16 |
| 0xA0        | par_p10   | uint8 |

### Cluster B — 0xE1..0xEE

| Addr           | Name      | Notes                                          |
|----------------|-----------|------------------------------------------------|
| 0xE1 / 0xE2[hi]| par_h2    | uint12 (E1=bits 11:4, E2[7:4]=bits 3:0)        |
| 0xE3 / 0xE2[lo]| par_h1    | uint12 (E3=bits 11:4, E2[3:0]=bits 3:0)        |
| 0xE4           | par_h3    | int8                                           |
| 0xE5           | par_h4    | int8                                           |
| 0xE6           | par_h5    | int8                                           |
| 0xE7           | par_h6    | uint8                                          |
| 0xE8           | par_h7    | int8                                           |
| 0xE9 / 0xEA    | par_t1    | uint16                                         |
| 0xEB / 0xEC    | par_gh2   | int16                                          |
| 0xED           | par_gh1   | int8                                           |
| 0xEE           | par_gh3   | int8                                           |

### Heater calibration extras

| Addr | Name              | Notes              |
|------|-------------------|--------------------|
| 0x02 | `res_heat_range`  | bits 5:4 used      |
| 0x00 | `res_heat_val`    | int8               |
| 0x04 | `range_sw_err`    | bits 7:4, int4     |

## Compensation formulas (floating point, datasheet 8.3-8.6)

`t_fine` is computed during temperature compensation and reused by pressure
and humidity.

### Temperature → °C
```
var1   = (raw_t/16384.0 - par_t1/1024.0) * par_t2
var2   = ((raw_t/131072.0 - par_t1/8192.0)**2) * par_t3 * 16.0
t_fine = var1 + var2
T_degC = t_fine / 5120.0
```

### Pressure → Pa
```
var1 = t_fine/2.0 - 64000.0
var2 = var1*var1 * (par_p6/131072.0)
var2 = var2 + var1*par_p5*2.0
var2 = var2/4.0 + par_p4*65536.0
var1 = ((par_p3*var1*var1)/16384.0 + par_p2*var1) / 524288.0
var1 = (1.0 + var1/32768.0) * par_p1
p    = 1048576.0 - raw_p
p    = (p - var2/4096.0) * 6250.0 / var1
var1 = par_p9*p*p / 2147483648.0
var2 = p * (par_p8/32768.0)
var3 = (p/256.0)**3 * (par_p10/131072.0)
P_Pa = p + (var1 + var2 + var3 + par_p7*128.0)/16.0
```

### Humidity → %RH
```
T = t_fine/5120.0
var1 = raw_h - (par_h1*16.0 + (par_h3/2.0)*T)
var2 = var1 * ((par_h2/262144.0) *
              (1.0 + (par_h4/16384.0)*T + (par_h5/1048576.0)*T*T))
var3 = par_h6/16384.0
var4 = par_h7/2097152.0
H    = var2 + (var3 + var4*T) * var2*var2
H    = clamp(H, 0.0, 100.0)
```

### Gas resistance → Ω (heater on)
Two formulas, picked by `variant_id` (reg `0xF0`):

**`variant_id == 0x00`** — BME680, lookup-table form:
```
k1 = [0,0,0,0,0,-1,0,-0.8,0,0,-0.2,-0.5,0,-1,0,0][gas_range]
k2 = [0,0,0,0,0.1,0.7,0,-0.8,-0.1,0,0,0,0,0,0,0][gas_range]
var1 = 1340.0 + 5.0*range_sw_err
var2 = var1 * (1.0 + k1/100.0)
gas_res = 1.0 / ((1 << gas_range) * 1.25e-7 * (1.0 + k2/100.0) *
                 ((raw_g - 512.0) / var2 + 1.0))
```

**`variant_id == 0x01`** — BME688/690, closed form:
```
var1 = 262144 >> gas_range
var2 = (raw_g - 512) * 3 + 4096
gas_res = 1e6 * var1 / var2
```

### Heater resistance code (write to `res_heat_x` for target temp °C)
Needs the gas-cal extras (`par_gh1..3`, `res_heat_range`, `res_heat_val`)
and the current ambient temperature.
```
var1 = (par_gh1/16.0) + 49.0
var2 = (par_gh2/32768.0)*0.0005 + 0.00235
var3 = par_gh3/1024.0
var4 = var1 * (1.0 + var2*target_C)
var5 = var4 + var3*amb_C
res_heat_x = 3.4 * ((var5 * (4.0/(4.0 + res_heat_range)) *
                     (1.0/(1.0 + res_heat_val*0.002))) - 25)
```

### Heater duration code (write to `gas_wait_x`)
Encodes a duration in 1/4/16/64 ms steps:
```
factor = 0
while dur_ms > 0x3F:
    dur_ms //= 4
    factor += 1
gas_wait_x = dur_ms | (factor << 6)
```
Max ≈ 4032 ms per step.

## Typical forced-mode sequence (T/P/H only)
1. Soft reset (`0xE0 ← 0xB6`), wait 10 ms.
2. Set `ctrl_hum` osrs_h.
3. Set `ctrl_gas_1` = `0x00` to disable gas.
4. Set `ctrl_meas` = `(osrs_t<<5) | (osrs_p<<2) | 0b01` → starts forced measurement.
5. Poll `meas_status_0` until bit 7 (`new_data_0`) is set.
6. Read 8 bytes from `0x1F` → unpack P/T/H ADCs.
7. Apply compensation.
8. Loop from step 4 (forced mode auto-returns to sleep).

## Sources
- BME688 Datasheet (Bosch Sensortec, BST-BME688-DS000), §5 register map, §8 compensation.
- Reference C driver: [boschsensortec/BME68x_SensorAPI](https://github.com/boschsensortec/BME68x_SensorAPI).
