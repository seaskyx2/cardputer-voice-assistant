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
  if ((uint32_t)(now - last_rssi_sample_ms) >= 1000 || last_rssi_sample_ms == 0) {
    last_rssi_sample_ms = now;
    for (int i = 0; i < RSSI_HISTORY_LEN - 1; i++) {
      rssi_history[i] = rssi_history[i + 1];
    }
    rssi_history[RSSI_HISTORY_LEN - 1] = rssi;
  }
}

// ===== 基于 ESPHome 官方内置 BMI270 组件 (含完整 8KB 微码校准) 的姿态手势与双轴电子水平仪 =====
struct ImuGestureTracker {
  float ax = 0.0f;
  float ay = 0.0f;
  float az = 1.0f;
  // 专供电子水平仪的高稳态低通滤波重力分量
  float lvl_ax = 0.0f;
  float lvl_ay = 0.0f;
  float lvl_az = 1.0f;
  // 相对归零校准偏移量 (Tare / Zeroing)
  bool zero_enabled = false;
  float zero_roll = 0.0f;
  float zero_pitch = 0.0f;
  float zero_edge = 0.0f;

  bool initialized = false;
  int face_down_cnt = 0;

  // 输入官方校准后的真实重力加速度 (单位: g)
  // 返回值：0=无手势, 1=翻扣桌面触发息屏静音, 2=拿起/翻正触发唤醒
  int on_accel(float nx, float ny, float nz, bool is_sleeping) {
    if (std::isnan(nx) || std::isnan(ny) || std::isnan(nz)) return 0;
    float prev_ax = ax, prev_ay = ay, prev_az = az;
    if (!initialized) {
      ax = nx; ay = ny; az = nz;
      lvl_ax = nx; lvl_ay = ny; lvl_az = nz;
      initialized = true;
      return 0;
    }
    ax = ax * 0.35f + nx * 0.65f;
    ay = ay * 0.35f + ny * 0.65f;
    az = az * 0.35f + nz * 0.65f;

    // 水平仪平滑二阶手感滤波 (兼顾丝滑跟手与 0.1° 静止稳定度)
    lvl_ax = lvl_ax * 0.72f + nx * 0.28f;
    lvl_ay = lvl_ay * 0.72f + ny * 0.28f;
    lvl_az = lvl_az * 0.72f + nz * 0.28f;

    // 手势 1：屏幕朝下翻扣桌面 (az < -0.78g 连续 5 次采样 ≈ 400ms @ 80ms interval)
    if (az < -0.78f) {
      face_down_cnt++;
      if (face_down_cnt == 5 && !is_sleeping) {
        return 1;
      }
    } else {
      face_down_cnt = 0;
      // 手势 2：休眠状态下被拿起或翻正
      if (is_sleeping && az > -0.35f) {
        float delta = std::abs(ax - prev_ax) + std::abs(ay - prev_ay) + std::abs(az - prev_az);
        if (delta > 0.20f) {
          return 2;
        }
      }
    }
    return 0;
  }

  float raw_roll_deg() const {
    float z = std::max(0.001f, std::abs(lvl_az));
    return std::atan2(lvl_ax, z) * 57.2957795f;
  }

  float raw_pitch_deg() const {
    float z = std::max(0.001f, std::abs(lvl_az));
    return std::atan2(lvl_ay, z) * 57.2957795f;
  }

  float raw_edge_deg() const {
    if (std::abs(lvl_ay) >= std::abs(lvl_ax)) {
      float y = std::max(0.001f, std::abs(lvl_ay));
      return std::atan2(lvl_ax, y) * 57.2957795f;
    } else {
      float x = std::max(0.001f, std::abs(lvl_ax));
      return std::atan2(lvl_ay, x) * 57.2957795f;
    }
  }

  void toggle_zero() {
    if (zero_enabled) {
      zero_enabled = false;
      zero_roll = 0.0f;
      zero_pitch = 0.0f;
      zero_edge = 0.0f;
    } else {
      zero_enabled = true;
      zero_roll = raw_roll_deg();
      zero_pitch = raw_pitch_deg();
      zero_edge = raw_edge_deg();
    }
  }

  float get_roll_deg() const { return raw_roll_deg() - (zero_enabled ? zero_roll : 0.0f); }
  float get_pitch_deg() const { return raw_pitch_deg() - (zero_enabled ? zero_pitch : 0.0f); }
  float get_edge_deg() const { return raw_edge_deg() - (zero_enabled ? zero_edge : 0.0f); }
  bool is_flat_mode() const { return std::abs(lvl_az) > 0.50f; }
  bool is_plumb_mode() const { return !is_flat_mode() && (std::abs(lvl_ax) > std::abs(lvl_ay)); }
};
static ImuGestureTracker imu_tracker;

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

// 严谨的单行 UTF-8 安全截断 (绝不劈裂多字节中文字符)
inline std::string truncate_utf8(const std::string &in, size_t max_bytes, bool ellipsis = true) {
  if (in.size() <= max_bytes) {
    return in;
  }
  size_t pos = max_bytes;
  while (pos > 0 && (static_cast<uint8_t>(in[pos]) & 0xC0) == 0x80) {
    pos--;
  }
  return ellipsis ? (in.substr(0, pos) + "...") : in.substr(0, pos);
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
