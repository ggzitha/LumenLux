"""
WS2812B NeoPixel Manager with hardware control and thread-safe animation loop.
Supports all 56 WS2812FX effects with single-writer hardware thread safety.
"""
import logging
import threading
import time
from effects import EffectRunner, EFFECT_NAMES

logger = logging.getLogger("led_manager")

DEFAULT_LED_COUNT = 100
DEFAULT_LED_PIN = 18  # GPIO 18 (PWM)

PRESETS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
    "off": (0, 0, 0)
}

class LEDManager:
    def __init__(self, count: int = DEFAULT_LED_COUNT, pin: int = DEFAULT_LED_PIN):
        self.count = count
        self.pin_num = pin
        self.pixels = None
        self.is_hardware = False
        
        # State: default power ON, white color, 1.0 brightness, static effect
        self.power = True
        self.color = (255, 255, 255)
        self.brightness = 1.0
        self.effect = "static"
        self.speed = 50  # milliseconds per frame (15 - 500)
        
        self.runner = EffectRunner(count=self.count)
        self.lock = threading.Lock()
        self.running = True
        self.state_changed = threading.Event()
        self.worker_thread = None

        self._init_hardware()
        self._start_worker()

    def _init_hardware(self):
        """Attempt to initialize neopixel hardware, fallback to virtual mode."""
        try:
            import board
            import neopixel

            pin_obj = getattr(board, f"D{self.pin_num}", board.D18)
            self.pixels = neopixel.NeoPixel(
                pin_obj,
                self.count,
                brightness=self.brightness,
                auto_write=False,
                pixel_order=neopixel.GRB
            )
            self.is_hardware = True
            logger.info("Hardware NeoPixel initialized on GPIO%d with %d LEDs", self.pin_num, self.count)
        except Exception as e:
            self.is_hardware = False
            self.pixels = None
            logger.warning("Hardware NeoPixel unavailable (%s). Running in virtual/mock mode.", e)

    def _start_worker(self):
        self.worker_thread = threading.Thread(target=self._run_loop, name="LEDWorker", daemon=True)
        self.worker_thread.start()

    def _write_hardware(self, fill_color=None, frame_colors=None, brightness=1.0):
        """Single-writer hardware access method."""
        if not self.is_hardware or self.pixels is None:
            return
        try:
            b_clamped = max(0.0, min(1.0, float(brightness)))
            if self.pixels.brightness != b_clamped:
                self.pixels.brightness = b_clamped
            if fill_color is not None:
                self.pixels.fill(fill_color)
            elif frame_colors is not None:
                for i, col in enumerate(frame_colors[:self.count]):
                    self.pixels[i] = col
            self.pixels.show()
        except Exception as e:
            logger.error("Failed to render LED hardware: %s. Re-initializing...", e)
            try:
                self._init_hardware()
            except Exception:
                pass

    def _run_loop(self):
        """
        Background thread handling all hardware writes.
        Guarantees that pixels.show() is NEVER called concurrently from multiple threads,
        preventing ws2811 DMA collisions and deadlocks.
        """
        logger.info("LED hardware rendering worker started.")
        last_rendered_key = None

        while self.running:
            with self.lock:
                power = self.power
                color = self.color
                brightness = self.brightness
                effect = self.effect
                speed_ms = self.speed

            if not power:
                state_key = ("off",)
                if last_rendered_key != state_key:
                    self._write_hardware(fill_color=(0, 0, 0), brightness=0.0)
                    last_rendered_key = state_key

                # Sleep waiting for next state change
                self.state_changed.wait(timeout=0.5)
                self.state_changed.clear()
                continue

            if effect == "static":
                state_key = ("static", color, brightness)
                if last_rendered_key != state_key:
                    self._write_hardware(fill_color=color, brightness=brightness)
                    last_rendered_key = state_key

                # Sleep waiting for next state change
                self.state_changed.wait(timeout=0.5)
                self.state_changed.clear()
                continue

            # Dynamic animated effect (WS2812FX)
            last_rendered_key = None
            frame = self.runner.get_frame(effect, color, speed_ms)
            self._write_hardware(frame_colors=frame, brightness=brightness)

            # Frame delay, waking immediately if state changed
            sleep_sec = max(0.015, min(0.5, speed_ms / 1000.0))
            if self.state_changed.wait(timeout=sleep_sec):
                self.state_changed.clear()

    def set_state(self, power: bool = None, color: tuple[int, int, int] = None,
                  brightness: float = None, effect: str = None, speed: int = None,
                  preset: str = None) -> dict:
        """Update LED settings atomically and wake the single-writer worker."""
        with self.lock:
            if preset and preset.lower() in PRESETS:
                self.color = PRESETS[preset.lower()]
                if preset.lower() == "off":
                    self.power = False
                else:
                    self.power = True
            if power is not None:
                self.power = bool(power)
            if color is not None:
                r = max(0, min(255, int(color[0])))
                g = max(0, min(255, int(color[1])))
                b = max(0, min(255, int(color[2])))
                self.color = (r, g, b)
            if brightness is not None:
                self.brightness = max(0.0, min(1.0, float(brightness)))
            if effect is not None:
                effect_clean = effect.strip().lower()
                if effect_clean != self.effect:
                    self.effect = effect_clean
                    self.runner.reset()
            if speed is not None:
                self.speed = max(10, min(1000, int(speed)))

        # Wake worker immediately for hardware push
        self.state_changed.set()
        return self.get_state()

    def get_state(self) -> dict:
        with self.lock:
            return {
                "power": self.power,
                "color": {
                    "r": self.color[0],
                    "g": self.color[1],
                    "b": self.color[2],
                    "hex": f"#{self.color[0]:02x}{self.color[1]:02x}{self.color[2]:02x}"
                },
                "brightness": round(self.brightness, 2),
                "brightness_pct": int(self.brightness * 100),
                "effect": self.effect,
                "speed": self.speed,
                "effects_list": EFFECT_NAMES,
                "hardware": self.is_hardware,
                "led_count": self.count,
                "gpio_pin": self.pin_num
            }

    def cleanup(self):
        """Turn off LEDs and release hardware cleanly."""
        self.running = False
        self.state_changed.set()
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=0.5)
        if self.is_hardware and self.pixels:
            try:
                self.pixels.fill((0, 0, 0))
                self.pixels.show()
                time.sleep(0.05)
                self.pixels.deinit()
            except Exception:
                pass
        logger.info("LEDManager cleaned up successfully.")
