"""
DLSS5 视觉增强适配层：把 DLSS 的处理能力并入本项目 FastAPI 后端（单进程）。

设计要点：
- 懒加载：本模块顶部不 import 任何 DLSS 模块；只有首次调用增强接口时才
  加载 DLSS 处理（并把 dlss/ 加入 sys.path），因此不会拖慢后端启动。
- 任务轮询：复用 task_store 的状态机制，处理放在后台线程。
- 输出目录：dlss/outputs/，通过 main.py 挂载的 /enhanceout 静态地址下载。
"""
import os
import sys
import uuid
import re
import threading
import datetime
import subprocess

from typing import List, Optional

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel

from task_store import new_task, get, update

# ---------------- 路径 ----------------
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(_BACKEND_DIR, ".."))
_DLSS_ROOT = os.path.join(_ROOT, "dlss")
_OUTPUT_DIR = os.path.join(_DLSS_ROOT, "outputs")
_CONFIG_PATH = os.path.join(_DLSS_ROOT, "config", "config.ini")
_CONFIG_DIR = os.path.join(_DLSS_ROOT, "config")
_UPLOAD_DIR = os.path.join(_ROOT, "data", "uploads")
os.makedirs(_UPLOAD_DIR, exist_ok=True)
os.makedirs(_CONFIG_DIR, exist_ok=True)

router = APIRouter(prefix="/api/enhance", tags=["enhance"])

# ---------------- 懒加载 DLSS 处理 ----------------
_init_lock = threading.Lock()
_init_state = {"done": False, "ready": False, "error": None, "capabilities": None, "probe_done": False}


def _warmup() -> None:
    """后端启动时在后台线程预热 DLSS 运行时，避免用户首次进入 DLSS 页时才加载。
    非阻塞、幂等；无 GPU 或初始化失败只会记录 error，不影响后端启动。"""
    threading.Thread(target=_ensure_loaded, kwargs={"wait": True}, daemon=True).start()


def _ensure_loaded(*, wait: bool = False) -> bool:
    """确保 DLSS 处理已初始化（prepare_runtime）。返回 True = 就绪。

    高效加载策略：
    - 就绪 = 原生运行时加载成功（快，约 1s）。
    - 帧插值能力探测（慢，子进程探测 GPU/HAGS）在后台线程异步执行，不阻塞就绪；
      图片/视频增强可在探测完成前立即使用。
    """
    if _init_state["done"]:
        return _init_state["ready"]
    with _init_lock:
        if _init_state["done"]:
            return _init_state["ready"]
        if _init_state.get("starting"):
            # 初始化线程已在运行：直接返回未就绪，不再重复起线程（避免惊群争用独占型原生运行时）
            return False
        _init_state["starting"] = True
        if not _DLSS_ROOT in sys.path:
            sys.path.insert(0, _DLSS_ROOT)

        def _init():
            try:
                from src.core.runtime import prepare_runtime
                prepare_runtime()
                # 核心运行时就绪：图片/视频增强立即可用
                _init_state.update(done=True, ready=True, error=None)
                # 能力探测放后台，不拖慢“就绪”
                threading.Thread(target=_probe_caps_thread, daemon=True).start()
            except Exception as e:  # noqa: BLE001
                _init_state.update(done=True, ready=False, error=f"{e}")
            finally:
                _init_state["starting"] = False

        if wait:
            _init()
        else:
            threading.Thread(target=_init, daemon=True).start()
        return _init_state["ready"]


def _probe_caps_thread():
    """后台探测帧插值能力，完成后填充 capabilities；不阻塞 ready。"""
    try:
        caps = _load_capabilities()
        _init_state["capabilities"] = caps
    except Exception as e:  # noqa: BLE001
        _init_state["capabilities"] = {"frame_interpolation": {"available": False, "detail": f"{e}"}}
    finally:
        _init_state["probe_done"] = True


def _require_ready():
    if not _ensure_loaded(wait=True):
        raise HTTPException(status_code=500, detail=_init_state.get("error") or "DLSS 运行时初始化失败")


def _load_capabilities():
    caps = {}
    try:
        from src.frame_interpolation.capabilities import probe_frame_interpolation_capabilities
        p = probe_frame_interpolation_capabilities()
        caps["frame_interpolation"] = {
            "available": bool(getattr(p, "available", False)),
            "gpu": getattr(p, "gpu", ""),
            "driver": getattr(p, "driver", ""),
            "hags_enabled": bool(getattr(p, "hags_enabled", False)),
            "native_generated_frame_max": getattr(p, "native_generated_frame_max", 0),
            "native_multiplier": getattr(p, "native_multiplier", 0),
            "cascade_available": bool(getattr(p, "cascade_available", False)),
            "runtime_version": getattr(p, "runtime_version", ""),
            "signature_status": getattr(p, "signature_status", ""),
            "detail": getattr(p, "detail", ""),
        }
    except Exception as e:  # noqa: BLE001
        caps["frame_interpolation"] = {"available": False, "detail": f"{e}"}
    return caps


# ---------------- 状态 ----------------
@router.get("/status")
def enhance_status():
    if not _init_state["done"]:
        _ensure_loaded()
        return {"ready": False, "initializing": True, "done": False}
    return {"ready": _init_state["ready"], "initializing": False,
            "done": True, "error": _init_state["error"],
            "probe_done": _init_state["probe_done"],
            "capabilities": _init_state["capabilities"]}


@router.post("/probe")
def enhance_probe():
    """重新探测帧插值能力：用户开启 HAGS 等系统设置后，无需重启后端即可重试。"""
    _require_ready()
    try:
        from src.frame_interpolation.capabilities import clear_capability_cache
        clear_capability_cache()
    except Exception:  # noqa: BLE001
        pass
    caps = _load_capabilities()
    _init_state["capabilities"] = caps
    _init_state["probe_done"] = True
    return {"ok": True, "capabilities": caps}


@router.get("/gpus")
def enhance_gpus():
    """返回已检测到的显卡列表，供「设置 → 运行设备」下拉填充。
    仅列出 DLSS 可用的显卡（对应 ai_gpu_uuid）；无可用显卡时 ok=False。"""
    gpus = []
    ok = False
    detail = ""
    try:
        from src.core.gpu_detection import detect_gpus
        detected = detect_gpus()
        ok = True
        for g in detected:
            gpus.append({
                "uuid": str(g.get("uuid", "")),
                "name": g.get("name", ""),
                "display_name": g.get("display_name") or g.get("name", ""),
                "memory_mb": g.get("memory_mb", 0),
                "ai_compatible": bool(g.get("ai_compatible", False)),
            })
    except Exception as e:  # noqa: BLE001
        ok = False
        detail = f"{e}"
    return {"ok": ok, "detail": detail, "gpus": gpus}


# ---------------- 工具 ----------------
_RE_JUNK_PREFIX = re.compile(r"(?:enh_\d+_)+")
_RE_AUTO_SUFFIX = re.compile(r"_DLSS5_IMAGE_\d{8}-\d{6}-\d{6}-\d{4}")


def _clean_upload_name(name: str) -> str:
    """清洗上传文件名：去掉历史下载文件里堆积的临时前缀与重复的 Auto 重命名段，
    避免反复增强同一文件时输出文件名越叠越长（如 原名_DLSS5_IMAGE_A_DLSS5_IMAGE_B.png）。"""
    stem, ext = os.path.splitext(name or "")
    stem = _RE_JUNK_PREFIX.sub("", stem)
    hits = _RE_AUTO_SUFFIX.findall(stem)
    if len(hits) > 1:
        stem = _RE_AUTO_SUFFIX.sub("", stem) + hits[-1]
    return (stem.strip().rstrip(".") or "input") + ext


def _save(files: List[UploadFile]) -> List[str]:
    # 每次请求存入独立子目录，保留原始文件名（同名时追加 (1)(2) 去重），
    # 不再把时间戳前缀揉进文件名污染输出。
    sub = os.path.join(_UPLOAD_DIR, datetime.datetime.now().strftime("%H%M%S%f"))
    os.makedirs(sub, exist_ok=True)
    paths = []
    for f in files:
        base = _clean_upload_name(os.path.basename(f.filename or "input"))
        stem, ext = os.path.splitext(base)
        dest = os.path.join(sub, base)
        k = 1
        while os.path.exists(dest):
            dest = os.path.join(sub, f"{stem}({k}){ext}")
            k += 1
        with open(dest, "wb") as out:
            out.write(f.file.read())
        paths.append(dest)
    return paths


def _url(name: str) -> str:
    # 文件名可能来自用户上传的原名，含 # ? % 空格等字符；# 会截断 URL、% 破坏
    # 百分号编码，导致结果 404（裂图/黑视频）。这里统一做 URL 编码保证可访问。
    from urllib.parse import quote
    return f"/enhanceout/{quote(name)}"


def _run(tid: str, fn):
    def wrapper():
        update(tid, status="running", progress=0, message="准备中…")
        try:
            extra = fn()
            update(tid, status="done", progress=100, message="完成", **extra)
        except Exception as e:  # noqa: BLE001
            update(tid, status="failed", progress=100, message=f"{e}")
    threading.Thread(target=wrapper, daemon=True).start()


def _manifest(files):
    return [{"name": os.path.basename(p), "url": _url(os.path.basename(p))} for p in files]


# ===================== 图片增强 =====================
@router.post("/image")
async def enhance_image(
    files: List[UploadFile] = File(...),
    nr_preset: str = Form("Default"),
    nr_style: str = Form("Default"),
    nr_intensity: float = Form(1.0),
    local_tone_strength: float = Form(1.0),
    local_structure_strength: float = Form(1.0),
    skin_structure_strength: float = Form(-1.0),
    upscaling_factor: float = Form(1.0),
    automatic_mask: str = Form("Off"),
    dlss_model_preset: str = Form("Default"),
    output_format: str = Form("PNG"),
    quality: int = Form(95),
    rename_mode: str = Form("Auto"),
    custom_suffix: str = Form("_DLSS5"),
    ai_gpu_uuid: str = Form("auto"),
):
    _require_ready()
    if not files:
        raise HTTPException(status_code=400, detail="至少选择一个图片")
    inputs = _save(files)
    tid = new_task("job")

    def work():
        from src.image.batch import convert_images
        from src.image.models import ImageConversionOptions
        options = ImageConversionOptions(
            ai_gpu_uuid=ai_gpu_uuid,
            nr_preset=nr_preset, nr_style=nr_style,
            nr_intensity=nr_intensity, local_tone_strength=local_tone_strength,
            local_structure_strength=local_structure_strength,
            skin_structure_strength=skin_structure_strength,
            upscaling_factor=upscaling_factor,
            automatic_mask=automatic_mask == "On",
            dlss_model_preset=dlss_model_preset,
            output_format=output_format, quality=int(quality),
            rename_mode=rename_mode, custom_suffix=custom_suffix,
        )
        res = convert_images(inputs, options, progress=_make_progress(tid))
        files_out = [it.output_path for it in res.successes]
        failures = [{"input": os.path.basename(it.input_path), "error": it.error} for it in res.failures]
        extra = {"outputs": _manifest(files_out), "failures": failures}
        if res.zip_path and os.path.exists(res.zip_path):
            extra["zip"] = {"name": os.path.basename(res.zip_path), "url": _url(os.path.basename(res.zip_path))}
        if res.cancelled:
            extra["cancelled"] = True
        return extra

    _run(tid, work)
    return {"task_id": tid}


# ===================== 实时预览（轻量快速通道） =====================
# 与 /image 全量管线的区别：不建任务、不打包 zip/清单/报告、不落盘输出，
# 单帧渲染后直接把 JPEG 字节返回给前端，省去轮询与文件管理开销。
_preview_lock = threading.Lock()


@router.post("/preview")
async def enhance_preview(
    file: UploadFile = File(...),
    nr_preset: str = Form("Default"),
    nr_style: str = Form("Default"),
    nr_intensity: float = Form(1.0),
    local_tone_strength: float = Form(1.0),
    local_structure_strength: float = Form(1.0),
    skin_structure_strength: float = Form(-1.0),
    upscaling_factor: float = Form(1.0),
    automatic_mask: str = Form("Off"),
    dlss_model_preset: str = Form("Default"),
    ai_gpu_uuid: str = Form("auto"),
):
    _require_ready()
    # GPU 上同一时间只能跑一个 DLSS 会话：已有预览在途时直接让新请求跳过，
    # 前端会丢弃过期响应并等待下一次（防抖后的）请求。
    if not _preview_lock.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="预览排队中")
    ts = datetime.datetime.now().strftime("%H%M%S%f")
    src = os.path.join(_UPLOAD_DIR, f"prev_{ts}.jpg")
    try:
        with open(src, "wb") as out:
            out.write(await file.read())

        def work() -> bytes:
            import io
            import numpy as np
            import cv2
            from PIL import Image
            from pathlib import Path as _P
            from src.image.decoder import decode_image
            from src.image.models import ImageConversionOptions
            from src.core.runtime import (
                DLSSFrameSession, resize_fit, resolve_native_settings,
                resolve_upscaling_mode, resolve_output_size, prepare_runtime,
            )
            from src.core.gpu_selection import resolve_runtime_ai_gpu
            from src.core.jobs import active_job

            options = ImageConversionOptions(
                ai_gpu_uuid=ai_gpu_uuid, nr_preset=nr_preset, nr_style=nr_style,
                nr_intensity=nr_intensity, local_tone_strength=local_tone_strength,
                local_structure_strength=local_structure_strength,
                skin_structure_strength=skin_structure_strength,
                upscaling_factor=upscaling_factor, output_format="JPEG", quality=95,
                automatic_mask=automatic_mask == "On", dlss_model_preset=dlss_model_preset,
            )
            decoded = decode_image(_P(src))
            height, width = decoded.rgba.shape[:2]
            if width < 64 or height < 64:
                raise ValueError("图片尺寸过小（DLSS 要求至少 64 像素）")
            prepared = prepare_runtime()
            out_w, out_h = resolve_output_size(width, height, options.upscaling_factor)
            factor, mode = resolve_upscaling_mode(options.upscaling_factor)
            native = resolve_native_settings(options)

            import time as _t
            deadline = _t.time() + 60  # 最多等 60s：等正在跑的真实增强/其他预览释放 GPU 槽位
            while True:
                try:
                    with active_job() as controller:
                        gpu = resolve_runtime_ai_gpu(
                            prepared.gpus, prepared.runtime_bundle, options.ai_gpu_uuid
                        )
                        session = DLSSFrameSession(
                            input_width=width, input_height=height,
                            output_width=out_w, output_height=out_h,
                            frame_count=1, warmup_frames=0,
                            factor=factor, mode=mode, native_settings=native,
                            gpu=gpu, runtime_bundle=prepared.runtime_bundle, controller=controller,
                        )
                        try:
                            motion = np.zeros(
                                (session.render_height, session.render_width, 2), dtype=np.float16
                            )
                            render_rgba = resize_fit(decoded.rgba, session.render_width, session.render_height)
                            processed, _pts = session.process(
                                index=0, rgba=render_rgba, motion=motion, reset=True, pts=0
                            )
                            if decoded.alpha.shape == (out_h, out_w):
                                processed[..., 3] = decoded.alpha
                            else:
                                processed[..., 3] = cv2.resize(
                                    decoded.alpha, (out_w, out_h), interpolation=cv2.INTER_LANCZOS4
                                )
                        finally:
                            try:
                                session.close()
                            except Exception:
                                session.abort()
                    break
                except RuntimeError as e:
                    if "already running" not in str(e).lower():
                        raise
                    if _t.time() > deadline:
                        raise RuntimeError("GPU 忙碌且等待超时，请稍后再试")
                    _t.sleep(0.4)
            img = Image.fromarray(processed, mode="RGBA")
            try:
                bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
                bg.alpha_composite(img)
                buf = io.BytesIO()
                bg.convert("RGB").save(buf, format="JPEG", quality=95, subsampling=0)
                return buf.getvalue()
            finally:
                img.close()
                bg.close()

        jpeg = await run_in_threadpool(work)
        return Response(content=jpeg, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{e}")
    finally:
        try:
            os.remove(src)
        except OSError:
            pass
        _preview_lock.release()


# ===================== 视频增强 =====================
@router.post("/video")
async def enhance_video(
    files: List[UploadFile] = File(...),
    nr_preset: str = Form("Default"),
    nr_style: str = Form("Default"),
    nr_intensity: float = Form(1.0),
    local_tone_strength: float = Form(1.0),
    local_structure_strength: float = Form(1.0),
    skin_structure_strength: float = Form(-1.0),
    upscaling_factor: float = Form(1.0),
    automatic_mask: str = Form("Off"),
    dlss_model_preset: str = Form("Default"),
    codec: str = Form("H.264"),
    container: str = Form("MP4"),
    quality: str = Form("Auto (Default)"),
    hdr_mode: str = Form("false"),
    rename_mode: str = Form("Auto"),
    custom_suffix: str = Form("_DLSS5"),
    ai_gpu_uuid: str = Form("auto"),
):
    _require_ready()
    if not files:
        raise HTTPException(status_code=400, detail="至少选择一个视频")
    inputs = _save(files)
    tid = new_task("job")

    def work():
        from src.video.batch import convert_videos
        from src.video.models import ConversionOptions
        options = ConversionOptions(
            ai_gpu_uuid=ai_gpu_uuid,
            nr_preset=nr_preset, nr_style=nr_style,
            nr_intensity=nr_intensity, local_tone_strength=local_tone_strength,
            local_structure_strength=local_structure_strength,
            skin_structure_strength=skin_structure_strength,
            upscaling_factor=upscaling_factor,
            automatic_mask=automatic_mask == "On",
            dlss_model_preset=dlss_model_preset,
            codec=codec, container=container, quality=quality,
            preserve_hdr=hdr_mode.lower() == "true",
            rename_mode=rename_mode, custom_suffix=custom_suffix,
        )
        res = convert_videos(inputs, options, progress=_make_progress(tid))
        files_out = [it.result.output_path for it in res.successes]
        failures = [{"input": os.path.basename(it.input_path), "error": it.error} for it in res.failures]
        return {"outputs": _manifest(files_out), "failures": failures,
                "cancelled": res.cancelled}

    _run(tid, work)
    return {"task_id": tid}


# ===================== 视频前 3 秒预览 =====================
@router.post("/video_preview")
async def enhance_video_preview(
    files: List[UploadFile] = File(...),
    duration: float = Form(3.0),
    nr_preset: str = Form("Default"),
    nr_style: str = Form("Default"),
    nr_intensity: float = Form(1.0),
    local_tone_strength: float = Form(1.0),
    local_structure_strength: float = Form(1.0),
    skin_structure_strength: float = Form(-1.0),
    upscaling_factor: float = Form(1.0),
    automatic_mask: str = Form("Off"),
    dlss_model_preset: str = Form("Default"),
    codec: str = Form("H.264"),
    container: str = Form("MP4"),
    quality: str = Form("Auto (Default)"),
    hdr_mode: str = Form("false"),
    rename_mode: str = Form("Auto"),
    custom_suffix: str = Form("_DLSS5"),
    ai_gpu_uuid: str = Form("auto"),
):
    """只渲染视频前 N 秒（默认 3 秒），让用户先看实际效果再决定参数。"""
    _require_ready()
    if not files:
        raise HTTPException(status_code=400, detail="至少选择一个视频")
    seconds = max(1.0, min(10.0, float(duration)))
    inputs = _save(files[:1])
    tid = new_task("job")

    def _trim(src: str) -> str:
        from src.core.paths import FFMPEG
        ffmpeg = str(FFMPEG) if os.path.exists(str(FFMPEG)) else "ffmpeg"
        stem = os.path.splitext(os.path.basename(src))[0]
        dest = os.path.join(os.path.dirname(src), f"预览_{stem}.mp4")
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", src,
               "-t", f"{seconds:.3f}", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "20", "-c:a", "aac", dest]
        proc = subprocess.run(cmd, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if proc.returncode != 0 or not os.path.exists(dest):
            err = proc.stderr.decode("utf-8", "replace")[-300:]
            raise RuntimeError(f"截取前 {seconds:g} 秒失败：{err}")
        return dest

    def work():
        from src.video.batch import convert_videos
        from src.video.models import ConversionOptions
        options = ConversionOptions(
            ai_gpu_uuid=ai_gpu_uuid,
            nr_preset=nr_preset, nr_style=nr_style,
            nr_intensity=nr_intensity, local_tone_strength=local_tone_strength,
            local_structure_strength=local_structure_strength,
            skin_structure_strength=skin_structure_strength,
            upscaling_factor=upscaling_factor,
            automatic_mask=automatic_mask == "On",
            dlss_model_preset=dlss_model_preset,
            codec=codec, container=container, quality=quality,
            preserve_hdr=hdr_mode.lower() == "true",
            rename_mode=rename_mode, custom_suffix=custom_suffix,
        )
        trimmed = []
        try:
            trimmed.append(_trim(inputs[0]))
            res = convert_videos(trimmed, options, progress=_make_progress(tid))
        finally:
            for p in trimmed:
                try:
                    os.remove(p)
                except OSError:
                    pass
        files_out = [it.result.output_path for it in res.successes]
        failures = [{"input": os.path.basename(it.input_path), "error": it.error} for it in res.failures]
        return {"outputs": _manifest(files_out), "failures": failures,
                "cancelled": res.cancelled, "preview": True}

    _run(tid, work)
    return {"task_id": tid}


# ===================== 帧插值 =====================
@router.post("/interpolate")
async def enhance_interpolate(
    files: List[UploadFile] = File(...),
    target_fps: str = Form("60"),
    engine: str = Form("Auto"),
    codec: str = Form("H.264"),
    container: str = Form("MP4"),
    quality: str = Form("Auto (Default)"),
    hdr_mode: str = Form("false"),
    rename_mode: str = Form("Auto"),
    custom_suffix: str = Form("_DLSSFG"),
    ai_gpu_uuid: str = Form("auto"),
):
    _require_ready()
    if not files:
        raise HTTPException(status_code=400, detail="至少选择一个视频")
    inputs = _save(files)
    tid = new_task("job")

    # 软件光流补帧（ffmpeg minterpolate）：DLSS 帧生成仅支持 RTX 40+，
    # 其余 GPU（如 RTX 30 系）自动回退到本路径，保证插帧功能可用。
    def _software_interp_one(src, fps, quality, out_path, report):
        from src.core.paths import FFMPEG, FFPROBE
        probe = subprocess.run(
            [str(FFPROBE), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", src],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        total = float(probe.stdout.strip() or 0)
        crf = {"Max": 16, "Best": 18}.get(quality, 20 if quality.startswith("Auto") else 23)
        cmd = [str(FFMPEG), "-y", "-i", src,
               "-vf", f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1",
               "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
               "-c:a", "copy", "-movflags", "+faststart",
               "-progress", "pipe:1", "-nostats", out_path]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=subprocess.CREATE_NO_WINDOW)
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                try:
                    done = float(line.strip().split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                if total > 0:
                    report(done / total, f"软件光流补帧 {fps}FPS · {done:.0f}s / {total:.0f}s")
        proc.wait()
        if proc.returncode or not os.path.isfile(out_path):
            raise RuntimeError(f"软件补帧失败（ffmpeg 退出码 {proc.returncode}）")
        return out_path

    def work():
        try:
            from src.frame_interpolation.capabilities import clear_capability_cache
            clear_capability_cache()
            caps = _load_capabilities()
            native_ok = bool((caps.get("frame_interpolation") or {}).get("available"))
        except Exception:  # noqa: BLE001
            native_ok = False
        use_software = engine == "Software" or (not native_ok and engine in ("Auto", "Software"))
        if use_software:
            outs, fails = [], []
            for i, src in enumerate(inputs):
                stem = os.path.splitext(os.path.basename(src))[0]
                out = os.path.join(_OUTPUT_DIR, f"{stem}_DLSSFG_sw_{uuid.uuid4().hex[:6]}.mp4")
                os.makedirs(_OUTPUT_DIR, exist_ok=True)

                def rep(v, msg, _i=i, _n=len(inputs)):
                    update(tid, progress=max(0, min(int(v * 100), 99)),
                           message=f"[{_i + 1}/{_n}] {msg}")
                try:
                    outs.append(_software_interp_one(src, target_fps, quality, out, rep))
                except Exception as e:  # noqa: BLE001
                    fails.append({"input": os.path.basename(src), "error": str(e)})
            return {"outputs": _manifest(outs), "failures": fails, "cancelled": False}

        from src.frame_interpolation.batch import interpolate_videos
        from src.frame_interpolation.models import FrameInterpolationOptions
        options = FrameInterpolationOptions(
            ai_gpu_uuid=ai_gpu_uuid,
            target_fps=target_fps, engine=engine,
            codec=codec, container=container, quality=quality,
            hdr_mode=hdr_mode.lower() == "true",
            rename_mode=rename_mode, custom_suffix=custom_suffix,
        )
        res = interpolate_videos(inputs, options, progress=_make_progress(tid))
        files_out = [it.result.output_path for it in res.successes]
        failures = [{"input": os.path.basename(it.input_path), "error": it.error} for it in res.failures]
        return {"outputs": _manifest(files_out), "failures": failures,
                "cancelled": res.cancelled}

    _run(tid, work)
    return {"task_id": tid}


def _make_progress(tid: str):
    def report(v, msg):
        p = max(0, min(int(v * 100), 99))
        update(tid, progress=p, message=str(msg))
    return report


# ===================== 设置 =====================
class EnhanceSettingsBody(BaseModel):
    nr_preset: Optional[str] = None
    nr_style: Optional[str] = None
    nr_intensity: Optional[float] = None
    local_tone_strength: Optional[float] = None
    local_structure_strength: Optional[float] = None
    skin_structure_strength: Optional[float] = None
    upscaling_factor: Optional[float] = None
    automatic_mask: Optional[bool] = None
    codec: Optional[str] = None
    container: Optional[str] = None
    quality: Optional[str] = None
    hdr_mode: Optional[bool] = None
    image_format: Optional[str] = None
    image_quality: Optional[int] = None
    dlss_model_preset: Optional[str] = None
    image_rename_mode: Optional[str] = None
    image_custom_suffix: Optional[str] = None
    video_rename_mode: Optional[str] = None
    video_custom_suffix: Optional[str] = None
    frame_interpolation_target_fps: Optional[str] = None
    frame_interpolation_engine: Optional[str] = None
    frame_interpolation_codec: Optional[str] = None
    frame_interpolation_container: Optional[str] = None
    frame_interpolation_quality: Optional[str] = None
    frame_interpolation_hdr_mode: Optional[bool] = None
    frame_interpolation_rename_mode: Optional[str] = None
    frame_interpolation_custom_suffix: Optional[str] = None
    preview_encoding: Optional[str] = None


@router.get("/settings")
def get_settings():
    try:
        from src.settings.storage import load_settings
        s = load_settings(_CONFIG_PATH)
        return {f.name: getattr(s, f.name) for f in s.__dataclass_fields__.values()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{e}"})


@router.post("/settings")
def save_settings(body: EnhanceSettingsBody):
    try:
        from src.settings.storage import load_settings, save_settings as _save
        from src.settings.models import UISettings, replace
        cur = load_settings(_CONFIG_PATH)
        updates = {k: v for k, v in body.dict(exclude_none=True).items()}
        new_s = replace(cur, **updates)
        _save(_CONFIG_PATH, new_s)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": f"{e}"})


# 后端启动即后台预热 DLSS 运行时，用户进入 DLSS 页时通常已就绪，无需等待加载。
_warmup()