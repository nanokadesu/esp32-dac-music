#include "cJSON.h"
#include "driver/i2s.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_heap_caps.h"
#include "esp_websocket_client.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define WIFI_SSID "Ruijie-YGXL"
#define WIFI_PASS "qwer1234"
#define WS_URI "ws://192.168.18.100:8000/stream"
#define DEVICE_ID "insight"
#define SAMPLE_RATE 16000
#define I2S_NUM I2S_NUM_0
#define STREAM_WINDOW_SIZE 3
#define WS_RX_BUFFER_SIZE 16384
#define MAX_CHUNK_BYTES 12288
#define QUEUE_SIZE 3
#define TAG "ws_i2s"
#define MAX_REQUEST_RETRIES 5
#define MAX_RECONNECT_ATTEMPTS 30

typedef struct
{
    uint8_t *data;
    size_t len;
    uint32_t index;
} audio_chunk_t;

static audio_chunk_t q[QUEUE_SIZE];
static bool prefilled = false;
static int q_head, q_count;
static SemaphoreHandle_t q_mutex;
static esp_websocket_client_handle_t ws;
static bool end_seen = false, started = false, ws_starting = false, start_sent = false, playback_complete = false;
static uint32_t next_index = 0;
static uint32_t receive_index = 0;
static uint32_t next_request_index = STREAM_WINDOW_SIZE;
static size_t pending_len = 0;
static size_t pending_received = 0;
static uint8_t pending_data[MAX_CHUNK_BYTES];
static uint32_t pending_index = 0;
static bool pending_chunk = false;
static int request_failures = 0;
static int reconnect_attempts = 0;
static bool stream_failed = false;
static volatile bool ws_health_retry = false;
static esp_timer_handle_t ws_health_timer;
static volatile uint32_t playback_generation = 0;

static void wifi_init_sta(void)
{
    esp_netif_init();
    esp_event_loop_create_default();
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t c = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&c);
    esp_wifi_set_mode(WIFI_MODE_STA);
    wifi_config_t w = {0};
    strncpy((char *)w.sta.ssid, WIFI_SSID, 32);
    strncpy((char *)w.sta.password, WIFI_PASS, 64);
    esp_wifi_set_config(WIFI_IF_STA, &w);
    esp_wifi_start();
    esp_wifi_connect();
}

static bool enqueue(const uint8_t *d, size_t n, uint32_t idx)
{
    if (stream_failed || n == 0 || n > MAX_CHUNK_BYTES)
        return false;
    xSemaphoreTake(q_mutex, portMAX_DELAY);
    if (q_count >= QUEUE_SIZE)
    {
        ESP_LOGW(TAG, "Audio queue full count=%d idx=%" PRIu32, q_count, idx);
        xSemaphoreGive(q_mutex);
        return false;
    }
    int p = (q_head + q_count) % QUEUE_SIZE;
    q[p].data = malloc(n);
    if (!q[p].data)
    {
        ESP_LOGW(TAG, "PCM allocation failed idx=%" PRIu32 " bytes=%u free_heap=%u largest_block=%u",
                 idx, (unsigned)n, (unsigned)esp_get_free_heap_size(),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
        xSemaphoreGive(q_mutex);
        return false;
    }
    memcpy(q[p].data, d, n);
    q[p].len = n;
    q[p].index = idx;
    q_count++;
    int count = q_count;
    xSemaphoreGive(q_mutex);
    ESP_LOGI(TAG, "queued chunk %" PRIu32 " count=%d", idx, count);
    return true;
}

static bool dequeue(audio_chunk_t *out)
{
    xSemaphoreTake(q_mutex, portMAX_DELAY);
    if (!q_count)
    {
        xSemaphoreGive(q_mutex);
        return false;
    }
    *out = q[q_head];
    q[q_head].data = NULL;
    q_head = (q_head + 1) % QUEUE_SIZE;
    q_count--;
    xSemaphoreGive(q_mutex);
    return true;
}

static void clear_queue(void)
{
    xSemaphoreTake(q_mutex, portMAX_DELAY);
    while (q_count > 0)
    {
        free(q[q_head].data);
        q[q_head].data = NULL;
        q_head = (q_head + 1) % QUEUE_SIZE;
        q_count--;
    }
    xSemaphoreGive(q_mutex);
}

static void release_chunk(const audio_chunk_t *c)
{
    free(c->data);
}

static bool send_json(const char *s)
{
    if (ws)
    {
        int ret = esp_websocket_client_send_text(ws, s, strlen(s), pdMS_TO_TICKS(5000));
        if (ret < 0)
        {
            ESP_LOGW(TAG, "WebSocket send failed: ret=%d", ret);
            return false;
        }
        return true;
    }
    return false;
}

static void stop_audio_output(void)
{
    stream_failed = true;
    playback_generation++;
    prefilled = false;
    clear_queue();
    pending_chunk = false;
    pending_received = 0;
    stream_failed = true;
    i2s_zero_dma_buffer(I2S_NUM);
    i2s_stop(I2S_NUM);
    ESP_LOGI(TAG, "audio output stopped");
}

static void ws_health_timer_cb(void *arg)
{
    if (!ws || !esp_websocket_client_is_connected(ws))
        ws_health_retry = true;
}

static void request_next(uint32_t idx)
{
    if (!ws || !esp_websocket_client_is_connected(ws))
    {
        ESP_LOGI(TAG, "WebSocket unavailable, waiting for reconnect");
        return;
    }
    char b[64];
    snprintf(b, sizeof(b), "{\"type\":\"next\",\"index\":%" PRIu32 "}", idx);
    ESP_LOGI(TAG, "requesting next %" PRIu32, idx);
    bool ok = false;
    for (int attempt = 1; attempt <= MAX_REQUEST_RETRIES; ++attempt)
    {
        if (send_json(b)) { ok = true; break; }
        ESP_LOGI(TAG, "request retry %d/%d", attempt, MAX_REQUEST_RETRIES);
    }
    if (!ok) { request_failures++; stop_audio_output(); }
    else request_failures = 0;
}

static void websocket_event_handler(void *a, esp_event_base_t base, int32_t id, void *ev)
{
    esp_websocket_event_data_t *d = ev;
    if (id == WEBSOCKET_EVENT_CONNECTED)
    {
        playback_generation++;
        prefilled = false;
        start_sent = false;
        clear_queue();
        i2s_start(I2S_NUM);
        end_seen = false;
        pending_chunk = false;
        pending_received = 0;
        started = true; playback_complete = false; stream_failed = false; reconnect_attempts = 0; request_failures = 0; ws_starting = false; ESP_LOGI(TAG, "WebSocket connected");
        next_request_index = STREAM_WINDOW_SIZE;
        receive_index = 0;
        return;
    }
    if (id == WEBSOCKET_EVENT_DISCONNECTED)
    {
        started = false; start_sent = false; playback_complete = false; ws_starting = false;
        end_seen = false;
        pending_chunk = false;
        pending_received = 0;
        clear_queue();
        i2s_zero_dma_buffer(I2S_NUM);
        i2s_stop(I2S_NUM);
        ESP_LOGI(TAG, "WebSocket disconnected; audio state cleared");
        return;
    }
    if (id != WEBSOCKET_EVENT_DATA || d->data_len == 0)
        return;
    if (d->op_code == 0x1)
    {
        char t[160] = {0};
        size_t n = d->data_len < sizeof(t) - 1 ? d->data_len : sizeof(t) - 1;
        memcpy(t, d->data_ptr, n);
        cJSON *j = cJSON_Parse(t);
        if (!j)
            return;
        cJSON *type = cJSON_GetObjectItem(j, "type");
        ESP_LOGI(TAG, "WS control: %s", t);
        // if (cJSON_IsString(type))
        //     ESP_LOGI(TAG, "WS text: %s (%d bytes)", type->valuestring, (int)d->data_len);
        if (cJSON_IsString(type) && strcmp(type->valuestring, "chunk") == 0)
        {
            if (stream_failed || playback_complete)
            {
                cJSON_Delete(j);
                return;
            }
            cJSON *li = cJSON_GetObjectItem(j, "length");
            cJSON *ii = cJSON_GetObjectItem(j, "index");
            if (cJSON_IsNumber(li) && cJSON_IsNumber(ii) && li->valueint > 0 && li->valueint <= MAX_CHUNK_BYTES)
            {
                pending_len = li->valueint;
                pending_index = ii->valueint;
                if (pending_index < receive_index)
                {
                    /* Discard stale chunks left in the socket after a
                     *切歌/reconnect instead of mixing them into new audio. */
                    pending_chunk = false;
                    cJSON_Delete(j);
                    return;
                }
                /* Recover from a dropped/late frame instead of permanently
                 * rejecting the rest of the stream. */
                if (pending_index > receive_index)
                    receive_index = pending_index;
                pending_received = 0;
                pending_chunk = true;
            }
            else
            {
                ESP_LOGW(TAG, "Invalid chunk header: length must be 1..%d; restart updated Python server", MAX_CHUNK_BYTES);
                stop_audio_output();
            }
        }
        else if (cJSON_IsString(type) && strcmp(type->valuestring, "end") == 0)
            end_seen = true;
        else if (cJSON_IsString(type) &&
                 (strcmp(type->valuestring, "switch") == 0 ||
                  strcmp(type->valuestring, "play") == 0))
        {
            ESP_LOGI(TAG, "play command received, interrupting current audio");
            end_seen = false;
            playback_generation++;
            prefilled = false;
            stream_failed = false;
            playback_complete = false;
            pending_chunk = false;
            pending_received = 0;
            start_sent = false;
            next_request_index = STREAM_WINDOW_SIZE;
            receive_index = 0;
            clear_queue();
            /* Ignore any partially delivered old PCM frame. The next text
             * start/chunk sequence establishes the new stream. */
            pending_len = 0;
            i2s_zero_dma_buffer(I2S_NUM);
            i2s_start(I2S_NUM);
            char ack[64];
            snprintf(ack, sizeof(ack), "{\"type\":\"play_ack\"}");
            (void)send_json(ack);
        }
        cJSON_Delete(j);
    }
    else if ((d->op_code == 0x2 || d->op_code == 0x0) && pending_chunk)
    {
        if (stream_failed || playback_complete)
        {
            pending_chunk = false;
            pending_received = 0;
            return;
        }
        size_t remaining = pending_len - pending_received;
        size_t copy_len = d->data_len < remaining ? d->data_len : remaining;
        memcpy(pending_data + pending_received, d->data_ptr, copy_len);
        pending_received += copy_len;
        // ESP_LOGI(TAG, "WS binary part=%d received=%d/%d", (int)d->data_len,
        //          (int)pending_received, (int)pending_len);
        if (pending_received == pending_len)
        {
            if (enqueue(pending_data, pending_len, pending_index))
            {
                next_index = pending_index + 1;
                receive_index = pending_index + 1;
            }
            else
            {
                ESP_LOGW(TAG, "Audio chunk rejected idx=%" PRIu32 " count=%d free_heap=%u largest_block=%u",
                         pending_index, q_count, (unsigned)esp_get_free_heap_size(),
                         (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
                /* Do not advance the sequence on a lost chunk. Stop this
                 * stream cleanly so reconnect starts a fresh window instead
                 * of silently corrupting all subsequent audio. */
                stop_audio_output();
            }
            pending_chunk = false;
            pending_received = 0;
        }
    }
}

static void i2s_init_play(void)
{
    i2s_config_t c = {.mode = I2S_MODE_MASTER | I2S_MODE_TX, .sample_rate = SAMPLE_RATE, .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT, .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT, .communication_format = I2S_COMM_FORMAT_STAND_I2S, .dma_buf_count = 4, .dma_buf_len = 1024};
    i2s_pin_config_t p = {.bck_io_num = 26, .ws_io_num = 25, .data_out_num = 22, .data_in_num = I2S_PIN_NO_CHANGE};
    i2s_driver_install(I2S_NUM, &c, 0, NULL);
    i2s_set_pin(I2S_NUM, &p);
}

static void play_task(void *a)
{
    for (;;)
    {
        audio_chunk_t c;
        xSemaphoreTake(q_mutex, portMAX_DELAY);
        bool ready = prefilled || q_count >= STREAM_WINDOW_SIZE || end_seen;
        if (ready) prefilled = true;
        xSemaphoreGive(q_mutex);
        if (!ready || stream_failed) { vTaskDelay(pdMS_TO_TICKS(10)); continue; }
        if (!dequeue(&c))
        {
            if (end_seen)
            {
                ESP_LOGI(TAG, "playback complete");
                /* Release all residual playback state so the next play
                 * command starts from an empty queue and silent DMA. */
                clear_queue();
                pending_chunk = false;
                pending_received = 0;
                i2s_zero_dma_buffer(I2S_NUM);
                i2s_stop(I2S_NUM);
                end_seen = false;
                playback_complete = true;
                vTaskDelay(pdMS_TO_TICKS(100));
                continue;
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        size_t off = 0;
        uint32_t generation = playback_generation;
        if (stream_failed) { release_chunk(&c); continue; }
        while (off < c.len)
        {
            if (generation != playback_generation)
            {
                i2s_zero_dma_buffer(I2S_NUM);
                break;
            }
            size_t w = 0;
            i2s_write(I2S_NUM, c.data + off, c.len - off, &w, portMAX_DELAY);
            if (!w)
                break;
            off += w;
        }
        uint32_t done = c.index;
        release_chunk(&c);
        if (generation == playback_generation && !stream_failed && !end_seen)
            request_next(next_request_index++);
        if (generation == playback_generation && !stream_failed)
            ESP_LOGI(TAG, "played chunk %" PRIu32, done);
    }
}

void app_main(void)
{
    esp_err_t r = nvs_flash_init();
    if (r == ESP_ERR_NVS_NO_FREE_PAGES || r == ESP_ERR_NVS_NEW_VERSION_FOUND)
    {
        nvs_flash_erase();
        nvs_flash_init();
    }
    q_mutex = xSemaphoreCreateMutex();
    wifi_init_sta();
    i2s_init_play();
    xTaskCreate(play_task, "i2s_play", 4096, NULL, 5, NULL);
    esp_websocket_client_config_t c = {.uri = WS_URI, .buffer_size = WS_RX_BUFFER_SIZE, .disable_auto_reconnect = true};
    ws = esp_websocket_client_init(&c);
    esp_websocket_register_events(ws, WEBSOCKET_EVENT_ANY, websocket_event_handler, NULL);
    const esp_timer_create_args_t timer_args = {
        .callback = ws_health_timer_cb,
        .arg = NULL,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "ws_health"};
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &ws_health_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(ws_health_timer, 5000000));
    /* Allow Wi-Fi DHCP/static networking to finish before the first connect. */
    vTaskDelay(pdMS_TO_TICKS(5000));
    while (1)
    {
        if (ws_health_retry)
        {
            ws_health_retry = false;
            if (!esp_websocket_client_is_connected(ws))
                ESP_LOGI(TAG, "WebSocket health check: disconnected");
        }
        if (stream_failed && esp_websocket_client_is_connected(ws))
        {
            ESP_LOGI(TAG, "stream failed, closing stale WebSocket");
            esp_websocket_client_close(ws, pdMS_TO_TICKS(1000));
        }
        if (!playback_complete && !esp_websocket_client_is_connected(ws) && !ws_starting && reconnect_attempts < MAX_RECONNECT_ATTEMPTS)
        {
            ws_starting = true;
            reconnect_attempts++;
            ESP_LOGI(TAG, "silent reconnect %d/%d", reconnect_attempts, MAX_RECONNECT_ATTEMPTS);
            esp_websocket_client_start(ws);
        }
        if (!esp_websocket_client_is_connected(ws) && reconnect_attempts >= MAX_RECONNECT_ATTEMPTS && !stream_failed)
        {
            ESP_LOGI(TAG, "reconnect limit reached, closing WebSocket");
            stop_audio_output();
            esp_websocket_client_close(ws, pdMS_TO_TICKS(1000));
        }
        if (started && !start_sent && esp_websocket_client_is_connected(ws))
        {
            char b[64];
            snprintf(b, sizeof(b), "{\"type\":\"start\",\"device\":\"%s\",\"window\":%d}", DEVICE_ID, STREAM_WINDOW_SIZE);
            if (send_json(b))
            {
                start_sent = true;
                ESP_LOGI(TAG, "start request sent");
            }
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}







