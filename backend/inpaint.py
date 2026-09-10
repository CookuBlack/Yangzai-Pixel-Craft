"""
去水印修复引擎。
- AI 模式：LaMa（sota 图像修复）+ MAT（结构感知 Transformer，复杂结构/人像保持更好）。
  底层库为 iopaint（原 lama-cleaner）。
  关键点：iopaint 的 ModelManager 只会注册 is_downloaded() 为真的模型，
  而 LaMa.is_downloaded() 检查的是 torch hub 缓存目录而非项目 model/ 目录，
  直接走 ModelManager 会报 "Unsupported model: lama"。
  因此这里绕过 ModelManager，直接实例化 iopaint 模型类，
  并提前把权重 URL 环境变量指向项目 model/ 下的权重，实现零联网加载。
- 回退模式：OpenCV Telea 修复，轻量、无需 GPU，开箱即用。
engine 参数：auto(优先AI) / ai(LaMa) / mat / opencv
"""
import os
import glob
import cv2
import numpy as np


# ===================== 本地模型定位 =====================
# 项目结构：video-watermark-remover/{backend/inpaint.py, model/big-lama.pt, model/mat/...}
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(_THIS_DIR, ".."))
MODEL_DIR = os.path.join(_PROJECT_ROOT, "model")
MAT_DIR = os.path.join(MODEL_DIR, "mat")

_MODEL_NAME_PRIORITY = ("big-lama.pt", "big-lama.pth", "lama.pt")
_MAT_NAME_PRIORITY = ("Places_512_FullData_G.pth", "places_512_fulldata_g.pth")


def find_local_lama() -> str | None:
    """在项目 model/ 目录下查找 LaMa 权重文件，找不到返回 None。"""
    if not os.path.isdir(MODEL_DIR):
        return None
    for name in _MODEL_NAME_PRIORITY:
        p = os.path.join(MODEL_DIR, name)
        if os.path.exists(p):
            return p
    cands = sorted(
        glob.glob(os.path.join(MODEL_DIR, "*.pt"))
        + glob.glob(os.path.join(MODEL_DIR, "*.pth"))
    )
    return cands[0] if cands else None


def find_local_mat() -> str | None:
    """在项目 model/mat/ 目录下查找 MAT 权重文件，找不到返回 None。"""
    if not os.path.isdir(MAT_DIR):
        return None
    for name in _MAT_NAME_PRIORITY:
        p = os.path.join(MAT_DIR, name)
        if os.path.exists(p):
            return p
    cands = sorted(
        glob.glob(os.path.join(MAT_DIR, "*.pt"))
        + glob.glob(os.path.join(MAT_DIR, "*.pth"))
    )
    return cands[0] if cands else None


# 关键：在 import iopaint 之前，把权重 URL 环境变量设为本地文件绝对路径。
# iopaint 的 load_jit_model / load_model 在 os.path.exists(url) 为真时直接加载本地文件，
# 不下载、不校验 md5。
_LOCAL_MODEL = find_local_lama()
if _LOCAL_MODEL:
    os.environ["LAMA_MODEL_URL"] = _LOCAL_MODEL
    os.environ["ANIME_LAMA_MODEL_URL"] = _LOCAL_MODEL  # 兼容 anime-lama 取值
    print(f"[info] 使用本地 LaMa 模型：{_LOCAL_MODEL}")
else:
    print(f"[warn] 未在 {MODEL_DIR} 找到 LaMa 权重文件，AI 模式将尝试联网下载。")

_LOCAL_MAT = find_local_mat()
if _LOCAL_MAT:
    os.environ["MAT_MODEL_URL"] = _LOCAL_MAT
    print(f"[info] 使用本地 MAT 模型：{_LOCAL_MAT}")


# ===================== 包导入（iopaint 优先，兼容旧 lama_cleaner） =====================
_PKG = None  # 'iopaint' | 'lama_cleaner' | None


def _resolve_pkg() -> str | None:
    global _PKG
    if _PKG is not None:
        return _PKG
    try:
        import iopaint  # noqa: F401

        _PKG = "iopaint"
    except Exception:
        try:
            import lama_cleaner  # noqa: F401

            _PKG = "lama_cleaner"
        except Exception:
            _PKG = None
    return _PKG


def lama_available() -> bool:
    """AI 引擎（iopaint / lama-cleaner）是否可用。"""
    return _resolve_pkg() is not None


_LAMA_MODEL = None
_LAMA_DEVICE = None
_SCHEMA = None  # (InpaintRequest/Config, HDStrategy)


def _norm_device(device: str) -> str:
    device = device or "cuda"
    if str(device) == "cuda":
        try:
            import torch

            if not torch.cuda.is_available():
                print("[warn] 未检测到 CUDA，AI 模式将使用 CPU（较慢）。")
                return "cpu"
        except Exception:
            return "cpu"
    return device


def _get_lama_model(device: str):
    """直接实例化 LaMa 模型（绕过 ModelManager 的 is_downloaded 注册门槛），并缓存。"""
    global _LAMA_MODEL, _LAMA_DEVICE, _SCHEMA
    device = _norm_device(device)
    if _LAMA_MODEL is not None and _LAMA_DEVICE == device and _SCHEMA is not None:
        return _LAMA_MODEL

    pkg = _resolve_pkg()
    if pkg is None:
        raise RuntimeError(
            "未安装 AI 引擎。请在你的环境中安装 iopaint（或旧版 lama-cleaner）："
            "pip install iopaint torch torchvision --index-url https://download.pytorch.org/whl/cu121"
        )

    if _LOCAL_MODEL:
        os.environ["LAMA_MODEL_URL"] = _LOCAL_MODEL

    if pkg == "iopaint":
        from iopaint.model.lama import LaMa
        from iopaint.schema import InpaintRequest, HDStrategy

        model = LaMa(device)
        _SCHEMA = (InpaintRequest, HDStrategy)
    else:
        # 旧版 lama_cleaner：退回到 ModelManager + Config
        from lama_cleaner.model_manager import ModelManager
        from lama_cleaner.schema import Config, HDStrategy

        model = ModelManager(name="lama", device=device)
        _SCHEMA = (Config, HDStrategy)

    _LAMA_MODEL = model
    _LAMA_DEVICE = device
    return model


def inpaint_lama(frame: np.ndarray, mask: np.ndarray, device: str = "cuda") -> np.ndarray:
    """使用 LaMa 对单帧进行修复，返回 BGR uint8 帧。"""
    model = _get_lama_model(device)
    ReqCls, HDStrategy = _SCHEMA

    image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)  # LaMa 期望 RGB 输入
    m = (mask > 0).astype(np.uint8) * 255  # [H, W] 0/255

    # RESIZE 策略：长边超限时先缩放修复再还原；配合默认 sd_keep_unmasked_area，
    # 只替换遮罩区域，其余像素原样保留，画质损失最小。
    config = ReqCls(
        hd_strategy=HDStrategy.RESIZE,
        hd_strategy_resize_limit=2000,
    )
    # 输出为 BGR（iopaint 的 __call__/forward 明确返回 BGR；sd_keep_unmasked_area=True
    # 会使结果变为 float64），此处直接转 uint8，切勿再做 RGB/BGR 互转。
    result = model(image, m, config)
    return np.asarray(result, dtype=np.uint8)


# ===================== MAT（结构感知 Transformer） =====================
_MAT_MODEL = None
_MAT_DEVICE = None


def mat_available() -> bool:
    """MAT 引擎是否可用：iopaint 可导入 且 权重已就位（model/mat/ 下）。"""
    return _resolve_pkg() is not None and find_local_mat() is not None


def _get_mat_model(device: str):
    """直接实例化 MAT 模型（绕过 ModelManager 注册门槛），并缓存。"""
    global _MAT_MODEL, _MAT_DEVICE, _SCHEMA
    device = _norm_device(device)
    if _MAT_MODEL is not None and _MAT_DEVICE == device:
        return _MAT_MODEL

    pkg = _resolve_pkg()
    if pkg is None:
        raise RuntimeError(
            "未安装 AI 引擎。请在你的环境中安装 iopaint（或旧版 lama-cleaner）："
            "pip install iopaint torch torchvision --index-url https://download.pytorch.org/whl/cu121"
        )
    if _LOCAL_MAT:
        os.environ["MAT_MODEL_URL"] = _LOCAL_MAT

    if pkg == "iopaint":
        from iopaint.model.mat import MAT
        from iopaint.schema import InpaintRequest, HDStrategy

        model = MAT(device)
        # 必须同步设置 _SCHEMA：inpaint_mat 依赖 (InpaintRequest, HDStrategy)，
        # 否则全新进程直接选 MAT 会 TypeError 并被静默降级为 OpenCV 修复。
        _SCHEMA = (InpaintRequest, HDStrategy)
    else:
        # 旧版 lama_cleaner 无 MAT，回退 LaMa（该函数内部会设置 _SCHEMA）
        return _get_lama_model(device)

    _MAT_MODEL, _MAT_DEVICE = model, device
    return model


def inpaint_mat(frame: np.ndarray, mask: np.ndarray, device: str = "cuda") -> np.ndarray:
    """使用 MAT 对单帧进行修复，返回 BGR uint8 帧。

    MAT 结构感知更强，对文字边缘 / 建筑线条 / 人像结构保持明显优于 LaMa。
    """
    model = _get_mat_model(device)
    ReqCls, HDStrategy = _SCHEMA

    image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)  # MAT 期望 RGB 输入
    m = (mask > 0).astype(np.uint8) * 255

    config = ReqCls(
        hd_strategy=HDStrategy.RESIZE,
        hd_strategy_resize_limit=2000,
    )
    result = model(image, m, config)
    return np.asarray(result, dtype=np.uint8)


def inpaint_frame(
    frame: np.ndarray, mask: np.ndarray, engine: str = "auto", device: str = "cuda"
) -> np.ndarray:
    if engine == "mat":
        try:
            return inpaint_mat(frame, mask, device)
        except Exception as e:  # MAT 不可用则回退 LaMa/OpenCV
            print(f"[warn] MAT 不可用，回退 OpenCV 修复: {e}")
            radius = max(3, int(np.mean(mask.shape) * 0.01))
            return cv2.inpaint(frame, mask, radius, cv2.INPAINT_TELEA)

    if engine in ("auto", "ai", "lama"):
        try:
            return inpaint_lama(frame, mask, device)
        except Exception as e:  # AI 不可用则优雅回退
            print(f"[warn] LaMa 不可用，回退 OpenCV 修复: {e}")

    radius = max(3, int(np.mean(mask.shape) * 0.01))
    return cv2.inpaint(frame, mask, radius, cv2.INPAINT_TELEA)
