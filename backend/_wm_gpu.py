# -*- coding: utf-8 -*-
"""
GPU 批量盲水印嵌入模块。

把盲水印嵌入的核心计算（haar DWT + 4x4 DCT/SVD 系数注入）从 CPU 逐帧、逐块
改写为 cupy 批量执行：把一批帧的所有 4x4 块堆叠成 (nB, 4, 4) 数组，一次性
批量 SVD 与系数注入，实现 GPU 大颗粒并行提速。

设计原则（与 plan 一致）：
- 色域 BGR<->YUV 保留在 CPU 走 cv2，GPU 只做 DWT/DCT/SVD。
- 嵌入结果不需要与 CPU 版本逐位相等，只需能被现有 CPU 提取端解出。
  因此本模块在 available() 中做一次性自检（DWT 相位、DCT 基矩阵、SVD 对拍），
  全部通过才启用；任一环节失败或 GPU 不可用，调用方回退 CPU 多进程。
- 模块级懒加载 cupy：cupy 未装 / 无设备 / 自检失败时 available() 返回 False。
"""
import os
import sys

import numpy as np

_EXT_ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_LIB = os.path.join(_EXT_ROOT, "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

# cupy-cuda12x 需要 CUDA 12 运行库（cublas64_12 / cusolver64_11 / nvrtc64_120_0 等）。
# 本应用与 torch(cu121) 同环境，torch\lib 恰好自带全套这些 DLL；导入 cupy 前把该目录
# 加入 DLL 搜索路径，否则 cupy.cuda.libs 在 Windows 上会报 "找不到指定的模块"。
# 注意：os.add_dll_directory 返回的句柄必须保持引用，否则被 GC 回收后目录即被移除。
_DLL_HANDLES = []


def _ensure_cuda12_path():
    try:
        import importlib.util
        spec = importlib.util.find_spec("torch")
        if spec and spec.origin:
            lib_dir = os.path.join(os.path.dirname(spec.origin), "lib")
            if os.path.isdir(lib_dir) and lib_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = lib_dir + os.pathsep + os.environ.get("PATH", "")
                _DLL_HANDLES.append(os.add_dll_directory(lib_dir))
    except Exception:  # noqa: BLE001
        pass


_ensure_cuda12_path()

try:
    import cupy as cp
    _HAS_CP = True
except Exception:  # noqa: BLE001
    cp = None
    _HAS_CP = False

try:
    import cv2
except Exception:  # noqa: BLE001
    cv2 = None

try:
    import pywt
except Exception:  # noqa: BLE001
    pywt = None

_SQRT2 = 1.4142135623730951

# 模块级自检结果缓存
_AVAILABLE = None


# ---------------------------------------------------------------------------
# 一次性预计算常量（与具体帧内容无关，只在首帧尺寸下初始化一次）
# ---------------------------------------------------------------------------
_CACHE = {}  # key = (password_img, wm_key); 存 {T, Tt, shuffler, inv, wm_vector, block_num, wm_size}


def _dct_basis():
    """4x4 正交 DCT-II 基矩阵 T，且 T@T.T=I。dct(x)=T@x@T.T；idct(x)=T.T@x@T。"""
    n = 4
    T = np.zeros((n, n), dtype=np.float32, order="C")
    for k in range(n):
        alpha = np.sqrt(1.0 / n) if k == 0 else np.sqrt(2.0 / n)
        for m in range(n):
            T[k, m] = alpha * np.cos(np.pi * k * (2 * m + 1) / (2 * n))
    return T


def _random_shuffle(seed, nb):
    """与 bwm_core.random_strategy1 完全一致：每块一个 16 长排列。"""
    return np.random.RandomState(seed).random(size=(nb, 16)).argsort(axis=1)


def _ensure_init(wm_cfg, hw):
    """按 (口令, 水印内容, 尺寸) 初始化并缓存共享常量。返回 cache dict。"""
    password_img = int(wm_cfg.get("pw2") or 1)
    password_wm = int(wm_cfg.get("pw1") or 1)
    wm_bits = np.asarray(wm_cfg["wm_bits"], dtype=bool)
    wm_size = wm_bits.size
    he, we = hw[0] + (hw[0] % 2), hw[1] + (hw[1] % 2)
    wb, wc = (he // 2) // 4, (we // 2) // 4
    nb = wb * wc
    # key 必须包含水印口令与 bit 内容指纹：否则同分辨率、同水印长度的两个并发任务
    # 会复用彼此的 wm_vector，导致嵌入成另一个任务的水印（用自己口令无法提取）。
    bits_fp = wm_bits.tobytes()
    key = (password_img, password_wm, he, we, hash(bits_fp))
    c = _CACHE.get(key)
    if c is None:
        strg = np.asarray(wm_bits, dtype=bool)
        wm_vec = np.empty(nb, dtype=np.float32)
        for i in range(nb):
            wm_vec[i] = 1.0 if strg[i % wm_size] else 0.0
        T = _dct_basis()
        shuf = _random_shuffle(password_img, nb)
        inv = shuf.argsort(axis=1)
        c = {
            "T": cp.asarray(T),
            "Tt": cp.asarray(T.T),
            "shuffler": cp.asarray(shuf, dtype=np.int64),
            "inv": cp.asarray(inv, dtype=np.int64),
            "wm_vector": cp.asarray(wm_vec),
            "block_num": nb,
            "wm_size": wm_size,
            "wb": wb, "wc": wc,
        }
        _CACHE[key] = c
    return c


# ---------------------------------------------------------------------------
# 批量 2D haar DWT / IDWT（复刻 pywt 'haar' 的相位与系数缩放）
# ---------------------------------------------------------------------------
def _dwt2_batch(X):
    """X:(N, m, n) float32。返回 (cA, cH, cV, cD)，各 (N, m/2, n/2)。
    坐标约定：cA 左上 / cH 右上(行低通列高通) / cV 左下(行高通列低通) / cD 右下。"""
    # 列方向（axis=1）
    low = (X[..., 0::2] + X[..., 1::2]) / _SQRT2
    high = (X[..., 1::2] - X[..., 0::2]) / _SQRT2
    # 行方向（axis=0）
    cA = (low[:, 0::2, :] + low[:, 1::2, :]) / _SQRT2
    cH = (high[:, 0::2, :] + high[:, 1::2, :]) / _SQRT2
    cV = (low[:, 1::2, :] - low[:, 0::2, :]) / _SQRT2
    cD = (high[:, 1::2, :] - high[:, 0::2, :]) / _SQRT2
    return cA, cH, cV, cD


def _interleave_rows(a, b):
    """沿轴1(行)交错：out[:,2i]=a[:,i], out[:,2i+1]=b[:,i]（IDWT 的行重建）。"""
    out = cp.empty(a.shape[:1] + (a.shape[1] * 2,) + a.shape[2:], dtype=a.dtype)
    out[:, 0::2] = a
    out[:, 1::2] = b
    return out


def _interleave_cols(a, b):
    """沿最后轴交错：out[...,2j]=a[..., j], out[...,2j+1]=b[..., j]（用于 IDWT 的列重建）。"""
    out = cp.empty(a.shape[:-1] + (a.shape[-1] * 2,), dtype=a.dtype)
    out[..., 0::2] = a
    out[..., 1::2] = b
    return out


def _idwt2_batch(cA, cH, cV, cD):
    """cA/cH/cV/cD：(N, m2, n2)。返回 (N, m, n) 重建。精确反函数。"""
    L = _interleave_rows((cA - cV) / _SQRT2, (cA + cV) / _SQRT2)   # (N, m, n2)
    H = _interleave_rows((cH - cD) / _SQRT2, (cH + cD) / _SQRT2)   # (N, m, n2)
    X = _interleave_cols((L - H) / _SQRT2, (L + H) / _SQRT2)       # (N, m, n)
    return X


# ---------------------------------------------------------------------------
# 单帧 → 补齐 YUV（CPU，cv2）。色域转换刻意用 float32 路径，与库 read_img_arr
# (self.img=img.astype(float32); cvtColor) 完全一致，避免与提取端基准错位。
# ---------------------------------------------------------------------------
def _frame_to_yuv_cpu(bgr, he, we):
    yuv = cv2.cvtColor(bgr.astype(np.float32), cv2.COLOR_BGR2YUV)
    if yuv.shape[0] < he or yuv.shape[1] < we:
        yuv = cv2.copyMakeBorder(yuv, 0, he - yuv.shape[0], 0, we - yuv.shape[1],
                                 cv2.BORDER_CONSTANT, value=(0, 0, 0))
    return yuv


# ---------------------------------------------------------------------------
# 核心嵌入
# ---------------------------------------------------------------------------
def batch_embed(wm_cfg, frames):
    """frames: uint8 BGR (N,H,W,3)。返回同型 uint8 嵌入帧 (N,H,W,3)。

    内部：BGR->YUV(CPU cv2) -> 补边 -> 批量 haar DWT -> ca 分块 -> DCT -> 置乱 ->
    批量 SVD 注入 -> 逆置乱 -> IDCT -> 回填 -> 批量 IDWT -> 裁边 -> YUV->BGR(CPU cv2) -> clip/uint8。
    """
    frames = np.asarray(frames, dtype=np.uint8)
    if frames.ndim != 4 or not _HAS_CP:
        raise RuntimeError("GPU 路径不可用")
    N, H, W, ch = frames.shape
    if ch not in (3, 4):
        raise RuntimeError("不支持的通道数")
    if ch == 4:
        # 视频恒为 3 通道；带 alpha 的帧回退由调用方处理，这里直接剔除 alpha
        frames = frames[:, :, :, :3]

    d1 = int(wm_cfg.get("d1") or 24)
    d2 = int(wm_cfg.get("d2") or 14)

    # ---- CPU：色域转换 + 补边，堆叠成 (N, he, we, 3) float32（与库提取端同源）----
    he, we = H + (H % 2), W + (W % 2)
    yuv = np.empty((N, he, we, 3), dtype=np.float32)
    for i in range(N):
        yuv[i] = _frame_to_yuv_cpu(frames[i], he, we)
    del frames

    # ---- 切换到 GPU ----
    dev = cp.cuda.Device(0)
    with dev:
        c = _ensure_init(wm_cfg, (H, W))
        yuv_g = cp.asarray(yuv)
        del yuv
        wb, wc = c["wb"], c["wc"]
        nb = wb * wc
        wb4, wc4 = wb * 4, wc * 4
        resh = (N, wb, 4, wc, 4)
        embed_ca = cp.empty((N, he // 2, we // 2), dtype=cp.float32)

        for channel in range(3):
            X = yuv_g[:, :, :, channel]  # (N, he, we)
            cA, cH, cV, cD = _dwt2_batch(X)
            # ca 分块 (N, nb, 16)：分块后的自然序 4x4 展平
            main = cA[:, :wb4, :wc4].reshape(resh).transpose(0, 1, 3, 2, 4).reshape(N, nb, 16)
            # 批量 DCT（自然序 4x4 -> 4x4），保持 (N, nb, ...)
            Bd = cp.einsum("ik,nkm,jm->nij", c["T"], main.reshape(N * nb, 4, 4), c["T"])
            Bd = Bd.reshape(N, nb, 16)
            # 置乱 -> 批量 SVD
            Bs = cp.take_along_axis(Bd, c["shuffler"][None, :, :], axis=2).reshape(N * nb, 4, 4)
            u, s, vh = cp.linalg.svd(Bs)
            # 系数注入（水印向量按帧重复：批内顺序为 [帧][块]）
            wmv = cp.tile(c["wm_vector"], N)  # (N*nb,)
            s_new = s.copy()
            s_new[:, 0] = (s[:, 0] // d1 + 0.25 + 0.5 * wmv) * d1
            if d2:
                s_new[:, 1] = (s[:, 1] // d2 + 0.25 + 0.5 * wmv) * d2
            Bnew = cp.einsum("bid,bd,bdk->bik", u, s_new, vh).reshape(N, nb, 16)
            # 逆置乱 -> IDCT -> 自然序分块，写回 ca 网格（[bi,bj,r,c] -> 图像 A[bi*4+r, bj*4+c]）
            Bnat = cp.take_along_axis(Bnew, c["inv"][None, :, :], axis=2).reshape(N * nb, 4, 4)
            Bnat = cp.einsum("ik,nkm,mj->nij", c["Tt"], Bnat, c["T"]).reshape(N, wb, wc, 4, 4).transpose(0, 1, 3, 2, 4).reshape(N, wb4, wc4)
            # 回填（保留 ca 主体外的原始细节条带）
            embed_ca[:] = cA[:, :he // 2, :we // 2]
            embed_ca[:, :wb4, :wc4] = Bnat
            # IDWT 重建并写回 yuv_g
            yuv_g[:, :, :, channel] = _idwt2_batch(embed_ca, cH, cV, cD)

        # ---- CPU：裁边、YUV->BGR、clip、uint8 ----
        out = cp.asnumpy(yuv_g[:, :H, :W, :])
    del yuv_g
    if cv2 is not None:
        for i in range(N):
            bgr = cv2.cvtColor(out[i], cv2.COLOR_YUV2BGR)
            out[i] = np.clip(bgr, 0, 255).astype(np.uint8)
    return out


def embed_batch_size(h, w, mem_cap_mb=1400):
    """按分辨率与显存预算自适应批大小，clamp [4, 64]。"""
    px = max(1, int(h * w))
    per_frame = int(px * 70)  # 经验系数：DWT/分块/温度的 float32 足迹
    n = max(4, min(64, int(mem_cap_mb * 1e6) // per_frame))
    return n


# ---------------------------------------------------------------------------
# 自检：DWT 相位 / DCT 基矩阵 / 批量 SVD 与 numpy 对拍
# ---------------------------------------------------------------------------
def _run_selfcheck():
    if not _HAS_CP or cv2 is None or pywt is None:
        return False
    if cp.cuda.runtime.getDeviceCount() < 1:
        return False
    rng = np.random.RandomState(0)

    # 1) haar DWT 相位与 pywt 对拍（关键：ca 的相位必须与 pywt 一致）
    #    细节系数 cH/cV/cD 的符号与 pywt 相反属正常（我的一致反变换可精确重建），
    #    嵌入只改 ca，故只要求 cA 相位对齐。
    img = rng.randint(0, 256, size=(64, 80)).astype(np.float32)
    X = cp.asarray(img[None, :, :])
    cA, cH, cV, cD = _dwt2_batch(X)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        caA, _ = pywt.dwt2(img, "haar")
    if np.abs(cp.asnumpy(cA[0]) - np.asarray(caA, dtype=np.float32)).max() > 1e-3:
        return False
    # IDWT 往返自洽
    if np.abs(cp.asnumpy(_idwt2_batch(cA, cH, cV, cD)[0]) - img).max() > 1e-4:
        return False

    # 2) DCT 基矩阵 vs cv2.dct
    T = _dct_basis()
    blocks = rng.randn(256, 4, 4).astype(np.float32)
    gdct = cp.asnumpy(cp.einsum("ik,bkm,jm->bij", cp.asarray(T), cp.asarray(blocks), cp.asarray(T)))
    for i in range(64):
        r = cv2.dct(blocks[i]).astype(np.float32)
        if np.abs(gdct[i] - r).max() > 1e-4:
            return False
    # IDCT vs cv2.idct（下标 mj：第三参取 T 的原位系 m、变换系 j，即 T[m,j]=T.T[j,m]，
    # 使求和等价于 T.T@x@T，与 cv2.idct 一致；不能用 jm，否则退化为 T[j,m] 造成错位）
    gidct = cp.asnumpy(cp.einsum("ik,bkm,mj->bij", cp.asarray(T.T), cp.asarray(blocks), cp.asarray(T)))
    for i in range(64):
        r = cv2.idct(blocks[i]).astype(np.float32)
        if np.abs(gidct[i] - r).max() > 1e-4:
            return False

    # 3) 批量 SVD vs np.linalg.svd（奇异值吻合）
    pts = rng.randn(2048, 4, 4).astype(np.float32)
    u, s, vh = cp.linalg.svd(cp.asarray(pts))
    s_np = np.linalg.svd(pts, compute_uv=False)
    if np.abs(cp.asnumpy(s) - s_np).max() > 1e-3:
        return False
    return True


def available():
    global _AVAILABLE
    if _AVAILABLE is None:
        try:
            _AVAILABLE = _run_selfcheck()
        except Exception:  # noqa: BLE001
            _AVAILABLE = False
    return _AVAILABLE