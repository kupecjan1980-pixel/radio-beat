#!/usr/bin/env python3
"""AI upscaling on the AMD NPU (testing): an ONNX super-resolution model (Real-ESRGAN-like: RGB in, RGB out,
values 0..1, NCHW) through ONNX Runtime's Vitis AI execution provider, in tiles.

usage: npu_upscale.py MODEL.onnx IN.rgb WIDTH HEIGHT OUT.rgb
IN.rgb / OUT.rgb are raw 8-bit RGB. Prints "SCALE <n>" when done. Exit 3: the NPU isn't the one running it
(no Vitis AI provider, or it gave the model to the CPU); the app then uses the GPU as usual."""

import sys

import numpy as np
import onnxruntime as ort

TILE, PAD = 128, 8


def main() -> int:
    model, src, w, h, out = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
    if "VitisAIExecutionProvider" not in ort.get_available_providers():
        print("no Vitis AI execution provider in this ONNX Runtime", file=sys.stderr)
        return 3
    sess = ort.InferenceSession(model, providers=["VitisAIExecutionProvider"])
    if sess.get_providers()[0] != "VitisAIExecutionProvider":
        print(f"the model runs on {sess.get_providers()[0]}, not the NPU", file=sys.stderr)
        return 3
    name = sess.get_inputs()[0].name
    img = np.fromfile(src, dtype=np.uint8).reshape(h, w, 3).astype(np.float32) / 255.0
    scale = None
    result = None
    for y in range(0, h, TILE):
        for x in range(0, w, TILE):
            # each tile with a margin, so its edges come out like the middle
            x0, y0 = max(0, x - PAD), max(0, y - PAD)
            x1, y1 = min(w, x + TILE + PAD), min(h, y + TILE + PAD)
            tile = img[y0:y1, x0:x1].transpose(2, 0, 1)[None]
            up = sess.run(None, {name: tile})[0][0].transpose(1, 2, 0)
            if scale is None:
                scale = up.shape[0] // tile.shape[2]
                result = np.zeros((h * scale, w * scale, 3), np.float32)
            tw, th = min(TILE, w - x), min(TILE, h - y)
            ox, oy = (x - x0) * scale, (y - y0) * scale
            result[y * scale:(y + th) * scale, x * scale:(x + tw) * scale] = \
                up[oy:oy + th * scale, ox:ox + tw * scale]
    (np.clip(result, 0, 1) * 255 + 0.5).astype(np.uint8).tofile(out)
    print(f"SCALE {scale}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
