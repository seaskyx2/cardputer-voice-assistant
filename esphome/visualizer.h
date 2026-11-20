#pragma once
#include "esphome.h"
#include <vector>
#include <cmath>
#include <string>
#include <algorithm>

namespace visualizer {

static const int NUM_BARS = 18;
static float bar_heights[NUM_BARS] = {0.0f};
static float current_energy = 0.0f;
static uint32_t think_start_ms = 0;

// ===== Wi-Fi 射频历史采样缓存 (用于绘制真实雷达波动折线图 Sparkline) =====
static const int RSSI_HISTORY_LEN = 16;
static float rssi_history[RSSI_HISTORY_LEN] = { -60.0f, -60.0f, -60.0f, -60.0f,
                                                -60.0f, -60.0f, -60.0f, -60.0f,
                                                -60.0f, -60.0f, -60.0f, -60.0f,
                                                -60.0f, -60.0f, -60.0f, -60.0f };
static uint32_t last_rssi_sample_ms = 0;

inline void record_rssi(float rssi, uint32_t now) {
  if (std::isnan(rssi) || rssi > 0.0f || rssi < -120.0f) return;
  if (now - last_rssi_sample_ms >= 1000 || last_rssi_sample_ms == 0) {
    last_rssi_sample_ms = now;
    for (int i = 0; i < RSSI_HISTORY_LEN - 1; i++) {
      rssi_history[i] = rssi_history[i + 1];
    }
    rssi_history[RSSI_HISTORY_LEN - 1] = rssi;
  }
}

// ===== 真实 Bosch BMI270 6 轴 IMU 轻量驱动 (I2C 0x69) =====
struct BMI270State {
  bool ready = false;
  uint8_t chip_id = 0;
  float ax = 0.0f;
  float ay = 0.0f;
  float az = 1.0f;
  float prev_ax = 0.0f;
  float prev_ay = 0.0f;
  float prev_az = 1.0f;
  int face_down_cnt = 0;
  uint32_t last_poll_ms = 0;

  bool write_reg(esphome::i2c::I2CBus *bus, uint8_t reg, uint8_t val) {
    uint8_t buf[2] = {reg, val};
    return bus->write(0x69, buf, 2) == esphome::i2c::ERROR_OK;
  }

  bool read_regs(esphome::i2c::I2CBus *bus, uint8_t reg, uint8_t *dst, size_t len) {
    if (bus->write(0x69, &reg, 1, false) != esphome::i2c::ERROR_OK) return false;
    return bus->read(0x69, dst, len) == esphome::i2c::ERROR_OK;
  }

  void init(esphome::i2c::I2CBus *bus) {
    if (!bus) return;
    uint8_t id = 0;
    if (!read_regs(bus, 0x00, &id, 1)) {
      ESP_LOGW("bmi270", "Failed to read BMI270 CHIP_ID at 0x69");
      return;
    }
    chip_id = id;
    // 关闭高级省电模式，开启 100Hz ±2g 加速度计
    write_reg(bus, 0x7C, 0x00); // PWR_CONF: adv_power_save = 0
    delay(2);
    write_reg(bus, 0x40, 0xA8); // ACC_CONF: 100Hz, normal
    write_reg(bus, 0x41, 0x00); // ACC_RANGE: +/-2g (16384 LSB/g)
    write_reg(bus, 0x7D, 0x04); // PWR_CTRL: acc_en = 1
    delay(2);
    ready = (id == 0x24);
    ESP_LOGI("bmi270", "BMI270 init done: chip_id=0x%02X ready=%s", id, ready ? "true" : "false");
  }

  // 返回值：0=无手势, 1=翻扣桌面触发息屏, 2=拿起/翻正触发唤醒
  int poll(esphome::i2c::I2CBus *bus, bool is_sleeping) {
    if (!ready || !bus) return 0;
    uint32_t now = millis();
    if (now - last_poll_ms < 200) return 0;
    last_poll_ms = now;

    uint8_t raw[6] = {0};
    if (!read_regs(bus, 0x0C, raw, 6)) return 0;

    int16_t rx = (int16_t)((raw[1] << 8) | raw[0]);
    int16_t ry = (int16_t)((raw[3] << 8) | raw[2]);
    int16_t rz = (int16_t)((raw[5] << 8) | raw[4]);

    prev_ax = ax; prev_ay = ay; prev_az = az;
    // 一阶低通滤波平滑抖动
    float nx = rx / 16384.0f;
    float ny = ry / 16384.0f;
    float nz = rz / 16384.0f;
    ax = ax * 0.4f + nx * 0.6f;
    ay = ay * 0.4f + ny * 0.6f;
    az = az * 0.4f + nz * 0.6f;

    // 手势 1：屏幕朝下翻扣在桌面 (az < -0.78g 持续 2 次采样 = 400ms)
    if (az < -0.78f) {
      face_down_cnt++;
      if (face_down_cnt == 2 && !is_sleeping) {
        return 1; // 触发翻扣息屏静音
      }
    } else {
      face_down_cnt = 0;
      // 手势 2：休眠状态下被拿起或翻正 (加速度变化显著且非倒扣)
      if (is_sleeping && az > -0.35f) {
        float delta = std::abs(ax - prev_ax) + std::abs(ay - prev_ay) + std::abs(az - prev_az);
        if (delta > 0.22f) {
          return 2; // 触发拿起唤醒
        }
      }
    }
    return 0;
  }
};
static BMI270State imu;

// 麦克风原始 PCM 采样计算 RMS 均方根音量 (每秒被 I2S 回调触发多次)
inline void on_mic_data(const std::vector<uint8_t> &data) {
  if (data.empty()) return;

  const int32_t *samples = reinterpret_cast<const int32_t*>(data.data());
  size_t count = data.size() / sizeof(int32_t);
  if (count == 0) return;

  int64_t sum_sq = 0;
  size_t step = (count > 64) ? (count / 64) : 1;
  size_t sampled = 0;

  for (size_t i = 0; i < count; i += step) {
    int32_t val = samples[i] >> 16;
    sum_sq += (int64_t)val * val;
    sampled++;
  }

  if (sampled == 0) return;
  float rms = std::sqrt((float)sum_sq / sampled);

  float energy = (rms - 180.0f) / 5500.0f;
  if (energy < 0.0f) energy = 0.0f;
  if (energy > 1.0f) energy = 1.0f;

  current_energy = current_energy * 0.35f + energy * 0.65f;
}

// 屏幕刷新每帧更新波形柱 (10 FPS)
inline void update_bars(bool is_listening) {
  if (!is_listening) {
    for (int i = 0; i < NUM_BARS; i++) {
      bar_heights[i] *= 0.65f;
      if (bar_heights[i] < 0.5f) bar_heights[i] = 0.0f;
    }
    current_energy = 0.0f;
    return;
  }

  int half = NUM_BARS / 2;
  for (int i = 0; i < half; i++) {
    float dist = (float)(i + 1) / half;
    float factor = 0.35f + 0.65f * dist;
    float jitter = ((rand() % 30) - 15) / 100.0f;
    float target = current_energy * 30.0f * (factor + jitter);
    if (target < 2.0f && current_energy > 0.04f) target = 2.0f;
    if (target > 30.0f) target = 30.0f;

    bar_heights[i] = bar_heights[i] * 0.45f + target * 0.55f;
    bar_heights[NUM_BARS - 1 - i] = bar_heights[i];
  }
}

// 思考态光效位置计算 (在 width 像素范围内平滑来回穿梭)
inline int get_scanner_x(int width, uint32_t ms) {
  if (width <= 0) return 0;
  int period = 1200;
  int phase = ms % period;
  if (phase > period / 2) {
    phase = period - phase;
  }
  return (phase * 2 * width) / period;
}

// 严谨的 UTF-8 中文双行平滑拆分算法 (防止汉字字节腰斩乱码)
inline void split_cn_text(const std::string &in, std::string &l1, std::string &l2, size_t max_bytes = 36) {
  if (in.size() <= max_bytes) {
    l1 = in;
    l2 = "";
    return;
  }
  size_t pos = max_bytes;
  while (pos > 0 && (static_cast<uint8_t>(in[pos]) & 0xC0) == 0x80) {
    pos--;
  }
  l1 = in.substr(0, pos);
  std::string rem = in.substr(pos);
  if (rem.size() <= max_bytes) {
    l2 = rem;
  } else {
    size_t p2 = max_bytes;
    while (p2 > 0 && (static_cast<uint8_t>(rem[p2]) & 0xC0) == 0x80) {
      p2--;
    }
    l2 = rem.substr(0, p2) + "...";
  }
}

} // namespace visualizer
