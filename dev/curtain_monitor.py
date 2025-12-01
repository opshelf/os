#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import time

# Give lgpio (backend for RPi.GPIO on newer Raspberry Pi OS) a writable place
# for its notification FIFOs if the environment did not set one.
_DEFAULT_LG_USER_DIR = "/tmp/lgpio"
if not os.environ.get("LG_USER_DIR"):
    try:
        os.makedirs(_DEFAULT_LG_USER_DIR, exist_ok=True)
        os.environ["LG_USER_DIR"] = _DEFAULT_LG_USER_DIR
    except Exception:
        # Fall through; import will fail with a clearer error if path is unusable.
        pass

try:
    import RPi.GPIO as GPIO  # type: ignore[import]
except Exception as import_error:
    # Broad except to also catch lgpio FileNotFoundError on import
    GPIO = None  # type: ignore[assignment]
    _GPIO_IMPORT_ERROR = import_error
else:
    _GPIO_IMPORT_ERROR = None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Light curtain tester: prints when state changes."
    )
    parser.add_argument(
        "--pin",
        type=int,
        default=3,
        help="BCM pin connected to the curtain sensor (default: 3)",
    )
    parser.add_argument(
        "--pull",
        choices=["up", "down", "off"],
        default="down",
        help="Pull configuration for the pin (default: down)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.1,
        help="Polling interval in seconds (default: 0.1)",
    )
    parser.add_argument(
        "--invert",
        action="store_true",
        help="Invert the reading if HIGH means clear instead of blocked.",
    )
    return parser.parse_args()


def _ensure_gpio() -> None:
    if GPIO is None:
        raise RuntimeError(
            "RPi.GPIO not available; run on a Raspberry Pi with RPi.GPIO installed."
        ) from _GPIO_IMPORT_ERROR


def _setup(pin: int, pull: str) -> None:
    _ensure_gpio()
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    pull_options = {
        "off": GPIO.PUD_OFF,
        "down": GPIO.PUD_DOWN,
        "up": GPIO.PUD_UP,
    }
    GPIO.setup(pin, GPIO.IN, pull_up_down=pull_options[pull])


def _read(pin: int, invert: bool) -> bool:
    raw = GPIO.input(pin) == GPIO.HIGH
    return not raw if invert else raw


def main() -> int:
    args = _parse_args()
    try:
        _setup(args.pin, args.pull)
    except Exception as exc:
        print(f"Failed to initialize GPIO: {exc}")
        return 1

    print(
        f"Monitoring curtain on BCM pin {args.pin} (pull {args.pull}, interval {args.interval}s). "
        f"'True' means light interrupted."
    )

    last_state: bool | None = None
    try:
        while True:
            state = _read(args.pin, args.invert)
            if last_state is None or state != last_state:
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                print(f"{timestamp} interrupted={state}")
                last_state = state
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopping curtain monitor.")
    finally:
        try:
            GPIO.cleanup(args.pin)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
