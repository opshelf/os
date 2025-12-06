#!/usr/bin/env python3
"""
Door position diagnostic script.

Measures door motor position in closed and open states to verify
threshold configuration for curtain guard logic.

Usage:
    python scripts/diagnose_door.py [--config /path/to/config.json]

The script will:
1. Read current door position (assumes door is closed after homing)
2. Prompt to open door, then measure open position
3. Report findings and suggest threshold adjustments
"""

import asyncio
import argparse
import json
import sys
import time
from pathlib import Path


async def main():
    parser = argparse.ArgumentParser(description="Diagnose door position readings")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/opt/opshelf/repos/perpetual_os/perpetual_hardware/packages/perpetual_os1/config.json"),
        help="Path to config.json",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=10,
        help="Number of position samples to take",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.1,
        help="Interval between samples in seconds",
    )
    args = parser.parse_args()

    if not args.config.exists():
        print(f"Config not found: {args.config}")
        sys.exit(1)

    with open(args.config) as f:
        config = json.load(f)

    io_cfg = config["robot"]["io"]
    curtain_cfg = io_cfg.get("curtain", {})
    current_threshold = curtain_cfg.get("door_open_threshold_mm", 5.0)
    door_motor_cfg = None
    for m in io_cfg["motors"]:
        if m["motor_name"] == "door":
            door_motor_cfg = m
            break

    if door_motor_cfg is None:
        print("Door motor not found in config")
        sys.exit(1)

    bus_cfg = config["robot"]["bus"]["motor"]
    port = bus_cfg["ports"][0]
    baudrate = bus_cfg["baudrate"]

    print("=" * 60)
    print("Door Position Diagnostic")
    print("=" * 60)
    print(f"Config: {args.config}")
    print(f"Motor ID: {door_motor_cfg['motor_id']}")
    print(f"Motor port: {port}")
    print(f"Current threshold: {current_threshold} mm")
    print(f"Door limit (open): {door_motor_cfg['lim']} mm")
    print(f"Samples per measurement: {args.samples}")
    print("=" * 60)

    sys.path.insert(0, str(Path(__file__).parent.parent / "perpetual_hardware" / "packages" / "hardware" / "src"))
    from hardware.controller import SharedModbusController

    class MotorParams:
        def __init__(self, cfg):
            self.motor_id = cfg["motor_id"]
            self.Ip = cfg["Ip"]
            self.rpm = cfg["rpm"]
            self.dr = cfg["dr"]
            self.mpr = cfg["mpr"]
            self.accel = cfg["accel"]
            self.ret_rpm = cfg["ret_rpm"]
            self.lim = cfg["lim"]

    params = MotorParams(door_motor_cfg)

    controller = SharedModbusController(
        ports=[port],
        baudrate=baudrate,
        parity="N",
        timeout=2.0,
    )

    async def read_position_samples(n: int, interval: float) -> list[float]:
        samples = []
        for _ in range(n):
            pos_l = await controller.read(params.motor_id, 0x602C)
            pos_h = await controller.read(params.motor_id, 0x602D)
            if pos_l is None or pos_h is None:
                print("  [WARN] Failed to read position registers")
                continue
            pos_pulses = (pos_h << 16) | pos_l
            ppr = await controller.read(params.motor_id, 0x6003)
            if ppr is None or ppr == 0:
                ppr = 10000
            pos_mm = (pos_pulses / ppr) * params.mpr
            samples.append(pos_mm)
            await asyncio.sleep(interval)
        return samples

    def analyze_samples(samples: list[float], label: str) -> dict:
        if len(samples) == 0:
            return {"error": "No samples collected"}
        avg = sum(samples) / len(samples)
        min_val = min(samples)
        max_val = max(samples)
        spread = max_val - min_val
        return {
            "label": label,
            "samples": len(samples),
            "min_mm": round(min_val, 3),
            "max_mm": round(max_val, 3),
            "avg_mm": round(avg, 3),
            "spread_mm": round(spread, 3),
        }

    try:
        await controller.connect()
        print("\nConnected to motor controller")

        print("\n" + "-" * 60)
        print("PHASE 1: Measuring CLOSED position")
        print("-" * 60)
        print("Ensure door is physically CLOSED (homed position)")
        input("Press Enter when ready...")

        closed_samples = await read_position_samples(args.samples, args.interval)
        closed_stats = analyze_samples(closed_samples, "CLOSED")
        print(f"\nClosed position stats:")
        for k, v in closed_stats.items():
            print(f"  {k}: {v}")

        print("\n" + "-" * 60)
        print("PHASE 2: Measuring OPEN position")
        print("-" * 60)
        print("Please OPEN the door manually or via API")
        input("Press Enter when door is OPEN...")

        open_samples = await read_position_samples(args.samples, args.interval)
        open_stats = analyze_samples(open_samples, "OPEN")
        print(f"\nOpen position stats:")
        for k, v in open_stats.items():
            print(f"  {k}: {v}")

        print("\n" + "=" * 60)
        print("ANALYSIS")
        print("=" * 60)

        closed_max = closed_stats.get("max_mm", 0)
        open_min = open_stats.get("min_mm", 0)
        gap = open_min - closed_max

        print(f"\nClosed position range: {closed_stats['min_mm']} - {closed_stats['max_mm']} mm")
        print(f"Open position range: {open_stats['min_mm']} - {open_stats['max_mm']} mm")
        print(f"Gap between states: {round(gap, 3)} mm")
        print(f"Current threshold: {current_threshold} mm")

        if closed_max > current_threshold:
            print(f"\n[PROBLEM] Closed position ({closed_max} mm) exceeds threshold ({current_threshold} mm)")
            print("  -> Door will be incorrectly detected as OPEN when closed")
            print("  -> This causes false curtain guard activations")
            suggested = closed_max + (gap / 2) if gap > 0 else closed_max + 5.0
            print(f"\n[SUGGESTION] Increase threshold to: {round(suggested, 1)} mm")
        elif gap < 5.0:
            print(f"\n[WARNING] Small gap ({gap} mm) between closed and open positions")
            print("  -> Consider mechanical inspection or recalibration")
        else:
            print(f"\n[OK] Current threshold ({current_threshold} mm) appears correct")
            print(f"  -> Closed max ({closed_max} mm) < threshold < Open min ({open_min} mm)")

        print("\n" + "-" * 60)
        print("CONTINUOUS MONITORING (Ctrl+C to stop)")
        print("-" * 60)
        print("Move door and observe position readings...")
        try:
            while True:
                samples = await read_position_samples(1, 0)
                if samples:
                    pos = samples[0]
                    state = "OPEN" if pos > current_threshold else "CLOSED"
                    bar_len = int(min(pos / door_motor_cfg["lim"] * 40, 40))
                    bar = "#" * bar_len + "." * (40 - bar_len)
                    print(f"\r  Position: {pos:7.2f} mm [{bar}] -> {state}   ", end="", flush=True)
                await asyncio.sleep(0.2)
        except KeyboardInterrupt:
            print("\n\nMonitoring stopped")

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        await controller.close()
        print("\nDisconnected from motor controller")


if __name__ == "__main__":
    asyncio.run(main())
