#!/usr/bin/env python3
"""
Set a WS2812B strip to a solid color from the command line.

Usage (must run as root for GPIO18 PWM/DMA):
    sudo python3 LED_Test.py --red
    sudo python3 LED_Test.py --green
    sudo python3 LED_Test.py --blue
    sudo python3 LED_Test.py --white
    sudo python3 LED_Test.py --off
    sudo python3 LED_Test.py --white --brightness 0.5

The color stays on until you press Ctrl+C, then the strip is turned off.
"""
import argparse
import signal
import sys
import time

import board
import neopixel

LED_COUNT = 100
LED_PIN = board.D18  # GPIO18 (physical pin 12)

COLORS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
    "off": (0, 0, 0),
}


def parse_args():
    parser = argparse.ArgumentParser(description="Set the LED strip to a solid color.")
    group = parser.add_mutually_exclusive_group(required=True)
    for name in COLORS:
        group.add_argument(f"--{name}", action="store_const", const=name, dest="color",
                           help=f"turn the strip {name}" if name != "off" else "turn the strip off")
    parser.add_argument("--brightness", type=float, default=0.3,
                        help="0.0 to 1.0 (default 0.3; white at full brightness draws a lot of current)")
    parser.add_argument("--count", type=int, default=LED_COUNT,
                        help=f"number of LEDs (default {LED_COUNT})")
    args = parser.parse_args()
    if not 0.0 <= args.brightness <= 1.0:
        parser.error("--brightness must be between 0.0 and 1.0")
    return args


def main():
    args = parse_args()

    # Make `kill` / systemd stop behave like Ctrl+C so cleanup always runs
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    pixels = neopixel.NeoPixel(
        LED_PIN, args.count,
        brightness=args.brightness,
        auto_write=False,
        pixel_order=neopixel.GRB,
    )

    try:
        pixels.fill(COLORS[args.color])
        pixels.show()

        if args.color == "off":
            return

        print(f"Strip set to {args.color}. Press Ctrl+C to turn off and exit.")
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        pass

    finally:
        pixels.fill((0, 0, 0))
        pixels.show()
        time.sleep(0.1)
        pixels.deinit()


if __name__ == "__main__":
    main()