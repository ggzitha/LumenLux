"""
WS2812FX Complete Effects Engine (56 Effects).
Faithfully ported from https://github.com/kitesurfer1404/WS2812FX
Optimized for Raspberry Pi with lightweight integer math and precomputed tables.
"""
import math
import random
import time

# Precomputed sine table (256 entries) for ultra-fast breathing & wave effects
SINE_TABLE = [
    int(127.5 * (1.0 + math.sin(i * 2.0 * math.pi / 256.0)))
    for i in range(256)
]

def wheel(pos: int) -> tuple[int, int, int]:
    """Generate vibrant color wheel from 0 to 255."""
    pos = pos & 255
    if pos < 85:
        return (255 - pos * 3, pos * 3, 0)
    elif pos < 170:
        pos -= 85
        return (0, 255 - pos * 3, pos * 3)
    else:
        pos -= 170
        return (pos * 3, 0, 255 - pos * 3)

def scale_color(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Scale RGB tuple by factor 0.0 to 1.0."""
    factor = max(0.0, min(1.0, factor))
    return (int(color[0] * factor), int(color[1] * factor), int(color[2] * factor))

# Names of all 56 effects from WS2812FX.h
EFFECT_NAMES = [
    "static",                    # 0
    "blink",                     # 1
    "breath",                    # 2
    "color_wipe",                # 3
    "color_wipe_random",         # 4
    "random_color",              # 5
    "single_dynamic",            # 6
    "multi_dynamic",             # 7
    "rainbow",                   # 8
    "rainbow_cycle",             # 9
    "scan",                      # 10
    "dual_scan",                 # 11
    "fade",                      # 12
    "theater_chase",             # 13
    "theater_chase_rainbow",     # 14
    "running_lights",            # 15
    "twinkle",                   # 16
    "twinkle_random",            # 17
    "twinkle_fade",              # 18
    "twinkle_fade_random",       # 19
    "sparkle",                   # 20
    "flash_sparkle",             # 21
    "hyper_sparkle",             # 22
    "strobe",                    # 23
    "strobe_rainbow",            # 24
    "multi_strobe",              # 25
    "blink_rainbow",             # 26
    "chase_white",               # 27
    "chase_color",               # 28
    "chase_random",              # 29
    "chase_rainbow",             # 30
    "chase_flash",               # 31
    "chase_flash_random",        # 32
    "chase_rainbow_white",       # 33
    "chase_blackout",            # 34
    "chase_blackout_rainbow",    # 35
    "color_sweep_random",        # 36
    "running_color",             # 37
    "running_red_blue",          # 38
    "running_random",            # 39
    "larson_scanner",            # 40
    "comet",                     # 41
    "fireworks",                 # 42
    "fireworks_random",          # 43
    "merry_christmas",           # 44
    "fire_flicker",              # 45
    "fire_soft",                 # 46
    "dual_color_wave",           # 47
    "circulating",               # 48
    "tricolor_wipe",             # 49
    "icu",                       # 50
    "multi_comet",               # 51
    "dual_larson_scanner",       # 52
    "random_chase",              # 53
    "oscillate",                 # 54
    "pride_2015"                 # 55
]

class EffectRunner:
    def __init__(self, count: int = 100):
        self.count = max(1, count)
        self.step = 0
        self.aux_state = [0] * self.count
        self.color_cache = [(0, 0, 0)] * self.count
        self.random_color = (255, 0, 0)
        self.last_update = time.time()

    def reset(self):
        self.step = 0
        self.aux_state = [0] * self.count
        self.color_cache = [(0, 0, 0)] * self.count
        self.last_update = time.time()

    def get_frame(self, effect_name: str, base_color: tuple[int, int, int], speed_ms: int = 50) -> list[tuple[int, int, int]]:
        self.step = (self.step + 1) & 0xFFFF
        name = (effect_name or "static").lower().strip().replace(" ", "_")
        c = self.count
        s = self.step
        r, g, b = base_color

        # 0. Static (Solid)
        if name in ("static", "0"):
            return [base_color] * c

        # 1. Blink
        elif name in ("blink", "1"):
            return [base_color if (s % 2 == 0) else (0, 0, 0)] * c

        # 2. Breath
        elif name in ("breath", "breathing", "2"):
            phase = (s * 3) & 255
            factor = SINE_TABLE[phase] / 255.0
            return [scale_color(base_color, factor)] * c

        # 3. Color Wipe
        elif name in ("color_wipe", "3"):
            idx = s % (c * 2)
            if idx < c:
                return [base_color if i <= idx else (0, 0, 0) for i in range(c)]
            else:
                idx -= c
                return [(0, 0, 0) if i <= idx else base_color for i in range(c)]

        # 4. Color Wipe Random
        elif name in ("color_wipe_random", "4"):
            idx = s % (c * 2)
            if idx == 0:
                self.random_color = wheel(random.randint(0, 255))
            if idx < c:
                return [self.random_color if i <= idx else (0, 0, 0) for i in range(c)]
            else:
                idx -= c
                return [(0, 0, 0) if i <= idx else self.random_color for i in range(c)]

        # 5. Random Color
        elif name in ("random_color", "5"):
            if s % 10 == 0:
                self.random_color = wheel(random.randint(0, 255))
            return [self.random_color] * c

        # 6. Single Dynamic
        elif name in ("single_dynamic", "6"):
            idx = s % c
            return [base_color if i == idx else scale_color(base_color, 0.1) for i in range(c)]

        # 7. Multi Dynamic
        elif name in ("multi_dynamic", "7"):
            return [wheel((s * 2 + i * 10) & 255) if (i + s) % 4 == 0 else (0, 0, 0) for i in range(c)]

        # 8. Rainbow
        elif name in ("rainbow", "8"):
            col = wheel((s * 3) & 255)
            return [col] * c

        # 9. Rainbow Cycle
        elif name in ("rainbow_cycle", "9"):
            offset = (s * 4) & 255
            return [wheel((int(i * 256 / c) + offset) & 255) for i in range(c)]

        # 10. Scan
        elif name in ("scan", "10"):
            span = c - 1 if c > 1 else 1
            pos = s % (span * 2)
            idx = pos if pos < span else (span * 2 - pos)
            return [base_color if i == idx else (0, 0, 0) for i in range(c)]

        # 11. Dual Scan
        elif name in ("dual_scan", "11"):
            span = c - 1 if c > 1 else 1
            pos = s % (span * 2)
            idx1 = pos if pos < span else (span * 2 - pos)
            idx2 = span - idx1
            return [base_color if (i == idx1 or i == idx2) else (0, 0, 0) for i in range(c)]

        # 12. Fade
        elif name in ("fade", "12"):
            phase = (s * 2) & 255
            factor = (255 - phase) / 255.0
            return [scale_color(base_color, factor)] * c

        # 13. Theater Chase
        elif name in ("theater_chase", "marquee", "13"):
            return [base_color if (i + s) % 3 == 0 else (0, 0, 0) for i in range(c)]

        # 14. Theater Chase Rainbow
        elif name in ("theater_chase_rainbow", "14"):
            return [wheel((s * 3 + i * 5) & 255) if (i + s) % 3 == 0 else (0, 0, 0) for i in range(c)]

        # 15. Running Lights
        elif name in ("running_lights", "15"):
            pixels = []
            for i in range(c):
                sine_val = SINE_TABLE[(s * 5 + int(i * 256 / c)) & 255] / 255.0
                pixels.append(scale_color(base_color, sine_val))
            return pixels

        # 16. Twinkle
        elif name in ("twinkle", "16"):
            pixels = [(0, 0, 0)] * c
            for _ in range(max(1, c // 8)):
                pixels[random.randint(0, c - 1)] = base_color
            return pixels

        # 17. Twinkle Random
        elif name in ("twinkle_random", "17"):
            pixels = [(0, 0, 0)] * c
            for _ in range(max(1, c // 8)):
                pixels[random.randint(0, c - 1)] = wheel(random.randint(0, 255))
            return pixels

        # 18. Twinkle Fade
        elif name in ("twinkle_fade", "18"):
            for i in range(c):
                self.color_cache[i] = scale_color(self.color_cache[i], 0.85)
            for _ in range(max(1, c // 12)):
                self.color_cache[random.randint(0, c - 1)] = base_color
            return list(self.color_cache)

        # 19. Twinkle Fade Random
        elif name in ("twinkle_fade_random", "19"):
            for i in range(c):
                self.color_cache[i] = scale_color(self.color_cache[i], 0.85)
            for _ in range(max(1, c // 12)):
                self.color_cache[random.randint(0, c - 1)] = wheel(random.randint(0, 255))
            return list(self.color_cache)

        # 20. Sparkle
        elif name in ("sparkle", "20"):
            pixels = list(self.color_cache)
            # Reset previous sparkle
            pixels = [base_color] * c
            pixels[random.randint(0, c - 1)] = (255, 255, 255)
            return pixels

        # 21. Flash Sparkle
        elif name in ("flash_sparkle", "21"):
            pixels = [base_color] * c
            if s % 3 == 0:
                for _ in range(max(1, c // 10)):
                    pixels[random.randint(0, c - 1)] = (255, 255, 255)
            return pixels

        # 22. Hyper Sparkle
        elif name in ("hyper_sparkle", "22"):
            pixels = [base_color] * c
            for _ in range(max(2, c // 4)):
                pixels[random.randint(0, c - 1)] = (255, 255, 255)
            return pixels

        # 23. Strobe
        elif name in ("strobe", "23"):
            return [base_color if (s % 6 == 0) else (0, 0, 0)] * c

        # 24. Strobe Rainbow
        elif name in ("strobe_rainbow", "24"):
            col = wheel((s * 10) & 255)
            return [col if (s % 6 == 0) else (0, 0, 0)] * c

        # 25. Multi Strobe
        elif name in ("multi_strobe", "25"):
            return [base_color if (s % 3 == 0) else (0, 0, 0)] * c

        # 26. Blink Rainbow
        elif name in ("blink_rainbow", "26"):
            col = wheel((s * 15) & 255)
            return [col if (s % 2 == 0) else (0, 0, 0)] * c

        # 27. Chase White
        elif name in ("chase_white", "27"):
            idx = s % c
            return [(255, 255, 255) if i == idx else base_color for i in range(c)]

        # 28. Chase Color
        elif name in ("chase_color", "28"):
            idx = s % c
            return [base_color if i == idx else (0, 0, 0) for i in range(c)]

        # 29. Chase Random
        elif name in ("chase_random", "29"):
            idx = s % c
            if idx == 0:
                self.random_color = wheel(random.randint(0, 255))
            return [self.random_color if i == idx else (0, 0, 0) for i in range(c)]

        # 30. Chase Rainbow
        elif name in ("chase_rainbow", "30"):
            idx = s % c
            return [wheel((s * 5) & 255) if i == idx else (0, 0, 0) for i in range(c)]

        # 31. Chase Flash
        elif name in ("chase_flash", "31"):
            idx = s % c
            return [(255, 255, 255) if i == idx else (base_color if s % 2 == 0 else (0, 0, 0)) for i in range(c)]

        # 32. Chase Flash Random
        elif name in ("chase_flash_random", "32"):
            idx = s % c
            return [(255, 255, 255) if i == idx else (wheel((s * 10) & 255) if s % 2 == 0 else (0, 0, 0)) for i in range(c)]

        # 33. Chase Rainbow White
        elif name in ("chase_rainbow_white", "33"):
            idx = s % c
            return [(255, 255, 255) if i == idx else wheel((i * 10) & 255) for i in range(c)]

        # 34. Chase Blackout
        elif name in ("chase_blackout", "34"):
            idx = s % c
            return [(0, 0, 0) if i == idx else base_color for i in range(c)]

        # 35. Chase Blackout Rainbow
        elif name in ("chase_blackout_rainbow", "35"):
            idx = s % c
            return [(0, 0, 0) if i == idx else wheel((s * 4 + i * 5) & 255) for i in range(c)]

        # 36. Color Sweep Random
        elif name in ("color_sweep_random", "36"):
            idx = s % c
            if idx == 0:
                self.random_color = wheel(random.randint(0, 255))
            self.color_cache[idx] = self.random_color
            return list(self.color_cache)

        # 37. Running Color
        elif name in ("running_color", "37"):
            return [base_color if (i + s) % 6 < 3 else (0, 0, 0) for i in range(c)]

        # 38. Running Red Blue
        elif name in ("running_red_blue", "38"):
            return [(255, 0, 0) if (i + s) % 6 < 3 else (0, 0, 255) for i in range(c)]

        # 39. Running Random
        elif name in ("running_random", "39"):
            return [wheel((s * 3 + (i // 4) * 32) & 255) for i in range(c)]

        # 40. Larson Scanner
        elif name in ("larson_scanner", "40"):
            span = c - 1 if c > 1 else 1
            pos = s % (span * 2)
            eye = pos if pos < span else (span * 2 - pos)
            pixels = []
            for i in range(c):
                dist = abs(i - eye)
                if dist == 0:
                    pixels.append(base_color)
                elif dist == 1:
                    pixels.append(scale_color(base_color, 0.4))
                elif dist == 2:
                    pixels.append(scale_color(base_color, 0.1))
                else:
                    pixels.append((0, 0, 0))
            return pixels

        # 41. Comet
        elif name in ("comet", "41"):
            head = s % c
            pixels = []
            for i in range(c):
                dist = (head - i) % c
                if dist < 8:
                    fade = (8 - dist) / 8.0
                    pixels.append(scale_color(base_color, fade))
                else:
                    pixels.append((0, 0, 0))
            return pixels

        # 42. Fireworks
        elif name in ("fireworks", "42"):
            for i in range(c):
                self.color_cache[i] = scale_color(self.color_cache[i], 0.75)
            if s % 12 == 0:
                center = random.randint(3, c - 4)
                self.color_cache[center] = base_color
                self.color_cache[center - 1] = scale_color(base_color, 0.6)
                self.color_cache[center + 1] = scale_color(base_color, 0.6)
            return list(self.color_cache)

        # 43. Fireworks Random
        elif name in ("fireworks_random", "43"):
            for i in range(c):
                self.color_cache[i] = scale_color(self.color_cache[i], 0.75)
            if s % 12 == 0:
                center = random.randint(3, c - 4)
                fw_col = wheel(random.randint(0, 255))
                self.color_cache[center] = fw_col
                self.color_cache[center - 1] = scale_color(fw_col, 0.6)
                self.color_cache[center + 1] = scale_color(fw_col, 0.6)
            return list(self.color_cache)

        # 44. Merry Christmas
        elif name in ("merry_christmas", "44"):
            return [(255, 0, 0) if (i + s) % 4 < 2 else (0, 255, 0) for i in range(c)]

        # 45. Fire Flicker
        elif name in ("fire_flicker", "fire", "45"):
            pixels = []
            for _ in range(c):
                flicker = random.randint(0, 55)
                fr = 255 - flicker
                fg = max(0, min(255, 96 - flicker // 2 + random.randint(-15, 15)))
                fb = max(0, min(255, 12 - flicker // 4))
                pixels.append((fr, fg, fb))
            return pixels

        # 46. Fire Soft
        elif name in ("fire_soft", "46"):
            pixels = []
            for i in range(c):
                sine_val = SINE_TABLE[(s * 4 + i * 8) & 255] / 255.0
                fr = int(255 * (0.8 + 0.2 * sine_val))
                fg = int(75 * sine_val)
                pixels.append((fr, fg, 0))
            return pixels

        # 47. Dual Color Wave / Gradient
        elif name in ("dual_color_wave", "gradient", "47"):
            offset = (s * 3) & 255
            sec_col = (base_color[2], base_color[0], base_color[1]) if sum(base_color) > 0 else (128, 0, 255)
            pixels = []
            for i in range(c):
                t = ((int(i * 256 / c) + offset) & 255) / 255.0
                cr = int(r * (1.0 - t) + sec_col[0] * t)
                cg = int(g * (1.0 - t) + sec_col[1] * t)
                cb = int(b * (1.0 - t) + sec_col[2] * t)
                pixels.append((cr, cg, cb))
            return pixels

        # 48. Circulating
        elif name in ("circulating", "48"):
            idx = s % c
            return [wheel((s * 2 + i * 12) & 255) if abs(i - idx) < 3 else (0, 0, 0) for i in range(c)]

        # 49. Tricolor Wipe
        elif name in ("tricolor_wipe", "49"):
            colors = [base_color, (0, 255, 128), (255, 0, 128)]
            pos = s % (c * 3)
            col_idx = (pos // c) % 3
            led_idx = pos % c
            return [colors[col_idx] if i <= led_idx else colors[(col_idx - 1) % 3] for i in range(c)]

        # 50. ICU
        elif name in ("icu", "50"):
            span = c - 1 if c > 1 else 1
            pos = s % (span * 2)
            idx = pos if pos < span else (span * 2 - pos)
            pixels = [(0, 0, 0)] * c
            if idx < c: pixels[idx] = base_color
            if idx + 1 < c: pixels[idx + 1] = base_color
            return pixels

        # 51. Multi Comet
        elif name in ("multi_comet", "51"):
            pixels = [(0, 0, 0)] * c
            for offset in (0, c // 3, (c * 2) // 3):
                head = (s + offset) % c
                for tail in range(6):
                    node = (head - tail) % c
                    fade = (6 - tail) / 6.0
                    col = scale_color(base_color, fade)
                    pixels[node] = (max(pixels[node][0], col[0]), max(pixels[node][1], col[1]), max(pixels[node][2], col[2]))
            return pixels

        # 52. Dual Larson Scanner
        elif name in ("dual_larson_scanner", "52"):
            span = c - 1 if c > 1 else 1
            pos = s % (span * 2)
            eye1 = pos if pos < span else (span * 2 - pos)
            eye2 = span - eye1
            pixels = [(0, 0, 0)] * c
            for eye, col in ((eye1, base_color), (eye2, (0, 200, 255))):
                for delta in (-1, 0, 1):
                    node = eye + delta
                    if 0 <= node < c:
                        factor = 1.0 if delta == 0 else 0.4
                        pixels[node] = scale_color(col, factor)
            return pixels

        # 53. Random Chase
        elif name in ("random_chase", "53"):
            idx = s % c
            return [wheel((i * 17 + s) & 255) if (i == idx) else (0, 0, 0) for i in range(c)]

        # 54. Oscillate
        elif name in ("oscillate", "54"):
            sine_val = (SINE_TABLE[(s * 4) & 255] / 255.0)
            threshold = int(c * sine_val)
            return [base_color if i < threshold else (0, 0, 0) for i in range(c)]

        # 55. Pride 2015
        elif name in ("pride_2015", "55"):
            return [wheel((s * 3 + i * 7) & 255) for i in range(c)]

        # Fallback to static
        return [base_color] * c
