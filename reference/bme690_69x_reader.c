#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "bme69x.h"

#define COINES_SUCCESS 0
#define COINES_COMM_INTF_USB 0
#define COINES_SPI_BUS_0 0
#define COINES_SPI_SPEED_1_MHZ 60
#define COINES_SPI_MODE0 0
#define SENSOR_COUNT 8
#define MAX_CONSECUTIVE_ERRORS 5
#define RETRY_DELAY_US 200000

#define MINI_SHUTTLE_PIN_1_4 0x10
#define MINI_SHUTTLE_PIN_1_5 0x11
#define MINI_SHUTTLE_PIN_1_6 0x12
#define MINI_SHUTTLE_PIN_1_7 0x13
#define MINI_SHUTTLE_PIN_2_5 0x14
#define MINI_SHUTTLE_PIN_2_6 0x15
#define MINI_SHUTTLE_PIN_2_7 0x1d
#define MINI_SHUTTLE_PIN_2_8 0x1e

int16_t coines_open_comm_intf(uint8_t intf, void *arg);
int16_t coines_close_comm_intf(uint8_t intf, void *arg);
int16_t coines_set_shuttleboard_vdd_vddio_config(uint16_t vdd_mv, uint16_t vddio_mv);
int16_t coines_config_spi_bus(uint8_t bus, uint32_t spi_speed, uint8_t spi_mode);
int16_t coines_deconfig_spi_bus(uint8_t bus);
int8_t coines_read_spi(uint8_t bus, uint8_t cs_pin, uint8_t reg_addr, uint8_t *reg_data, uint16_t len);
int8_t coines_write_spi(uint8_t bus, uint8_t cs_pin, uint8_t reg_addr, uint8_t *reg_data, uint16_t len);
void coines_delay_usec(uint32_t period);

struct sensor_ctx {
    uint8_t cs_pin;
    struct bme69x_dev dev;
    struct bme69x_conf conf;
    struct bme69x_heatr_conf heatr_conf;
    int initialized;
    int consecutive_errors;
};

static volatile sig_atomic_t keep_running = 1;

static const uint8_t sensor_cs_pins[SENSOR_COUNT] = {
    MINI_SHUTTLE_PIN_1_4,
    MINI_SHUTTLE_PIN_1_5,
    MINI_SHUTTLE_PIN_1_6,
    MINI_SHUTTLE_PIN_1_7,
    MINI_SHUTTLE_PIN_2_5,
    MINI_SHUTTLE_PIN_2_6,
    MINI_SHUTTLE_PIN_2_7,
    MINI_SHUTTLE_PIN_2_8,
};

static void stop_running(int signum)
{
    (void)signum;
    keep_running = 0;
}

static BME69X_INTF_RET_TYPE app3_spi_read(uint8_t reg_addr, uint8_t *reg_data, uint32_t length, void *intf_ptr)
{
    uint8_t cs_pin = *(uint8_t *)intf_ptr;
    return coines_read_spi(COINES_SPI_BUS_0, cs_pin, reg_addr, reg_data, (uint16_t)length);
}

static BME69X_INTF_RET_TYPE app3_spi_write(uint8_t reg_addr, const uint8_t *reg_data, uint32_t length, void *intf_ptr)
{
    uint8_t cs_pin = *(uint8_t *)intf_ptr;
    return coines_write_spi(COINES_SPI_BUS_0, cs_pin, reg_addr, (uint8_t *)reg_data, (uint16_t)length);
}

static void app3_delay_us(uint32_t period, void *intf_ptr)
{
    (void)intf_ptr;
    coines_delay_usec(period);
}

static void print_result(const char *step, int result)
{
    if (result != COINES_SUCCESS) {
        fprintf(stderr, "%s failed: %d\n", step, result);
    }
}

static int init_sensor(struct sensor_ctx *sensor, int index)
{
    int8_t rslt;

    memset(sensor, 0, sizeof(*sensor));
    sensor->cs_pin = sensor_cs_pins[index];
    sensor->dev.intf = BME69X_SPI_INTF;
    sensor->dev.intf_ptr = &sensor->cs_pin;
    sensor->dev.read = app3_spi_read;
    sensor->dev.write = app3_spi_write;
    sensor->dev.delay_us = app3_delay_us;
    sensor->dev.amb_temp = 25;

    rslt = bme69x_init(&sensor->dev);
    if (rslt != BME69X_OK) {
        fprintf(stderr, "sensor%d bme69x_init failed on CS 0x%02x: %d\n", index, sensor->cs_pin, rslt);
        return 0;
    }

    sensor->conf.os_hum = BME69X_OS_2X;
    sensor->conf.os_pres = BME69X_OS_16X;
    sensor->conf.os_temp = BME69X_OS_1X;
    sensor->conf.filter = BME69X_FILTER_OFF;
    sensor->conf.odr = BME69X_ODR_NONE;
    rslt = bme69x_set_conf(&sensor->conf, &sensor->dev);
    if (rslt != BME69X_OK) {
        fprintf(stderr, "sensor%d bme69x_set_conf failed: %d\n", index, rslt);
        return 0;
    }

    sensor->heatr_conf.enable = BME69X_ENABLE;
    sensor->heatr_conf.heatr_temp = 300;
    sensor->heatr_conf.heatr_dur = 100;
    rslt = bme69x_set_heatr_conf(BME69X_FORCED_MODE, &sensor->heatr_conf, &sensor->dev);
    if (rslt != BME69X_OK) {
        fprintf(stderr, "sensor%d bme69x_set_heatr_conf failed: %d\n", index, rslt);
        return 0;
    }

    sensor->initialized = 1;
    fprintf(stderr, "sensor%d initialized on CS 0x%02x, variant %u\n", index, sensor->cs_pin, sensor->dev.variant_id);
    return 1;
}

static void print_header(void)
{
    printf("time");
    for (int i = 0; i < SENSOR_COUNT; i++) {
        printf(",s%d_temp_c,s%d_humidity_pct,s%d_pressure_hpa,s%d_gas_ohm,s%d_status,s%d_variant", i, i, i, i, i, i);
    }
    printf("\n");
    fflush(stdout);
}

int main(void)
{
    signal(SIGINT, stop_running);
    signal(SIGTERM, stop_running);

    int result = coines_open_comm_intf(COINES_COMM_INTF_USB, NULL);
    if (result != COINES_SUCCESS) {
        print_result("coines_open_comm_intf", result);
        return 1;
    }

    result = coines_set_shuttleboard_vdd_vddio_config(3300, 3300);
    if (result != COINES_SUCCESS) {
        print_result("coines_set_shuttleboard_vdd_vddio_config", result);
        coines_close_comm_intf(COINES_COMM_INTF_USB, NULL);
        return 1;
    }
    usleep(200000);

    coines_deconfig_spi_bus(COINES_SPI_BUS_0);
    result = coines_config_spi_bus(COINES_SPI_BUS_0, COINES_SPI_SPEED_1_MHZ, COINES_SPI_MODE0);
    if (result != COINES_SUCCESS) {
        print_result("coines_config_spi_bus", result);
        coines_set_shuttleboard_vdd_vddio_config(0, 0);
        coines_close_comm_intf(COINES_COMM_INTF_USB, NULL);
        return 1;
    }

    struct sensor_ctx sensors[SENSOR_COUNT];
    int active = 0;
    for (int i = 0; i < SENSOR_COUNT; i++) {
        active += init_sensor(&sensors[i], i);
    }

    if (active == 0) {
        fprintf(stderr, "No BME690 sensors initialized over SPI.\n");
        coines_set_shuttleboard_vdd_vddio_config(0, 0);
        coines_close_comm_intf(COINES_COMM_INTF_USB, NULL);
        return 1;
    }

    print_header();

    while (keep_running) {
        struct bme69x_data data[SENSOR_COUNT];
        uint8_t valid[SENSOR_COUNT] = { 0 };

        for (int i = 0; i < SENSOR_COUNT; i++) {
            if (!sensors[i].initialized) {
                continue;
            }

            int8_t rslt = bme69x_set_op_mode(BME69X_FORCED_MODE, &sensors[i].dev);
            if (rslt != BME69X_OK) {
                fprintf(stderr, "sensor%d bme69x_set_op_mode failed: %d\n", i, rslt);
                sensors[i].consecutive_errors++;
                continue;
            }
        }

        uint32_t delay_us = bme69x_get_meas_dur(BME69X_FORCED_MODE, &sensors[0].conf, &sensors[0].dev) +
                            (sensors[0].heatr_conf.heatr_dur * 1000);
        coines_delay_usec(delay_us);

        for (int i = 0; i < SENSOR_COUNT; i++) {
            if (!sensors[i].initialized) {
                continue;
            }

            uint8_t n_fields = 0;
            int8_t rslt = bme69x_get_data(BME69X_FORCED_MODE, &data[i], &n_fields, &sensors[i].dev);
            if (rslt == BME69X_OK && n_fields > 0) {
                valid[i] = 1;
                sensors[i].consecutive_errors = 0;
            } else {
                fprintf(stderr, "sensor%d bme69x_get_data failed/no data: %d\n", i, rslt);
                sensors[i].consecutive_errors++;
                if (sensors[i].consecutive_errors >= MAX_CONSECUTIVE_ERRORS) {
                    fprintf(stderr, "sensor%d disabled after %d consecutive errors\n", i, MAX_CONSECUTIVE_ERRORS);
                    sensors[i].initialized = 0;
                }
                usleep(RETRY_DELAY_US);
            }
        }

        time_t now = time(NULL);
        struct tm tm_now;
        localtime_r(&now, &tm_now);
        char ts[32];
        strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", &tm_now);
        printf("%s", ts);

        for (int i = 0; i < SENSOR_COUNT; i++) {
            if (valid[i]) {
                printf(",%.2f,%.2f,%.2f,%.0f,0x%02x,%u",
                       data[i].temperature,
                       data[i].humidity,
                       data[i].pressure / 100.0f,
                       data[i].gas_resistance,
                       data[i].status,
                       sensors[i].dev.variant_id);
            } else {
                printf(",,,,,,");
            }
        }
        printf("\n");
        fflush(stdout);
        sleep(1);
    }

    coines_set_shuttleboard_vdd_vddio_config(0, 0);
    coines_close_comm_intf(COINES_COMM_INTF_USB, NULL);
    return 0;
}
