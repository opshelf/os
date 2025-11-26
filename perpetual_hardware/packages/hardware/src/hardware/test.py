#!/usr/bin/env python3
import time
import numpy as np
from pathlib import Path
from PIL import Image
import ArducamDepthCamera as ac

def main(out_dir: str = "."):
    out = Path(out_dir)
    out.mkdir(exist_ok=True)

    cam = ac.ArducamCamera()
    # --- Open the camera ---
    # Use CSI or USB depending on your setup:
    err = cam.open(ac.Connection.CSI, 2)   # or ac.Connection.USB
    # if err != ac.TofErrorCode.OK:
    #     raise RuntimeError(f"Camera open failed: {err}")

    # --- Start streaming depth frames ---
    err = cam.start(ac.FrameType.DEPTH)
    # if err != ac.TofErrorCode.OK:
    #     raise RuntimeError(f"Camera start failed: {err}")

    time.sleep(0.1)

    err, depth, conf = cam.requestFrame(200)
    # if err != ac.TofErrorCode.OK:
    #     raise RuntimeError(f"Failed to get frame: {err}")

    # --- Convert and save ---
    if depth.dtype == np.float32:
        depth = (depth * 1000).astype(np.uint16)

    Image.fromarray(depth, mode="I;16").save(out / "depth_mm.png")
    np.save(out / "depth_mm.npy", depth)
    print(f"Depth saved to {out.resolve()}/depth_mm.png")

    cam.stop()
    cam.close()

if __name__ == "__main__":
    main()
