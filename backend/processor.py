"""
任务编排：读取视频 -> 提取帧/音频 -> 生成遮罩 -> 逐帧修复 -> 高质量重封装。

性能优化：
1. 按水印框的最小外接矩形 + 边距裁剪出局部区域，只对该局部做修复再贴回，
   大幅缩小处理面积（对 AI 与 OpenCV 都显著加速）。
2. OpenCV 模式用多线程并行逐帧（cv2 释放 GIL，接近线性加速）。
3. AI 模式走 GPU（LaMa 单帧局部修复），配合硬件编码重封装。
4. 中间帧默认高质量 JPEG，I/O 更快。
"""
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import cv2
import numpy as np

import video_io
import mask as maskmod
import inpaint
import task_store


def _workers_for(engine: str) -> int:
    """OpenCV 模式并行线程数；AI 模式 GPU 串行，用 1。"""
    if engine == "opencv":
        return min(16, max(1, os.cpu_count() or 4))
    return 1


def _run_propainter(task_id, frames_dir, frame_files, m, crop, w, h, device, fps, audio, out_path, quality):
    """ProPainter 视频级修复：对水印局部裁剪区域做整段时间传播修复。

    关键：
    - 只处理水印区域裁剪序列（远小于 320x240，4GB 显存 fp16 可流畅跑）；
    - 裁剪区域 8 对齐（ProPainter 要求）；
    - 按 500 帧分段 + 20 帧重叠，兼顾内存与时间一致性。
    """
    import propainter_engine as pe
    if not pe.is_available():
        raise RuntimeError(
            "ProPainter 不可用：请确认 third_party/ProPainter 源码存在，"
            "且 model/propainter/ 下有三个权重文件（ProPainter.pth / "
            "recurrent_flow_completion.pth / raft-things.pth）。"
        )
    x0, y0, x1, y1, _ = crop
    # 8 对齐
    x0 -= x0 % 8
    y0 -= y0 % 8
    x1 = x0 + ((x1 - x0) // 8) * 8
    y1 = y0 + ((y1 - y0) // 8) * 8
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise RuntimeError("水印区域过小，无法使用 ProPainter，请改用 AI(LaMa) 或快速(OpenCV)")
    crop_mask = m[y0:y1, x0:x1]
    if crop_mask.max() == 0:
        raise RuntimeError("水印遮罩为空")

    total = len(frame_files)
    CLIP, OV = 500, 20
    segs = []
    s = 0
    while s < total:
        segs.append((s, min(total, s + CLIP)))
        if s + CLIP >= total:
            break
        s += CLIP - OV

    for si, (s0, e0) in enumerate(segs):
        seg = [cv2.imread(fp) for fp in frame_files[s0:e0]]
        seg_crop = [f[y0:y1, x0:x1] for f in seg]

        def _cb(p, _si=si, _ns=len(segs)):
            gp = 20 + int(70 * (_si + max(0.0, p) / 100.0) / _ns)
            task_store.update(task_id, progress=min(90, gp),
                              message=f"ProPainter 视频级修复（第 {_si + 1}/{_ns} 段）…")

        out_crop = pe.inpaint_clip(seg_crop, crop_mask, device=device, progress_cb=_cb)
        # 进度更新降频：每 4 帧更新一次，减少任务锁开销（提升整体吞吐）
        for i, fp in enumerate(frame_files[s0:e0]):
            frame = seg[i]
            frame[y0:y1, x0:x1] = out_crop[i]
            cv2.imwrite(fp, frame)
            if (s0 + i) % 4 == 0 or s0 + i == total - 1:
                task_store.update(task_id, progress=20 + int(70 * (s0 + i + 1) / total))
        del seg, seg_crop, out_crop

    task_store.update(task_id, progress=92, message="重新封装视频（硬件编码加速）…")
    video_io.rebuild_video(frames_dir, audio, out_path, fps, quality)


def _crop_region(mask: np.ndarray, w: int, h: int):
    """计算水印遮罩的最小外接矩形 + 边距，返回 (x0,y0,x1,y1, crop_mask) 或 None。"""
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return None
    margin = max(48, int(0.08 * max(w, h)))
    x0 = max(0, int(xs.min()) - margin)
    x1 = min(w, int(xs.max()) + 1 + margin)
    y0 = max(0, int(ys.min()) - margin)
    y1 = min(h, int(ys.max()) + 1 + margin)
    return (x0, y0, x1, y1, mask[y0:y1, x0:x1])


def run_job(task_id: str, input_path: str, boxes, engine: str, device: str, quality: str,
            seg_start: Optional[float] = None, seg_end: Optional[float] = None):
    jobdir = None
    try:
        boxes = boxes or []
        if isinstance(boxes, dict):
            boxes = [boxes]
        base = os.path.abspath(os.path.join(os.path.dirname(input_path), ".."))
        jobdir = os.path.join(base, "jobs", task_id)
        frames_dir = os.path.join(jobdir, "frames")
        audio_path = os.path.join(jobdir, "audio.m4a")
        out_dir = os.path.join(base, "output")
        os.makedirs(frames_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{task_id}.mp4")

        task_store.update(task_id, status="processing", progress=5, message="读取视频信息…")
        info = video_io.probe(input_path)
        fps = info["fps"]
        w = info["width"]
        h = info["height"]

        fmt = video_io.intermediate_format(quality)
        task_store.update(task_id, progress=10, message="提取视频帧与音频…")
        frame_files = video_io.extract_frames(input_path, frames_dir, fps, fmt=fmt)
        total = len(frame_files)
        if total == 0:
            raise RuntimeError("未能从视频中提取到帧，可能格式不受支持")
        audio = video_io.extract_audio(input_path, audio_path)

        # 片段：只处理 [seg_start, seg_end) 秒内的帧，其余帧保持原样参与重封装
        if seg_start is not None and seg_end is not None:
            st = max(0, int(seg_start * fps))
            en = min(total, max(st + 1, int(seg_end * fps + 0.5)))
            targets = frame_files[st:en]
        else:
            targets = frame_files
        seg_total = len(targets) or total

        if engine == "propainter" and seg_start is not None:
            raise RuntimeError("「片段去除」暂不支持 ProPainter 引擎，请改用「AI」或「快速」引擎")

        task_store.update(task_id, progress=15, message="生成水印遮罩…")
        if engine == "ai" and not inpaint.lama_available():
            raise RuntimeError(
                "尚未安装 AI 引擎。请在你的环境中安装 iopaint（或旧版 lama-cleaner）："
                "pip install iopaint torch torchvision --index-url https://download.pytorch.org/whl/cu121；"
                "并将 big-lama.pt 放入项目 model/ 目录（或在「引擎」中选择「快速（OpenCV）」）。"
            )
        if engine == "propainter":
            import propainter_engine as pe
            if not pe.is_available():
                raise RuntimeError(
                    "ProPainter 不可用：请确认 third_party/ProPainter 源码存在，"
                    "且 model/propainter/ 下有三个权重文件（ProPainter.pth / "
                    "recurrent_flow_completion.pth / raft-things.pth）。"
                )
        if not boxes:
            first = cv2.imread(frame_files[0])
            boxes = maskmod.auto_detect_all(first) or [maskmod.auto_detect(first)]
        m = maskmod.boxes_to_mask(w, h, boxes)
        crop = _crop_region(m, w, h)

        if engine == "propainter":
            _run_propainter(task_id, frames_dir, frame_files, m, crop, w, h,
                            device, fps, audio, out_path, quality)
            task_store.update(task_id, status="done", progress=100, message="处理完成", output=out_path)
            return

        if crop:
            x0, y0, x1, y1, crop_mask = crop
        else:
            crop_mask = m

        jpg_params = [cv2.IMWRITE_JPEG_QUALITY, 95] if fmt == "jpg" else []

        def process_one(fpath: str):
            frame = cv2.imread(fpath)
            if frame is None:
                return
            if crop:
                sub = frame[y0:y1, x0:x1]
                out = inpaint.inpaint_frame(sub, crop_mask, engine=engine, device=device)
                frame[y0:y1, x0:x1] = out
            else:
                frame = inpaint.inpaint_frame(frame, crop_mask, engine=engine, device=device)
            if jpg_params:
                cv2.imwrite(fpath, frame, jpg_params)
            else:
                cv2.imwrite(fpath, frame)

        workers = _workers_for(engine)
        mode_desc = f"并行 {workers} 线程" if workers > 1 else ("GPU" if engine == "ai" else "串行")
        seg_desc = f"，仅片段 {st / fps:.1f}-{en / fps:.1f} 秒" if targets is not frame_files else ""
        task_store.update(task_id, progress=20,
                          message=f"逐帧去除水印（{mode_desc}{seg_desc}，共 {len(targets)} 帧）…")

        done = 0
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(process_one, fp) for fp in targets]
                for _ in as_completed(futs):
                    done += 1
                    task_store.update(task_id, progress=20 + int(70 * done / seg_total))
        else:
            for fp in targets:
                process_one(fp)
                done += 1
                task_store.update(task_id, progress=20 + int(70 * done / seg_total))

        task_store.update(task_id, progress=92, message="重新封装视频（硬件编码加速）…")
        video_io.rebuild_video(frames_dir, audio, out_path, fps, quality)

        task_store.update(task_id, status="done", progress=100, message="处理完成", output=out_path)
    except Exception as e:
        task_store.update(task_id, status="error", message=f"处理失败：{e}")
    finally:
        # 抽帧 PNG/音频可能达数 GB，无论成功失败都清理任务临时目录（产物在 output/）
        if jobdir:
            shutil.rmtree(jobdir, ignore_errors=True)


def process_image(task_id: str, input_path: str, boxes, engine: str, device: str):
    """单张图片去水印：读取 -> 遮罩 -> (裁剪局部) 修复 -> 保存 PNG。

    复用与视频相同的 LaMa/OpenCV 修复与多框遮罩、局部裁剪加速；
    图片只处理一帧，速度远快于视频。输出无损 PNG 保证画质。
    """
    try:
        boxes = boxes or []
        if isinstance(boxes, dict):
            boxes = [boxes]
        base = os.path.abspath(os.path.join(os.path.dirname(input_path), ".."))
        out_dir = os.path.join(base, "output")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{task_id}.png")

        task_store.update(task_id, status="processing", progress=5, message="读取图片…")
        if engine == "propainter":
            raise RuntimeError("ProPainter 是视频级模型，图片请使用「AI（LaMa）」或「快速（OpenCV）」")
        img = cv2.imread(input_path)
        if img is None:
            raise RuntimeError("无法读取图片，请确认格式受支持（jpg / png / webp 等）")
        h, w = img.shape[:2]

        task_store.update(task_id, progress=20, message="生成水印遮罩…")
        if engine == "ai" and not inpaint.lama_available():
            raise RuntimeError(
                "尚未安装 AI 引擎。请在你的环境中安装 iopaint（或旧版 lama-cleaner）："
                "pip install iopaint torch torchvision --index-url https://download.pytorch.org/whl/cu121；"
                "并将 big-lama.pt 放入项目 model/ 目录（或在「引擎」中选择「快速（OpenCV）」）。"
            )
        if not boxes:
            boxes = maskmod.auto_detect_all(img) or [maskmod.auto_detect(img)]
        m = maskmod.boxes_to_mask(w, h, boxes)
        crop = _crop_region(m, w, h)
        if crop:
            x0, y0, x1, y1, crop_mask = crop
        else:
            crop_mask = m

        task_store.update(task_id, progress=45, message="去除水印（AI 修复中）…")
        if crop:
            # 只修复水印局部区域再贴回，面积小、速度快、无感
            sub = img[y0:y1, x0:x1]
            out = inpaint.inpaint_frame(sub, crop_mask, engine=engine, device=device)
            img[y0:y1, x0:x1] = out
        else:
            img = inpaint.inpaint_frame(img, crop_mask, engine=engine, device=device)

        task_store.update(task_id, progress=92, message="保存结果…")
        cv2.imwrite(out_path, img)
        task_store.update(task_id, status="done", progress=100, message="处理完成", output=out_path)
    except Exception as e:
        task_store.update(task_id, status="error", message=f"处理失败：{e}")
