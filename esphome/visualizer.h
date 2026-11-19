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
static std::string last_stt_text = "";
static std::string last_tts_text = "";

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

  // 灵敏度动态归一化 (0.0 ~ 1.0)
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

  // 模拟从中央向两侧扩散的立体动感频谱
  int half = NUM_BARS / 2;
  for (int i = 0; i < half; i++) {
    float dist = (float)(i + 1) / half; // 边缘到中心
    float factor = 0.35f + 0.65f * dist;
    float jitter = ((rand() % 30) - 15) / 100.0f;
    float target = current_energy * 30.0f * (factor + jitter);
    if (target < 2.0f && current_energy > 0.04f) target = 2.0f;
    if (target > 30.0f) target = 30.0f;

    // 平滑上升与衰减
    bar_heights[i] = bar_heights[i] * 0.45f + target * 0.55f;
    bar_heights[NUM_BARS - 1 - i] = bar_heights[i];
  }
}

// 思考态光效位置计算 (在 width 像素范围内平滑来回穿梭)
inline int get_scanner_x(int width, uint32_t ms) {
  if (width <= 0) return 0;
  int period = 1200; // 1.2秒往返一次
  int phase = ms % period;
  if (phase > period / 2) {
    phase = period - phase;
  }
  return (phase * 2 * width) / period;
}

} // namespace visualizer
