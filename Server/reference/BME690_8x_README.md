# BME690 8x Shuttle Board Reader

This project reads a Bosch BME690 8x shuttle board through a Bosch APP3 board using COINES.

The reader is a C program. It streams CSV data with columns for sensors `s0` through `s7`.

## Files to Send

Send these to the other user:

```text
bme690_69x_reader.c
BME690_SensorAPI/
```

The main source file is:

```text
/home/hayeonglee/bme690_69x_reader.c
```

The Bosch SensorAPI folder is:

```text
/home/hayeonglee/BME690_SensorAPI/
```

## Hardware Required

- Bosch APP3 application board
- Bosch BME690 8x shuttle board
- USB cable connected from APP3 to the computer

## Software Required

The user needs COINES / `coinespy` installed, including the shared library:

```text
libcoines_64.so
```

On this machine, it is located at:

```text
/home/hayeonglee/venvs/bme690/lib/python3.12/site-packages/coinespy/libcoines_64.so
```

On another computer, this path may be different. Update the compile command if needed.

## Build

From the directory containing `bme690_69x_reader.c` and `BME690_SensorAPI/`, compile with:

```bash
gcc -Wall -Wextra -O2 \
  -I./BME690_SensorAPI \
  ./bme690_69x_reader.c ./BME690_SensorAPI/bme69x.c \
  -L./venvs/bme690/lib/python3.12/site-packages/coinespy \
  -lcoines_64 \
  -Wl,-rpath,./venvs/bme690/lib/python3.12/site-packages/coinespy \
  -o ./bme690_69x_reader
```

If `libcoines_64.so` is elsewhere, replace both `./venvs/bme690/lib/python3.12/site-packages/coinespy` paths with the correct folder.

Example for this machine:

```bash
gcc -Wall -Wextra -O2 \
  -I/home/hayeonglee/BME690_SensorAPI \
  /home/hayeonglee/bme690_69x_reader.c /home/hayeonglee/BME690_SensorAPI/bme69x.c \
  -L/home/hayeonglee/venvs/bme690/lib/python3.12/site-packages/coinespy \
  -lcoines_64 \
  -Wl,-rpath,/home/hayeonglee/venvs/bme690/lib/python3.12/site-packages/coinespy \
  -o /home/hayeonglee/bme690_69x_reader
```

## Run Live Stream

To print live readings in the terminal:

```bash
./bme690_69x_reader
```

To print live readings and save them to a CSV file:

```bash
./bme690_69x_reader | tee bme690_8x_live.csv
```

Stop the stream with:

```text
Ctrl+C
```

## Output Format

The output is CSV. It starts with a header like:

```text
time,s0_temp_c,s0_humidity_pct,s0_pressure_hpa,s0_gas_ohm,s0_status,s0_variant,...,s7_temp_c,s7_humidity_pct,s7_pressure_hpa,s7_gas_ohm,s7_status,s7_variant
```

Each row contains one timestamp and the readings for all detected sensors.

If a sensor does not respond, its columns are left blank.

## Chip-Select Mapping Used

The code treats the BME690 8x shuttle as an SPI board with one chip-select line per sensor:

```text
sensor0: MINI_SHUTTLE_PIN_1_4 / 0x10
sensor1: MINI_SHUTTLE_PIN_1_5 / 0x11
sensor2: MINI_SHUTTLE_PIN_1_6 / 0x12
sensor3: MINI_SHUTTLE_PIN_1_7 / 0x13
sensor4: MINI_SHUTTLE_PIN_2_5 / 0x14
sensor5: MINI_SHUTTLE_PIN_2_6 / 0x15
sensor6: MINI_SHUTTLE_PIN_2_7 / 0x1d
sensor7: MINI_SHUTTLE_PIN_2_8 / 0x1e
```

## Current Test Result on My Board

On my board, sensors `s0` through `s4` initialized and streamed data.

Sensors `s5`, `s6`, and `s7` did not return the BME690 chip ID, so their CSV columns were blank.

This may be due to a hardware issue, board revision difference, connector issue, or different COINES pin mapping for the last three chip-select lines. Another user with the same board should test whether all eight sensors respond on their hardware.

## Troubleshooting

If the program cannot open the APP3 board:

- Check USB connection.
- Close other programs using the APP3 board.
- Replug the board.
- Check Linux USB permissions.

If only one sensor appears:

- Make sure this C reader is being used, not the older I2C reader.
- The 8x shuttle must be read using SPI chip-select pins, not only I2C address `0x76`.

If some sensors are blank:

- Check whether those sensors are populated on the shuttle board.
- Re-seat the shuttle board on the APP3 connector.
- Inspect the GPIO/CS lines for the missing sensors.
- Verify the board revision and pin mapping.
