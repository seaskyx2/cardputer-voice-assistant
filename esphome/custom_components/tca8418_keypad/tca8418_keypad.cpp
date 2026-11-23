#include "tca8418_keypad.h"

#include <algorithm>

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome {
namespace tca8418_keypad {

static const char *const TAG = "tca8418_keypad";

static const uint8_t REG_CFG = 0x01;
static const uint8_t REG_INT_STAT = 0x02;
static const uint8_t REG_KEY_LCK_EC = 0x03;
static const uint8_t REG_KEY_EVENT_A = 0x04;
static const uint8_t REG_KP_GPIO1 = 0x1D;
static const uint8_t REG_KP_GPIO2 = 0x1E;
static const uint8_t REG_KP_GPIO3 = 0x1F;

void TCA8418Keypad::setup() {
  if (this->interrupt_pin_ != nullptr) {
    this->interrupt_pin_->setup();
    this->interrupt_pin_->pin_mode(gpio::FLAG_INPUT | gpio::FLAG_PULLUP);
  }

  const size_t key_slots = static_cast<size_t>(this->rows_) * this->columns_;
  this->key_pressed_state_.assign(key_slots, false);
  this->key_last_event_ms_.assign(key_slots, 0);
  this->key_press_start_ms_.assign(key_slots, 0);

  if (!this->configure_keypad_()) {
    ESP_LOGE(TAG, "Failed to configure TCA8418 keypad controller");
    this->mark_failed();
    return;
  }

  if (this->interrupt_pin_ != nullptr) {
    this->interrupt_pin_->attach_interrupt(&TCA8418Keypad::gpio_intr_, this, gpio::INTERRUPT_FALLING_EDGE);
    if (!this->interrupt_pin_->digital_read()) {
      this->interrupt_pending_ = true;
    }
    ESP_LOGI(TAG, "Interrupt + level-guard enabled on pin %d", this->interrupt_pin_->get_pin());
  }
}

void TCA8418Keypad::loop() {
  const uint32_t now = millis();
  if (this->interrupt_pin_ != nullptr) {
    // 1. 0ns 本地 GPIO 电平检查 + 边沿中断标志（彻底根除边沿丢失与 disable_loop 死锁）
    const bool pin_active_low = !this->interrupt_pin_->digital_read();
    const bool watchdog_due = (uint32_t)(now - this->last_watchdog_poll_ms_) >= 250;
    if (!this->interrupt_pending_ && !pin_active_low && !watchdog_due) {
      return;
    }
    this->last_watchdog_poll_ms_ = now;
    this->process_events_();
    return;
  }
  if ((uint32_t)(now - this->last_watchdog_poll_ms_) >= 20) {
    this->last_watchdog_poll_ms_ = now;
    this->process_events_();
  }
}

void TCA8418Keypad::process_events_() {
  this->interrupt_pending_ = false;

  // 先在微秒内一次性抽干 TCA8418 硬件 FIFO 到本地栈缓存，立即清中断释放 GPIO11，
  // 绝不在 FIFO 未清空前执行耗时的 UI 刷新回调（防止 Release 事件卡死 K_INT 低电平）
  uint8_t queued[24];
  size_t queued_len = 0;

  for (int round = 0; round < 3; round++) {
    uint8_t key_lck_ec = 0;
    if (!this->read_byte(REG_KEY_LCK_EC, &key_lck_ec)) {
      return;
    }
    const uint8_t event_count = key_lck_ec & 0x0F;
    if (event_count == 0) {
      break;
    }
    const uint8_t to_read = std::min<uint8_t>(event_count, 10);
    for (uint8_t i = 0; i < to_read; i++) {
      uint8_t ev = 0;
      if (!this->read_byte(REG_KEY_EVENT_A, &ev)) {
        break;
      }
      if ((ev & 0x7F) != 0 && queued_len < sizeof(queued)) {
        queued[queued_len++] = ev;
      }
    }
  }

  // 检查并无条件清除所有挂起的中断标志 (K_INT / OVR_FLOW_INT / GPI_INT)
  uint8_t int_stat = 0;
  if (this->read_byte(REG_INT_STAT, &int_stat) && (int_stat & 0x1F) != 0) {
    this->write_byte(REG_INT_STAT, 0x1F);
  } else if (queued_len > 0) {
    this->write_byte(REG_INT_STAT, 0x1F);
  }

  // FIFO 已彻底排空且 INT 脚已释放，安全分发按键回调
  for (size_t i = 0; i < queued_len; i++) {
    this->handle_event_(queued[i]);
  }
}

void IRAM_ATTR TCA8418Keypad::gpio_intr_(TCA8418Keypad *self) {
  self->interrupt_pending_ = true;
}

void TCA8418Keypad::dump_config() {
  ESP_LOGCONFIG(TAG, "TCA8418 Keypad:");
  LOG_I2C_DEVICE(this);
  ESP_LOGCONFIG(TAG, "  Rows: %u", this->rows_);
  ESP_LOGCONFIG(TAG, "  Columns: %u", this->columns_);
  if (this->interrupt_pin_ != nullptr) {
    LOG_PIN("  Interrupt Pin: ", this->interrupt_pin_);
  }
  ESP_LOGCONFIG(TAG, "  Debounce: %u ms", this->debounce_ms_);
  ESP_LOGCONFIG(TAG, "  Long Press Threshold: %u ms", this->long_press_ms_);
}

bool TCA8418Keypad::configure_keypad_() {
  if (this->rows_ == 0 || this->columns_ == 0) {
    ESP_LOGE(TAG, "rows and columns must be configured");
    return false;
  }

  const uint8_t kp_gpio1 = static_cast<uint8_t>((1U << this->rows_) - 1U);
  const uint8_t kp_gpio2 =
      this->columns_ <= 8 ? static_cast<uint8_t>((1U << this->columns_) - 1U) : 0xFF;
  const uint8_t kp_gpio3 =
      this->columns_ > 8 ? static_cast<uint8_t>((1U << (this->columns_ - 8U)) - 1U) : 0x00;

  if (!this->write_byte(REG_KP_GPIO1, kp_gpio1)) {
    return false;
  }
  if (!this->write_byte(REG_KP_GPIO2, kp_gpio2)) {
    return false;
  }
  if (!this->write_byte(REG_KP_GPIO3, kp_gpio3)) {
    return false;
  }

  // REG_CFG: AI=0 (读取 FIFO 固定地址 REG_KEY_EVENT_A=0x04！旧代码写 0x81 开 auto-increment 还写 REG_KEY_EVENT_A + i 双重递增跳过了一半按键事件！！)
  // Bit 0 (KE_IEN) = 1: 开启按键事件中断；Bit 7 (AI) = 0: 严禁开启地址自增，否则连续读 FIFO 会跳到其他寄存器！
  if (!this->write_byte(REG_CFG, 0x01)) {
    return false;
  }

  // 排空历史残留 FIFO 事件并清除所有中断位
  for (int i = 0; i < 12; i++) {
    uint8_t dummy = 0;
    if (!this->read_byte(REG_KEY_EVENT_A, &dummy) || dummy == 0) {
      break;
    }
  }
  if (!this->write_byte(REG_INT_STAT, 0x1F)) {
    return false;
  }

  return true;
}

void TCA8418Keypad::handle_event_(uint8_t event) {
  const uint32_t now = millis();
  const bool pressed = (event & 0x80) != 0;
  const uint8_t keycode = event & 0x7F;

  if (keycode == 0) {
    return;
  }

  const uint8_t key_index = keycode - 1;
  const uint8_t row = key_index / 10;
  const uint8_t col = key_index % 10;

  if (row >= this->rows_ || col >= this->columns_) {
    ESP_LOGW(TAG, "Ignoring out-of-range key event code=%u row=%u col=%u", keycode, row, col);
    return;
  }

  const char key_char = this->lookup_key_(keycode, row, col);
  const size_t index = static_cast<size_t>(row) * this->columns_ + col;

  if (index < this->key_last_event_ms_.size() && this->debounce_ms_ > 0) {
    if ((uint32_t)(now - this->key_last_event_ms_[index]) < this->debounce_ms_) {
      return;
    }
  }

  bool long_press = false;
  if (index < this->key_pressed_state_.size()) {
    if (pressed) {
      this->key_pressed_state_[index] = true;
      this->key_press_start_ms_[index] = now;
    } else {
      if (this->key_pressed_state_[index] && this->long_press_ms_ > 0) {
        long_press = (uint32_t)(now - this->key_press_start_ms_[index]) >= this->long_press_ms_;
      }
      this->key_pressed_state_[index] = false;
    }
    this->key_last_event_ms_[index] = now;
  }

  ESP_LOGI(TAG, "Key row=%u col=%u code=%u pressed=%d", row, col, keycode, pressed ? 1 : 0);

  for (TCA8418KeypadListener *listener : this->listeners_) {
    if (pressed) {
      listener->button_pressed(row, col);
      listener->keycode_pressed(keycode);
    } else {
      listener->button_released(row, col);
      listener->keycode_released(keycode);
    }
  }

  for (TCA8418KeyEventTrigger *trigger : this->key_event_triggers_) {
    trigger->trigger(row, col, pressed, key_char, keycode, long_press);
  }
}

char TCA8418Keypad::lookup_key_(uint8_t keycode, uint8_t row, uint8_t col) const {
  if (keycode < this->keymap_by_code_.size() && this->keymap_by_code_[keycode] != 0) {
    return this->keymap_by_code_[keycode];
  }
  return 0x00;
}

}  // namespace tca8418_keypad
}  // namespace esphome
