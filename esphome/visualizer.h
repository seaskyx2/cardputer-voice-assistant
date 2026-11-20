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
