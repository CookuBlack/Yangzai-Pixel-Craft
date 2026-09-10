"""
盲水印模块（blind_watermark 库）：
- 嵌入：给图片 / 视频逐帧嵌入文字或图片水印
- 提取：从含水印的图片 / 视频帧提取水印
新增端点，不改动已有接口。
"""
import os
import sys
import time
import json
import uuid
import glob
import shutil
import subprocess
import threading

import cv2
import numpy as np
import imageio_ffmpeg
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse

from task_store import new_task, get, update  # noqa: E402

# 盲水印库位于项目内 lib/：加入 sys.path
_EXT_ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
_BW_REPO = os.path.join(_EXT_ROOT, "lib")
if _BW_REPO not in sys.path:
    sys.path.insert(0, _BW_REPO)

# 抑制库实例化时打印的 note 文案
try:
    from blind_watermark import version as _bw_version
    _bw_version.bw_notes.print_notes = lambda: None
except Exception:
    pass

from blind_watermark import WaterMark  # noqa: E402

_ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
_WMOUT = os.path.join(_ROOT, "data", "wmout")
os.makedirs(_WMOUT, exist_ok=True)

# ---------------------------------------------------------------------------
# 水印参数元数据：嵌入时保存，提取时用「提取识别码」一键自动带入，
# 从而让用户无需理解「口令 / 水印长度 / 形状」这些专业参数。
# ---------------------------------------------------------------------------
_META_FILE = os.path.join(_WMOUT, "wm_meta.json")

# Windows + 冻结(GUI)环境：嵌入回退到 multiprocessing spawn 的每个工作进程都会
# 各自弹一个黑色命令行窗口（“很多命令行窗口闪动”）。强制 spawn 子进程不建控制台。
# 注意：popen_spawn_win32.Popen 不是 subprocess.Popen 的子类，其 __init__ 只接收
# (process_obj)、内部直接调 _winapi.CreateProcess(..., creationflags, ...)，塞
# creationflags 关键字会 TypeError 打挂整个进程池（多核嵌入静默退化为单线程）。
# 正确做法：仅在 spawn Popen 初始化的同步调用期间临时包装 CreateProcess，
# 在其第 6 个参数(dwCreationFlags)上 OR CREATE_NO_WINDOW。
if sys.platform == "win32":
    try:
        import multiprocessing.popen_spawn_win32 as _psw

        _SPAWN_POPEN_INIT = _psw.Popen.__init__
        _SPAWN_CREATE = _psw._winapi.CreateProcess
        _CREATE_NO_WINDOW = 0x08000000

        def _spawn_init_no_console(self, process_obj):
            def _create_no_window(*args):
                args = list(args)
                # CreateProcess(app,cmd,pa,ta,inherit,flags,env,cwd,startup)
                args[5] = int(args[5]) | _CREATE_NO_WINDOW
                return _SPAWN_CREATE(*args)

            _psw._winapi.CreateProcess = _create_no_window
            try:
                return _SPAWN_POPEN_INIT(self, process_obj)
            finally:
                _psw._winapi.CreateProcess = _SPAWN_CREATE

        _psw.Popen.__init__ = _spawn_init_no_console
    except Exception:  # noqa: BLE001
        pass


_ENC_CACHE = {"nv": None}   # 探测结果缓存：None=未探测


def _use_nvenc(ffmpeg_exe) -> bool:
    """检测当前 ffmpeg 是否带 NVIDIA NVENC（h264_nvenc）。带缓存，只探测一次。"""
    if _ENC_CACHE["nv"] is None:
        try:
            r = subprocess.run(
                [ffmpeg_exe, "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            _ENC_CACHE["nv"] = "h264_nvenc" in r.stdout
        except Exception:  # noqa: BLE001
            _ENC_CACHE["nv"] = False
    return _ENC_CACHE["nv"]


def _load_meta():
    if os.path.exists(_META_FILE):
        try:
            with open(_META_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_meta(meta):
    try:
        with open(_META_FILE, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
    except Exception:
        pass


def _store_embed_meta(wm_key, mode, wm_shape, password_wm, password_img,
                      strength=None, d1=None, d2=None):
    meta = _load_meta()
    meta[wm_key] = {
        "wm_mode": mode,
        "wm_shape": wm_shape,
        "password_wm": int(password_wm or 1),
        "password_img": int(password_img or 1),
        "strength": strength or _DEFAULT_STRENGTH,
        "d1": int(d1) if d1 else None,
        "d2": int(d2) if d2 else None,
        "created": time.time(),
    }
    _save_meta(meta)
    return wm_key


def _pop_embed_meta(wm_key):
    """取回并返回指定识别码保存的参数，找不到返回 None。"""
    if not wm_key:
        return None
    meta = _load_meta()
    return meta.get(wm_key)

router = APIRouter(prefix="/api/wm", tags=["wm"])

# 嵌入强度档位：(d1, d2)。值越小画质越好，越大越抗攻击。
# 库默认 (36, 20) 偏重鲁棒性、失真明显；本应用图片输出 PNG、视频 crf10 近无损，
# 无需极强鲁棒，默认用「均衡」档在画质与可靠性间折中。
_STRENGTH_PRESETS = {
    "quality": (14, 10),    # 高画质：几乎无感，适合不经过二次压缩的场景
    "balanced": (24, 14),   # 均衡（默认）
    "robust": (36, 20),     # 强保护：等同库默认，抗截图/压缩，画质损失较明显
}
_DEFAULT_STRENGTH = "balanced"


def _apply_strength(bwm, strength):
    """把档位写入水印核心。必须嵌入/提取两侧一致，否则无法正确提取。"""
    d1, d2 = _STRENGTH_PRESETS.get(strength or _DEFAULT_STRENGTH, _STRENGTH_PRESETS[_DEFAULT_STRENGTH])
    bwm.bwm_core.d1, bwm.bwm_core.d2 = d1, d2
    return d1, d2


# ---- 手动提取时的强度自动匹配 ----
_STR_NAME = {"quality": "高画质", "balanced": "均衡", "robust": "最强保护"}


def _score_wm_text(txt):
    """错误档位解出的文字含大量乱码（U+FFFD 替换符/不可打印字符），正确档位接近满分。"""
    s = str(txt or "")
    if not s:
        return -1.0
    bad = s.count("\ufffd")
    printable = sum(1 for ch in s if ch.isprintable() or ch in "\n\r\t")
    return (printable - 3.0 * bad) / len(s)


def _score_wm_img(path):
    """错误档位解出的"水印图"是均匀噪声；正确档位接近黑白二值（极暗+极亮像素占比高）。"""
    g = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if g is None:
        return -1.0
    f = g.astype("float32")
    return float(((f < 40) | (f > 215)).mean())


def _bits_to_text(bits):
    """把 0/1 位串还原为 UTF-8 文本（与 blind_watermark 库 decode 一致）。"""
    s = "".join("1" if b else "0" for b in bits)
    try:
        return bytes.fromhex(hex(int(s, base=2))[2:]).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _detect_period(bits):
    """通过自相关检测 0/1 序列的最小周期。

    嵌入时水印 bit 是按真实长度 P 周期性铺满全图分块的，因此提取出的原生位串
    在偏移为 P 的倍数处完全一致、其余位置近似不相关。返回第一个"高度自相关"的
    偏移即真实水印长度 P（文本按字节对齐，≥8）。
    """
    n = bits.size
    cap = min(n - 1, 1800)
    if cap < 8:
        return None
    for lag in range(8, cap + 1):
        w = n - lag
        if w < 1:
            break
        if float((bits[:w] == bits[lag:]).mean()) > 0.97:
            return lag
    return None


def _auto_str_length(img, password_wm, password_img):
    """在未知水印长度下自动推断文本水印（连同嵌入强度一起定位）。

    流程：对每档强度解出全图原生位串（与长度无关）→ 自相关定位真实周期(长度) →
    按该长度解密重排 → 以可打印率在强度间择优。
    """
    presets = _STRENGTH_PRESETS
    best = None
    for key, (d1, d2) in presets.items():
        bb = WaterMark(password_wm=password_wm, password_img=password_img)
        bb.bwm_core.d1, bb.bwm_core.d2 = int(d1), int(d2)
        try:
            raw = bb.bwm_core.extract_raw(img)  # (3, block_num) 每分块解出的 bit
        except Exception:  # noqa: BLE001
            continue
        bits = (raw.mean(0) > 0.5).astype(int)  # 三通道平均阈值化
        period = _detect_period(bits)
        if not period:
            continue
        S = period
        wm_avg = np.zeros(S)
        for i in range(S):
            wm_avg[i] = raw[:, i::S].mean()
        bb.wm_size = S
        dec = bb.extract_decrypt(wm_avg.copy())
        txt = _bits_to_text((dec >= 0.5).astype(int))
        if not txt:
            continue
        score = _score_wm_text(txt)
        if best is None or score > best[0]:
            best = (score, key, S, txt)
    if best is None:
        raise HTTPException(status_code=500, detail="提取失败：未能还原出有效水印文字，请确认文件含水印且已填入正确口令")
    _, key, S, txt = best
    return key, S, txt


_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")
_VID_EXT = (".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv", ".flv", ".wmv")


def _kind(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in _IMG_EXT:
        return "image"
    if ext in _VID_EXT:
        return "video"
    raise HTTPException(status_code=400, detail="不支持的文件类型")


# 尺寸头冗余次数：64bit 头重复 8 遍（共 512bit），提取端逐 bit 投票，
# 大幅提升有损编码（视频 H.264）下头部存活率。容量极小的源图降级为 1 遍。
_WM_HDR_REPEAT = 8
_WM_HDR_LEN = 64 * _WM_HDR_REPEAT


def _hdr_repeats(block_num):
    """按源图容量决定头部冗余次数：块数太少（<900）时降级为单头。"""
    return _WM_HDR_REPEAT if block_num >= 900 else 1


def _text_to_wm_bits(text, password_wm=1):
    """复刻库 read_wm(str) 的明文→乱序 bit 流。

    文本视频走 GPU/CPU 嵌入时，watermark_api 折叠各帧需同一份 wm_bits；
    这里与 blind_watermark.read_wm(str) 完全一致（bin 不带前导0，再用口令乱序），
    保证 GPU 嵌入后能被现有提取端按同协议解出。
    """
    hex_bytes = str(text).encode("utf-8").hex()
    bits = bin(int(hex_bytes, base=16))[2:] if hex_bytes else ""
    arr = (np.array(list(bits)) == "1").astype(bool)
    if arr.size:
        np.random.RandomState(int(password_wm or 1)).shuffle(arr)
    return arr


def _build_img_wm_stream(wm_gray, block_num, password_wm=1):
    """把水印灰度图缩放到容量内，构建嵌入流：[尺寸头×冗余] + [乱序后的载荷]。
    头部为 64bit（32bit 高 + 32bit 宽），原位不乱序，重复 _WM_HDR_REPEAT 遍，
    提取端逐 bit 多数投票恢复。载荷用口令乱序增强鲁棒性。
    返回 (bool bit流, (h, w))。注意：须配合 bwm_core.read_wm 直接嵌入（绕过
    WaterMark.read_wm 的整流 shuffle），否则头部会被再次打乱。"""
    repeats = _hdr_repeats(block_num)
    hdr_bits = 64 * repeats
    cap = max(0, int(block_num * 0.8) - hdr_bits)
    if wm_gray.size > cap:
        ratio = (cap / float(wm_gray.size)) ** 0.5
        nw = max(1, int(wm_gray.shape[1] * ratio))
        nh = max(1, int(wm_gray.shape[0] * ratio))
        wm_gray = cv2.resize(wm_gray, (nw, nh), interpolation=cv2.INTER_AREA)
    h, w = wm_gray.shape[:2]
    hdr = np.array(list(np.binary_repr(h, 32) + np.binary_repr(w, 32))) == "1"
    bits = wm_gray.flatten() > 128
    idx = np.arange(bits.size)
    np.random.RandomState(password_wm).shuffle(idx)
    return np.concatenate([np.tile(hdr, repeats), bits[idx]]), (h, w)


def _load_wm(bwm, mode, wm_text="", wm_file_bytes=None):
    """读取水印：str 模式直接用文本；img 模式从临时文件读取灰度图。"""
    if mode == "str":
        if not wm_text:
            raise HTTPException(status_code=400, detail="文字模式必须提供 wm_text")
        bwm.read_wm(wm_text, mode="str")
        return len(bwm.wm_bit)
    # img 模式
    if not wm_file_bytes:
        raise HTTPException(status_code=400, detail="图片水印模式必须上传水印图片")
    arr = np.frombuffer(wm_file_bytes, np.uint8)
    wm = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if wm is None:
        raise HTTPException(status_code=400, detail="无法读取水印图片")

    # 图片源：核心已由调用方 read_img 初始化。按容量(80%余量)自动等比缩放水印图，
    # 并在流首部拼 64bit 尺寸头，嵌入必成功且提取端可自动还原宽高。
    # 注意 ca_block_shape 是普通元组 (rows, cols, 4, 4)，不能取 .size。
    blocks = getattr(getattr(bwm, "bwm_core", None), "ca_block_shape", None)
    if blocks and len(blocks) >= 2:
        stream, (h, w) = _build_img_wm_stream(wm, int(blocks[0]) * int(blocks[1]), password_wm=getattr(bwm, "password_wm", 1))
        bwm.bwm_core.read_wm(stream)   # 绕过整流 shuffle：头部必须保持在流首
        return [h, w]

    # 容量未知（理论不发生）：退回按原图直接嵌入
    shape = wm.shape
    tmp = os.path.join(_WMOUT, f"wm_tmp_{uuid.uuid4().hex}.png")
    cv2.imwrite(tmp, wm)
    try:
        bwm.read_wm(tmp, mode="img")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return [shape[0], shape[1]]


def _do_embed(in_tmp, in_wm, kind, src_name, wm_mode, wm_text,
              password_wm, password_img, progress, strength=_DEFAULT_STRENGTH):
    """同步执行嵌入，期间通过 progress(pct,msg) 上报进度。返回 (output_url, shape, key)。"""
    bwm = WaterMark(password_wm=password_wm, password_img=password_img)
    d1, d2 = _apply_strength(bwm, strength)
    wm_bytes = None
    if wm_mode == "img":
        if not in_wm:
            raise HTTPException(status_code=400, detail="图片模式必须上传水印图片")
        with open(in_wm, "rb") as f:
            wm_bytes = f.read()
    # 待嵌入对象：只要源图是图片，就必须先 read_img 初始化 core 的 img / ca_block 结构，
    # 与 wm_mode（文字/图片水印）无关。此前因 elif 关联导致「图片模式+图片源」漏读而崩溃。
    if kind == "image":
        bwm.read_img(in_tmp)

    if progress:
        progress(5, kind == "image" and "正在读取水印…" or "正在准备视频…")
    wm_bits = None
    if wm_mode == "img" and kind == "video":
        # 视频：核心未初始化，按首帧尺寸算容量；缩放+64bit 尺寸头的 bit 流
        # 直接交给逐帧 worker 复用（与图片嵌入同一协议，提取端可自动还原宽高）。
        capc = cv2.VideoCapture(in_tmp)
        okc, f0 = capc.read()
        capc.release()
        if not okc:
            raise HTTPException(status_code=500, detail="无法读取视频首帧")
        fh, fw = f0.shape[:2]
        arr = np.frombuffer(wm_bytes, np.uint8)
        wm_gray = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if wm_gray is None:
            raise HTTPException(status_code=400, detail="无法读取水印图片")
        block_num = (((fh + 1) // 2) // 4) * (((fw + 1) // 2) // 4)
        wm_bits, shp = _build_img_wm_stream(wm_gray, block_num, password_wm=password_wm)
        wm_shape = [shp[0], shp[1]]
    else:
        wm_shape = _load_wm(bwm, wm_mode,
                             wm_text=wm_text,
                             wm_file_bytes=wm_bytes if wm_mode == "img" else None)
        # 视频文本模式：构建与库同协议的乱序 bit 流供 GPU/CPU 复用（否则为 None 会让 GPU 崩溃回退）
        if kind == "video" and wm_mode == "str" and wm_bits is None:
            wm_bits = _text_to_wm_bits(wm_text, password_wm)

    out_name = f"{uuid.uuid4().hex}.png" if kind == "image" else f"{uuid.uuid4().hex}.mp4"
    out_path = os.path.join(_WMOUT, out_name)

    if kind == "image":
        if progress:
            progress(40, "正在嵌入水印…")
        bwm.embed(out_path)
        if progress:
            progress(100, "完成")
    else:
        wm_cfg = {
            "mode": wm_mode,
            "text": wm_text if wm_mode == "str" else "",
            "wm_bytes": wm_bytes if wm_mode == "img" else b"",
            "wm_bits": wm_bits,   # 新协议：带头 bit 流（视频逐帧直接复用）
            "pw1": password_wm,
            "pw2": password_img,
            "d1": d1,
            "d2": d2,
        }
        _embed_video(wm_cfg, in_tmp, out_path, progress=progress)

    wm_key = _store_embed_meta(uuid.uuid4().hex[:10], wm_mode, wm_shape,
                               password_wm, password_img, strength, d1, d2)
    return {
        "ok": True,
        "output_url": f"/files/wmout/{out_name}",
        "wm_shape": wm_shape,
        "wm_key": wm_key,
        "kind": kind,
        "wm_mode": wm_mode,
    }


@router.post("/embed")
async def embed(
    file: UploadFile = File(...),
    wm_mode: str = Form("str"),
    wm_text: str = Form(""),
    wm_file: UploadFile = File(None),
    password_wm: int = Form(1),
    password_img: int = Form(1),
    strength: str = Form(_DEFAULT_STRENGTH),   # 画质档位：quality / balanced / robust
):
    if strength not in _STRENGTH_PRESETS:
        strength = _DEFAULT_STRENGTH
    src_name = file.filename or "input"
    kind = _kind(src_name)
    data = await file.read()
    # 临时文件只用 uuid+扩展名：原始文件名可能含中文/特殊字符，
    # Windows 非 UTF-8 代码页下 cv2.imread/VideoCapture 打不开非 ASCII 路径。
    _ext = os.path.splitext(src_name)[1] or (".mp4" if kind == "video" else ".png")
    in_tmp = os.path.join(_WMOUT, f"in_{uuid.uuid4().hex}{_ext}")
    with open(in_tmp, "wb") as f:
        f.write(data)

    # 图片模式需把水印图片先落盘，供后台线程读取
    in_wm = None
    if wm_mode == "img" and wm_file is not None:
        wm_data = await wm_file.read()
        in_wm = os.path.join(_WMOUT, f"wm_{uuid.uuid4().hex}.png")
        with open(in_wm, "wb") as f:
            f.write(wm_data)

    tid = new_task("wm-embed")

    def runner():
        def progress(pct, msg):
            update(tid, status="running", progress=pct, message=msg)
        try:
            update(tid, status="running", progress=0, message="准备中…")
            res = _do_embed(in_tmp, in_wm, kind, src_name, wm_mode, wm_text,
                            password_wm, password_img, progress, strength)
            update(tid, status="done", progress=100, message="完成", output=res)
        except HTTPException as e:
            update(tid, status="failed", progress=100, message=e.detail or "嵌入失败")
        except Exception as e:  # noqa: BLE001
            update(tid, status="failed", progress=100, message=f"嵌入失败: {e}")
        finally:
            # 上传的临时副本用完即删，避免 data/wmout 持续膨胀
            for _tmp in (in_tmp, in_wm):
                if _tmp:
                    try:
                        os.remove(_tmp)
                    except OSError:
                        pass

    threading.Thread(target=runner, daemon=True).start()
    return {"task_id": tid}


def _serial_embed(wm_cfg, in_path, job_dir, progress=None):
    """单线程兜底嵌入：用于进程池初始化失败时保证功能仍可用。"""
    from _wm_worker import _init_wm, _embed_one  # noqa: PLC0415
    _init_wm(wm_cfg)
    cap = cv2.VideoCapture(in_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    fid = 0
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            cv2.imwrite(os.path.join(job_dir, f"f{fid + 1:06d}.bmp"), _embed_one(fr))
            fid += 1
            if progress:
                pct = int(fid / total * 78) if total else 0
                progress(pct, f"正在嵌入帧 {fid}/{total}…")
    finally:
        cap.release()
    return fid


_GPU_BATCH_BUDGET_MB = 1400   # GPU 嵌入单批显存预算（MB），按分辨率自适应批大小


def _gpu_embed_video(wm_cfg, in_path, job, total, start_id, h, w, progress):
    """GPU 批量嵌入视频帧（deep GPU 嵌入：DWT/DCT/SVD 全上 CUDA）。

    把相邻帧堆成一批交给 _wm_gpu.batch_embed 一次算完，相比逐帧 CPU 多进程
    可显著提速。CUDA 不可用 / 自检失败 / 运行异常时返回 0（上层回退 CPU），
    否则返回已嵌入帧数（含 start_id 之前的计数）。
    """
    import _wm_gpu as G  # noqa: PLC0415
    try:
        if not G.available():
            return 0
    except Exception:  # noqa: BLE001
        return 0
    wcap = cv2.VideoCapture(in_path)
    if not wcap.isOpened():
        return 0
    try:
        bs = max(4, G.embed_batch_size(h, w, _GPU_BATCH_BUDGET_MB))
        fid = start_id
        while True:
            frames = []
            for _ in range(bs):
                ok, fr = wcap.read()
                if not ok:
                    break
                frames.append(fr)
            if not frames:
                break
            try:
                out = G.batch_embed(wm_cfg, frames)
            except Exception:  # noqa: BLE001  单批异常（显存/驱动）→ 整体回退 CPU
                return 0
            for i in range(out.shape[0]):
                cv2.imwrite(os.path.join(job, f"f{fid + 1:06d}.bmp"), out[i])
                fid += 1
                if progress:
                    pct = int(fid / total * 78) if total else 0
                    progress(pct, f"正在嵌入帧 {fid}/{total}…")
        return fid
    finally:
        wcap.release()


def _embed_video(wm_cfg, in_path, out_path, progress=None):
    """逐帧嵌入盲水印，再用 H.264 近无损（crf 10）写回。
    说明：本库的 DWT 盲水印很脆弱，常规有损编码会破坏水印；
    近无损编码才能保证后续可提取，故输出文件体积较大（仅适合短片）。
    嵌入为 CPU 密集且逐帧多秒，故用多进程并行以显著提速；失败自动回退单线程。"""
    cap = cv2.VideoCapture(in_path)
    if not cap.isOpened():
        raise HTTPException(status_code=500, detail="无法读取视频")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if w <= 0 or h <= 0:
        raise HTTPException(status_code=500, detail="视频尺寸无效")

    job = os.path.join(_WMOUT, f"vjob_{uuid.uuid4().hex}")
    os.makedirs(job, exist_ok=True)

    # ffmpeg 近无损编码器路径（由 imageio_ffmpeg 提供静态 ffmpeg）
    try:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001
        if os.path.exists(job):
            shutil.rmtree(job, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"缺少 ffmpeg: {e}")

    # 首选 GPU 批量嵌入（DWT/DCT/SVD 上 CUDA，一次算一批帧，速度数倍于 CPU）；
    # GPU 不可用/自检失败/显存不足时自动回退多进程，再兜底单线程。
    frame_id = _gpu_embed_video(wm_cfg, in_path, job, total, 0, h, w, progress)
    if frame_id == 0:
        try:
            # CPU 多进程并行嵌入。中间帧用无损 BMP 落盘（BMP 无压缩、编码极快，
            # 相比 PNG 显著省时），再交给 ffmpeg 统一合成为近无损 H.264。
            from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed  # noqa: PLC0415
            import multiprocessing as mp  # noqa: PLC0415
            from _wm_worker import _init_wm, _embed_one  # noqa: PLC0415
            ncores = os.cpu_count() or 2
            workers = max(1, min(ncores, 12))   # 充分利用多核（帧相互独立）
            ctx = mp.get_context("spawn")
            batch = max(24, workers * 5)        # 加大批处理，减少进程池往返同步
            wcap = cv2.VideoCapture(in_path)
            # 并行写盘线程池：嵌入结果先丢给独立线程写 BMP，主线程不阻塞，
            # 让嵌入 worker 持续满负荷，避免磁盘 IO 拖慢整体吞吐。
            writer = ThreadPoolExecutor(max_workers=max(2, min(workers, 6)))
            writer_futs = []
            try:
                with ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
                                         initializer=_init_wm, initargs=(wm_cfg,)) as ex:
                    while True:
                        frames = []
                        for _ in range(batch):
                            ok, fr = wcap.read()
                            if not ok:
                                break
                            frames.append(fr)
                        if not frames:
                            break
                        for out in ex.map(_embed_one, frames):
                            writer_futs.append(writer.submit(
                                cv2.imwrite, os.path.join(job, f"f{frame_id + 1:06d}.bmp"), out))
                            frame_id += 1
                            if progress:
                                pct = int(frame_id / total * 78) if total else 0
                                progress(pct, f"正在嵌入帧 {frame_id}/{total}…")
                        # 回收本批写盘任务，避免无界堆积
                        for fut in writer_futs:
                            fut.result()
                        writer_futs.clear()
            finally:
                for fut in writer_futs:
                    fut.result() or None
                writer.shutdown(wait=True)
                wcap.release()
        except Exception:  # noqa: BLE001  进程池失败（spawn/资源受限）→ 单线程兜底
            frame_id = _serial_embed(wm_cfg, in_path, job, progress)

    if frame_id == 0:
        shutil.rmtree(job, ignore_errors=True)
        raise HTTPException(status_code=500, detail="视频无有效帧")

    if progress:
        progress(82, "正在合成视频…")
    pattern = os.path.join(job, "f%06d.bmp")
    if _use_nvenc(ffmpeg):
        # 有 NVIDIA 显卡 → 用 NVENC 硬件编码，编码阶段 GPU 加速
        # （嵌入与编码均可用 GPU；此处只负责编码合成）。p5 比 p4 更快，近无损画质足够保水印。
        enc = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "12", "-b:v", "0"]
    else:
        enc = ["-c:v", "libx264", "-crf", "10", "-preset", "faster"]
    cmd = [
        ffmpeg, "-y", "-threads", "0", "-framerate", str(fps), "-i", pattern,
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        *enc,
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    shutil.rmtree(job, ignore_errors=True)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise HTTPException(status_code=500, detail=f"视频编码失败: {r.stderr.decode('utf-8', 'replace')[-400:]}")
    if progress:
        progress(100, "完成")


def _img_raw_bits(img, d1, d2, password_img):
    """用指定强度档位从含水印图解出原始 bit 序列（3通道平均，长度=块数）。"""
    from blind_watermark.bwm_core import WaterMarkCore  # noqa: PLC0415
    core = WaterMarkCore(password_img=password_img)
    core.d1, core.d2 = int(d1), int(d2)
    return core.extract_raw(img=img).mean(axis=0)


def _parse_hdr(raw, repeats):
    """从原始 bit 序列流首解析尺寸头（逐 bit 多数投票）。返回 (h, w) 或 None。"""
    n = 64 * repeats
    if raw.size < n:
        return None
    seg = raw[:n].reshape(repeats, 64).mean(axis=0) > 0.5
    try:
        h = int("".join("1" if b else "0" for b in seg[:32]), 2)
        w = int("".join("1" if b else "0" for b in seg[32:64]), 2)
    except ValueError:
        return None
    if not (4 <= h <= 8192 and 4 <= w <= 8192):
        return None
    return h, w


def _decode_img_from_raw(raw, h, w, password_wm, out_path, repeats=None):
    """按真实流长(头+载荷)折叠平均，再对载荷做逆置换还原水印图并落盘。
    配合 _build_img_wm_stream 的新协议（头部原位冗余 + 载荷乱序）。"""
    repeats = repeats if repeats is not None else _hdr_repeats(raw.size // 1)
    hdr_len = 64 * repeats
    stream_len = hdr_len + h * w
    if stream_len > raw.size:
        # 尝试另一种头部冗余档（源图容量边界情形）
        for alt in (1, _WM_HDR_REPEAT):
            if 64 * alt + h * w <= raw.size:
                repeats, hdr_len, stream_len = alt, 64 * alt, 64 * alt + h * w
                break
        else:
            raise ValueError("源图容量不足以承载该水印")
    wm_avg = np.zeros(stream_len)
    for i in range(stream_len):
        wm_avg[i] = raw[i::stream_len].mean()
    enc = wm_avg[hdr_len:]
    idx = np.arange(enc.size)
    np.random.RandomState(password_wm).shuffle(idx)
    payload = np.zeros(enc.size, dtype=bool)
    payload[idx] = enc > 0.5
    cv2.imwrite(out_path, (255 * payload.astype(np.uint8)).reshape(h, w))
    return out_path


def _auto_extract_img_header(img, password_wm, password_img):
    """新版图片水印嵌入流自带冗余尺寸头（原位不乱序）。
    先从流首投票解析尺寸，再按真实流长折叠平均还原水印；三档强度择优。
    返回 (conf, 档位, 输出路径, h, w) 或 None。"""
    best = None
    for key in ("quality", "balanced", "robust"):
        d1, d2 = _STRENGTH_PRESETS[key]
        try:
            raw = _img_raw_bits(img, d1, d2, password_img)
            # 优先 8 遍冗余头投票；容量小/旧版单头再试 1 遍
            hw = _parse_hdr(raw, _WM_HDR_REPEAT) or _parse_hdr(raw, 1)
            if hw is None:
                continue
            h, w = hw
            out = os.path.join(_WMOUT, f"wm_{uuid.uuid4().hex}.png")
            _decode_img_from_raw(raw, h, w, password_wm, out)
            # 置信度：解码区间均值偏离 0.5 的程度，越大越可信
            for alt in (_WM_HDR_REPEAT, 1):
                L = 64 * alt + h * w
                if L <= raw.size:
                    break
            wm_avg = np.zeros(L)
            for i in range(L):
                wm_avg[i] = raw[i::L].mean()
            conf = float(np.abs(wm_avg[64 * alt:] - 0.5).mean())
            if best is None or conf > best[0]:
                best = (conf, key, out, h, w)
        except Exception:  # noqa: BLE001
            continue
    return best


def _do_extract(in_tmp, kind, wm_key, wm_mode, wm_shape, password_wm,
                password_img, progress, strength=_DEFAULT_STRENGTH):
    """同步执行提取，期间上报进度。返回结果 dict。"""
    # 优先用「提取识别码」自动带入嵌入时保存的参数，用户无需填写任何专业项。
    saved = _pop_embed_meta(wm_key.strip()) if wm_key.strip() else None
    if saved is not None:
        wm_mode = saved.get("wm_mode", wm_mode)
        password_wm = saved.get("password_wm", password_wm)
        password_img = saved.get("password_img", password_img)
        # 提取必须使用与嵌入相同的强度：优先取保存的 d1/d2；
        # 旧版本元数据没有强度字段，说明是用库默认 (36,20) 嵌入的，回退 robust 档。
        if saved.get("d1") and saved.get("d2"):
            strength = None  # 下面直接用 d1/d2
        else:
            strength = "robust"
        if wm_mode == "str":
            wm_shape = str(saved.get("wm_shape", ""))
        else:
            shp = saved.get("wm_shape")
            wm_shape = f"{shp[0]},{shp[1]}" if isinstance(shp, (list, tuple)) and len(shp) == 2 else str(shp)

    if progress:
        progress(10, "正在解析参数…")
    # 文本模式：若用户未填写水印长度，则自动推断（见 _do_auto_str_length）。
    auto_length = wm_mode == "str" and not wm_shape
    shape = None
    if wm_mode == "str" and not auto_length:
        try:
            shape = int(wm_shape)
        except ValueError:
            raise HTTPException(status_code=400, detail="文字模式需要数字型水印长度")
    elif wm_mode == "img" and wm_shape:
        try:
            hh, ww = [int(x) for x in wm_shape.split(",")]
        except Exception:
            raise HTTPException(status_code=400, detail="图片模式水印宽高需为 高,宽")
        shape = (hh, ww)
    # img 且未填宽高 → shape 保持 None，走自动尺寸头还原（新版嵌入协议）

    img = None
    if kind == "image":
        if progress:
            progress(40, "正在读取图像…")
        img = cv2.imread(in_tmp, flags=cv2.IMREAD_COLOR)
    else:
        if progress:
            progress(30, "正在读取视频首帧…")
        cap = cv2.VideoCapture(in_tmp)
        ok, first = cap.read()
        cap.release()
        if not ok:
            raise HTTPException(status_code=500, detail="无法读取视频首帧")
        img = first
        if progress:
            progress(55, "正在提取水印…")

    if progress:
        progress(60, "正在提取水印…")

    def _run_once(d1, d2):
        out_wm = None
        if wm_mode == "img":
            # 新协议解码：折叠平均 + 载荷逆置换（与 _build_img_wm_stream 对应）
            out_wm = os.path.join(_WMOUT, f"wm_{uuid.uuid4().hex}.png")
            raw = _img_raw_bits(img, d1, d2, password_img)
            _decode_img_from_raw(raw, shape[0], shape[1], password_wm, out_wm)
            text = None
        else:
            bwm = WaterMark(password_wm=password_wm, password_img=password_img)
            bwm.bwm_core.d1, bwm.bwm_core.d2 = int(d1), int(d2)
            text = bwm.extract(embed_img=img, wm_shape=shape, mode="str")
        return text, out_wm

    matched = None
    auto_len_result = None
    if saved is not None and strength is None:
        # 识别码路径：直接用嵌入时保存的强度
        text, out_wm = _run_once(saved["d1"], saved["d2"])
    elif auto_length and wm_mode == "str":
        # 文本模式且未填长度：自动推断长度（连同强度一起定位），用户无需回忆长度/口令外的任何参数
        key, used_len, text = _auto_str_length(img, password_wm, password_img)
        matched = key
        out_wm = None
        auto_len_result = used_len
    elif wm_mode == "img" and shape is None:
        # 新版嵌入协议：流首部带 64bit 尺寸头，无需宽高/识别码即可自动还原
        best = _auto_extract_img_header(img, password_wm, password_img)
        if best is None:
            raise HTTPException(
                status_code=400,
                detail="未能自动识别水印尺寸（可能是旧版本嵌入或画面损失较大）。请使用识别码提取，或手动填写宽高（高,宽）")
        _, matched, out_wm, _ah, _aw = best
        text = None
    else:
        best = None
        for key in ("quality", "balanced", "robust"):
            d1, d2 = _STRENGTH_PRESETS[key]
            try:
                ttext, tout = _run_once(d1, d2)
            except Exception:  # noqa: BLE001
                continue
            score = _score_wm_text(ttext) if wm_mode == "str" else _score_wm_img(tout)
            if best is None or score > best[0]:
                best = (score, key, ttext, tout)
        if best is None:
            raise HTTPException(status_code=500, detail="提取失败：三档嵌入强度均解码失败，请确认文件含水印且参数正确")
        _, matched, text, out_wm = best

    if progress:
        progress(100, "完成")
    return {
        "ok": True,
        "wm_mode": wm_mode,
        "text": text if wm_mode == "str" else None,
        "wm_output_url": f"/files/wmout/{os.path.basename(out_wm)}" if out_wm else None,
        "matched_strength": matched,
        "auto_length": auto_len_result,
    }


@router.post("/extract")
async def extract(
    file: UploadFile = File(...),
    wm_mode: str = Form("str"),
    wm_shape: str = Form(""),          # str: 位长整数；img: "h,w"
    password_wm: int = Form(1),
    password_img: int = Form(1),
    wm_key: str = Form(""),            # 「提取识别码」：命中则自动带入以上全部参数
    strength: str = Form(_DEFAULT_STRENGTH),  # 兼容保留：手动提取现在会自动尝试三档匹配，无需传入
):
    if strength not in _STRENGTH_PRESETS:
        strength = _DEFAULT_STRENGTH
    src_name = file.filename or "input"
    kind = _kind(src_name)
    data = await file.read()
    # 临时文件只用 uuid+扩展名：原始文件名可能含中文/特殊字符，
    # Windows 非 UTF-8 代码页下 cv2.imread/VideoCapture 打不开非 ASCII 路径。
    _ext = os.path.splitext(src_name)[1] or (".mp4" if kind == "video" else ".png")
    in_tmp = os.path.join(_WMOUT, f"in_{uuid.uuid4().hex}{_ext}")
    with open(in_tmp, "wb") as f:
        f.write(data)

    tid = new_task("wm-extract")

    def runner():
        def progress(pct, msg):
            update(tid, status="running", progress=pct, message=msg)
        try:
            update(tid, status="running", progress=0, message="准备中…")
            res = _do_extract(in_tmp, kind, wm_key, wm_mode, wm_shape,
                              password_wm, password_img, progress, strength)
            update(tid, status="done", progress=100, message="完成", output=res)
        except HTTPException as e:
            update(tid, status="failed", progress=100, message=e.detail or "提取失败")
        except Exception as e:  # noqa: BLE001
            update(tid, status="failed", progress=100, message=f"提取失败: {e}")
        finally:
            try:
                os.remove(in_tmp)
            except OSError:
                pass

    threading.Thread(target=runner, daemon=True).start()
    return {"task_id": tid}