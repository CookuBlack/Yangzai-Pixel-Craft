# -*- coding: utf-8 -*-
"""
盲水印嵌入的进程内 worker：给 creamExecutor 的每个子进程初始化一次
WaterMark（复用同一水印参数），随后对单帧做嵌入，返回 uint8 数组。

独立成模块是为了让多进程 spawn 时只加载轻量依赖（numpy + blind_watermark），
避免被子进程重复导入主后端模块而触发重型模型初始化。
"""
import os
import sys
import uuid

import numpy as np

_EXT_ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_LIB = os.path.join(_EXT_ROOT, "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from blind_watermark import WaterMark  # noqa: E402

_WM = None


def _init_wm(cfg):
    """cfg: dict(mode, text, wm_bytes, pw1, pw2[, d1, d2])。在本进程内构造并复用 WaterMark。"""
    global _WM
    b = WaterMark(password_wm=int(cfg.get("pw1") or 1),
                  password_img=int(cfg.get("pw2") or 1))
    # 嵌入强度档位须与嵌入请求一致（提取时也用同一组值）
    if cfg.get("d1") and cfg.get("d2"):
        b.bwm_core.d1 = int(cfg["d1"])
        b.bwm_core.d2 = int(cfg["d2"])
    if cfg.get("wm_bits") is not None:
        # 新协议：流已含 64bit 尺寸头 + 已按口令乱序的载荷，直接写入核心
        # （绕过 WaterMark.read_wm 的整流 shuffle，保证头部留在流首）
        b.bwm_core.read_wm(np.asarray(cfg["wm_bits"], dtype=bool))
    elif (cfg.get("mode") or "str") == "str":
        b.read_wm(cfg.get("text") or "", mode="str")
    else:
        wm_bytes = cfg.get("wm_bytes") or b""
        if not wm_bytes:
            raise ValueError("图片水印模式缺少水印内容")
        tmp = os.path.join(_EXT_ROOT, "data", "wmout", f"wtmp_{uuid.uuid4().hex}.png")
        with open(tmp, "wb") as f:
            f.write(wm_bytes)
        try:
            b.read_wm(tmp, mode="img")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    _WM = b


def _embed_one(frame):
    """对单帧进行盲水印嵌入，返回 uint8 数组（尺寸与输入一致）。"""
    if _WM is None:
        return frame
    _WM.read_img(img=frame)
    out = _WM.embed()
    if out.dtype != np.uint8:
        out = np.clip(out, 0, 255).astype(np.uint8)
    return out