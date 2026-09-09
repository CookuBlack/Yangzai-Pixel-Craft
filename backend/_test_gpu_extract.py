# -*- coding: utf-8 -*-
"""GPU 端到端对拍：GPU 嵌入 -> 现有 CPU 提取端复核，验证三档强度位级兼容。"""
import sys, os
sys.path.insert(0, r"c:\Users\CooKu\Desktop\Creation\Remove\video-watermark-remover\backend")
os.chdir(r"c:\Users\CooKu\Desktop\Creation\Remove\video-watermark-remover\backend")
import numpy as np, cv2
import _wm_gpu as G
from blind_watermark.bwm_core import WaterMarkCore

print("GPU available:", G.available())

def extract_bits(img, pw2, d1, d2):
    core = WaterMarkCore(password_img=pw2)
    core.d1, core.d2 = int(d1), int(d2)
    raw = core.extract_raw(img)   # (3, block_num)
    return (raw.mean(0) > 0.5).astype(int)

rng = np.random.RandomState(42)
frames = rng.randint(0, 256, size=(4, 64, 64, 3)).astype(np.uint8)
bits = rng.randint(0, 2, size=16).astype(bool)
bits_arr = bits

presets = {"quality": (14, 10), "balanced": (24, 14), "robust": (36, 20)}
ok_all = True
for name, (d1, d2) in presets.items():
    cfg = {"mode": "str", "pw1": 1, "pw2": 7, "d1": d1, "d2": d2,
           "wm_bits": [int(b) for b in bits_arr]}
    out = G.batch_embed(cfg, frames)
    # 逐帧独立提取并与期望水印位对比
    for f in range(out.shape[0]):
        got = extract_bits(out[f], 7, d1, d2)
        nb = got.size
        exp = np.array([int(bits[i % len(bits)]) for i in range(nb)])
        match = (got == exp).mean()
        print(f"[{name}] frame{f}  提取位匹配率: {match*100:.2f}%  (误码 {int(nb*(1-match))}/{nb})")
        if match < 1.0:
            ok_all = False
print("提取兼容结果:", "全部通过 ✓" if ok_all else "存在误码 ✗")