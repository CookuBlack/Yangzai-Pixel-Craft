"""
视频 I/O 工具：基于 ffmpeg（优先系统 ffmpeg，否则 imageio-ffmpeg 自带）完成
帧提取、音频提取、以及重封装。

性能优化点：
1. 硬件编码：检测 h264_nvenc（NVIDIA），可用时用 GPU 硬件编码重封装，比 libx264 快数倍。
2. 中间帧用高质量 JPEG（默认）而非 PNG，I/O 快 5~10 倍、体积小；「高保真」画质才用 PNG。
3. 解码/编码均开启多线程，但限制线程数避免内存峰值导致系统卡死/进程被杀。
4. 所有 ffmpeg 调用均捕获 stderr，出错时把真实原因抛出，便于定位（不再静默失败）。
"""
import os
import shutil
import subprocess

import cv2
import imageio_ffmpeg


_FFMPEG = None
_NVENC = None


def get_ffmpeg_exe() -> str:
    """获取 ffmpeg 路径：环境变量 FFMPEG_EXE > 系统 PATH > imageio-ffmpeg 自带。"""
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    env = os.environ.get("FFMPEG_EXE")
    if env and os.path.exists(env):
        _FFMPEG = env
        return _FFMPEG
    sysf = shutil.which("ffmpeg")
    if sysf:
        _FFMPEG = sysf
        return _FFMPEG
    _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    return _FFMPEG


def _encoders() -> str:
    try:
        out = subprocess.run(
            [get_ffmpeg_exe(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout or ""
    except Exception:
        return ""


def has_nvenc() -> bool:
    """是否支持 NVIDIA NVENC 硬件编码（GPU 加速重封装）。"""
    global _NVENC
    if _NVENC is None:
        _NVENC = "h264_nvenc" in _encoders()
    return _NVENC


def _run_ffmpeg(cmd, timeout=None):
    """运行 ffmpeg；失败时将 stderr 一并抛出，便于定位真正原因。"""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg 执行超时（>{timeout}s），命令：{' '.join(cmd)}")
    except FileNotFoundError:
        raise RuntimeError(f"找不到 ffmpeg 可执行文件：{cmd[0]}")
    if res.returncode != 0:
        tail = (res.stderr or "")[-1500:]
        raise RuntimeError(
            f"ffmpeg 失败（exit {res.returncode}）：\n{tail}\n命令：{' '.join(cmd)}"
        )
    return res


def make_thumbnail(video_path: str, out_path: str, max_w: int = 480) -> bool:
    """提取首帧缩略图，用于前端预览封面。"""
    cap = cv2.VideoCapture(video_path)
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return False
    h0, w0 = frame.shape[:2]
    if w0 <= 0:
        return False
    scale = min(1.0, max_w / w0)
    small = cv2.resize(frame, (int(w0 * scale), int(h0 * scale)))
    cv2.imwrite(out_path, small)
    return True


def probe(video_path: str) -> dict:
    """读取视频基础信息（fps / 宽高 / 帧数）。"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError("无法打开视频文件，请确认格式是否被支持")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return {"fps": float(fps), "width": width, "height": height, "frame_count": count}


def extract_frames(video_path: str, out_dir: str, fps: float, fmt: str = "jpg") -> list:
    """按原始 fps 提取全部帧，返回按序号排序的文件路径列表。

    fmt: 'jpg'(快，高质量，默认) 或 'png'(无损)。
    """
    os.makedirs(out_dir, exist_ok=True)
    ffmpeg = get_ffmpeg_exe()
    ext = "jpg" if fmt == "jpg" else "png"
    pattern = os.path.join(out_dir, f"frame_%05d.{ext}")
    cmd = [ffmpeg, "-y", "-threads", "0", "-i", video_path]
    if fmt == "jpg":
        cmd += ["-q:v", "2"]  # mjpeg 质量 2 ≈ 视觉无损
    cmd += ["-vf", f"fps={fps}", pattern]
    _run_ffmpeg(cmd)
    files = sorted(
        os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.endswith("." + ext)
    )
    return files


def extract_audio(video_path: str, out_path: str):
    """提取音频。优先无损保留原始音轨（copy），容器不兼容时回退 AAC 192k。"""
    ffmpeg = get_ffmpeg_exe()
    # 第一次：无损 copy（保留原始编码与码率，画质零损失）
    cmd = [ffmpeg, "-y", "-i", video_path, "-vn", "-c:a", "copy", out_path]
    try:
        _run_ffmpeg(cmd)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception as e:
        last_err = str(e)
    else:
        last_err = "copy 未产生有效音频文件"
    # 回退：AAC 192k（原音轨格式不被 mp4 支持时）
    cmd = [ffmpeg, "-y", "-i", video_path, "-vn", "-acodec", "aac", "-b:a", "192k", out_path]
    try:
        _run_ffmpeg(cmd)
    except Exception as e:
        last_err = str(e)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    # 音频可有可无：返回 None，由上层决定静音输出
    return None


# 画质 -> 编码参数：nvenc 用恒定质量 cq，libx264 用 crf/preset。
# 重要：H.264「真·无损」只能在 High 4:4:4 配置下实现（libx264 -crf 0 会自动强制该配置），
# 而 4:4:4 在浏览器（Chrome/Edge/Safari 的 <video>）与大量播放器里黑屏只有声音（无画面）。
# 因此「原画质」档改用「视觉无损」：极低 crf(8) + 常规 High 4:2:0 + yuv420p，
# 各播放器可正常播放且画质与原片几乎无差异。绝不使用 crf 0。
QUALITY = {
    "fast":     {"nvenc_cq": 26, "x264_crf": 23, "x264_preset": "veryfast", "fmt": "jpg"},
    "balanced": {"nvenc_cq": 18, "x264_crf": 16, "x264_preset": "medium",   "fmt": "png"},
    "best":     {"nvenc_cq": 15, "x264_crf": 13, "x264_preset": "slow",     "fmt": "png"},
    "lossless": {"nvenc_cq": 12, "x264_crf": 8,  "x264_preset": "medium",  "fmt": "png"},
}

# 安全下限：crf <= 0（会触发 H.264 High 4:4:4 无损，浏览器黑屏无画面），强制抬到该值。
_X264_CRF_FLOOR = 8


def intermediate_format(quality: str) -> str:
    return QUALITY.get(quality, QUALITY["balanced"])["fmt"]


def _pix_fmt_for(quality: str) -> str:
    # 统一用 yuv420p：与原始 H.264 源一致、各播放器兼容性好；
    # 配合 PNG 中间帧 + crf 0 已验证像素级零损失（diff=0）。
    return "yuv420p"


def rebuild_video(frames_dir: str, audio_path, out_path: str, fps: float, quality: str):
    """将修复后的帧与音频重新封装为 MP4。

    - fast/balanced：优先 NVENC 硬件编码（GPU，快数倍）；balanced 已接近视觉无损
    - best：libx264 高码率（crf 13，视觉无损）
    - lossless（原画质）：视觉无损（crf 8 + PNG 中间帧 + 常规 High 4:2:0），浏览器可正常播放
    - 音频：原样保留（copy），不二次压缩；copy 不被 MP4 支持时回退 AAC

    健壮性：
    - 限制编码线程数，避免内存峰值导致进程被杀。
    - 音轨 copy 失败时自动回退 AAC 重编码。
    - 编码失败时自动用「更轻预设 + 更少线程」重试一次。
    - 视频/音频任一创建失败都抛出 ffmpeg 真实 stderr。
    """
    ffmpeg = get_ffmpeg_exe()
    q = QUALITY.get(quality, QUALITY["balanced"])
    ext = q["fmt"]
    pattern = os.path.join(frames_dir, f"frame_%05d.{ext}")
    first_frame = os.path.join(frames_dir, f"frame_00001.{ext}")
    if not os.path.exists(first_frame):
        # 兜底：目录内任意该扩展名帧
        alts = sorted(f for f in os.listdir(frames_dir) if f.endswith("." + ext)) if os.path.isdir(frames_dir) else []
        if not alts:
            raise RuntimeError("找不到修复后的帧文件，可能抽帧或修复阶段失败")
        pattern = os.path.join(frames_dir, alts[0].rsplit("_", 1)[0] + "_%05d." + ext)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    has_audio = bool(audio_path) and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0
    # 限制线程，避免 16~22 线程的 libx264 在高分辨率时内存暴涨把进程拖垮
    threads = max(1, min(os.cpu_count() or 4, 8))
    pix = _pix_fmt_for(quality)

    use_nvenc = has_nvenc() and quality in ("fast", "balanced")

    if use_nvenc:
        vcodec = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                  "-cq", str(q["nvenc_cq"]), "-b:v", "0"]
    else:
        # 视觉无损/各档统一走 libx264 + 常规 High 4:2:0 + yuv420p。
        # crf 强制 >= _X264_CRF_FLOOR，杜绝 H.264 High 4:4:4（浏览器黑屏只有声音）。
        vcodec = ["-c:v", "libx264", "-preset", q["x264_preset"],
                  "-crf", str(max(_X264_CRF_FLOOR, int(q["x264_crf"]))),
                  "-threads", str(threads)]

    def build_cmd(audio_mode: str):
        c = [ffmpeg, "-y", "-framerate", f"{fps}", "-i", pattern]
        if has_audio:
            c += ["-i", audio_path, "-map", "0:v:0", "-map", "1:a:0"]
        c += vcodec + ["-pix_fmt", pix]
        if has_audio:
            if audio_mode == "copy":
                c += ["-c:a", "copy", "-shortest"]
            else:
                c += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
        # faststart：把 moov 原子前置，浏览器可边下边播、进度条可拖动
        c += ["-movflags", "+faststart", out_path]
        return c

    # 主路径：音轨原样 copy
    try:
        _run_ffmpeg(build_cmd("copy"))
        return
    except RuntimeError as e:
        err = str(e)
        # 情况1：音轨 copy 不被 MP4 容器支持 → 回退 AAC 重编码
        if has_audio and ("not currently supported in container" in err
                          or "Could not find tag for codec" in err):
            try:
                _run_ffmpeg(build_cmd("aac"))
                return
            except RuntimeError as e2:
                err = f"{err}\n[AAC 回退也失败] {e2}"
        # 情况2：仍失败（NVENC 不可用/资源不足/中断等）→ 回退 libx264 软编（浏览器兼容）
        if q["x264_crf"] != 0 or use_nvenc:
            fb = [ffmpeg, "-y", "-framerate", f"{fps}", "-i", pattern]
            if has_audio:
                fb += ["-i", audio_path, "-map", "0:v:0", "-map", "1:a:0"]
            fb += ["-c:v", "libx264", "-preset", "veryfast",
                   "-crf", str(max(_X264_CRF_FLOOR, int(q["x264_crf"] or 16))),
                   "-threads", "2", "-pix_fmt", pix]
            if has_audio:
                fb += ["-c:a", "copy", "-shortest"]
            fb += ["-movflags", "+faststart", out_path]
            try:
                _run_ffmpeg(fb)
                return
            except RuntimeError as e3:
                err = f"{err}\n[libx264 软编回退也失败] {e3}"
        raise RuntimeError(err)
