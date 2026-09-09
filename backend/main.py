"""
FastAPI 主应用：提供网页界面与去水印处理接口。
支持：本地上传（视频/图片）/ 多处水印(多框) / 模板 / 批量处理。

说明：原「分享链接（豆包等）」功能已移除。此类平台采用 JS 动态加载 +
签名播放地址 + 登录校验，视频真实地址位于需登录态的接口之后，自动解析在
技术上无法稳定绕过（抓取并保存用户登录态既不安全也违反平台条款）。
因此仅保留「本地文件上传」这一可靠入口。
"""
import os
import re
import sys
import json
import time
import threading
import zipfile
import subprocess
import urllib.request
import mimetypes
from typing import List, Optional

import cv2
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(__file__))

from task_store import new_task, get, update
import video_io
import mask as maskmod
import processor
from watermark_api import router as wm_router
from enhance_api import router as enhance_router, _OUTPUT_DIR as _ENHANCE_OUT_DIR

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WMWORKSPACE = os.path.abspath(os.path.join(ROOT, ".."))
FRONTEND_DIR = os.path.join(ROOT, "frontend")
BASE = os.path.join(ROOT, "data")
for d in ("uploads", "staging", "output", "preview", "jobs", "templates"):
    os.makedirs(os.path.join(BASE, d), exist_ok=True)

TEMPLATES_FILE = os.path.join(BASE, "templates", "templates.json")
TEMPLATES_LOCK = threading.Lock()

app = FastAPI(title="视频去水印工具")
app.include_router(wm_router)
app.include_router(enhance_router)
app.mount("/files", StaticFiles(directory=BASE), name="files")


class _NoCacheStaticFiles(StaticFiles):
    """静态资源禁止缓存：桌面窗口/浏览器可能复用旧 app.js 等，导致前端改动不生效。"""
    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


app.mount("/ui", _NoCacheStaticFiles(directory=FRONTEND_DIR), name="ui")
# /enhanceout 必须无条件挂载：打包 exe 时 dlss/outputs 不随包分发（启动时不存在），
# 若按旧逻辑条件挂载会导致所有增强结果 URL 404（图片破图标 / 视频黑块）。
os.makedirs(_ENHANCE_OUT_DIR, exist_ok=True)
app.mount("/enhanceout", StaticFiles(directory=_ENHANCE_OUT_DIR), name="enhanceout")


# ===================== 支持 Range 的视频流（拖动进度条依赖它） =====================
def _range_file_response(path: str, request: Request, media_type: str = "video/mp4"):
    """返回支持 HTTP Range 的文件响应（206 分段），用于视频实时拖动 seek。

    Starlette 的 FileResponse 不支持 Range 请求（永远返回 200 整个文件），
    导致浏览器无法通过字节范围请求 seek，进度条拖不动。此函数补齐该能力。
    """
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="文件不存在")
    file_size = os.path.getsize(path)
    start, end = 0, file_size - 1
    status_code = 200

    range_header = request.headers.get("range", "")
    m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
    if m and file_size > 0:
        g1, g2 = m.group(1), m.group(2)
        if g1 == "" and g2 != "":          # bytes=-N 末尾 N 字节
            start = max(0, file_size - int(g2))
            end = file_size - 1
        else:
            start = int(g1) if g1 else 0
            end = int(g2) if g2 else file_size - 1
        start = max(0, min(start, file_size - 1))
        end = max(start, min(end, file_size - 1))
        status_code = 206

    length = end - start + 1
    chunk_size = 256 * 1024

    def iterfile():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                data = f.read(min(chunk_size, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    return StreamingResponse(iterfile(), status_code=status_code, media_type=media_type, headers=headers)


@app.get("/video/uploads/{name}")
def video_upload(name: str, request: Request):
    """预览用：上传/解析出的视频，支持 Range 拖动。"""
    safe = os.path.basename(name)
    return _range_file_response(os.path.join(BASE, "uploads", safe), request)


@app.get("/video/output/{name}")
def video_output(name: str, request: Request):
    """结果视频流，支持 Range 拖动。"""
    safe = os.path.basename(name)
    return _range_file_response(os.path.join(BASE, "output", safe), request)


# ===================== 模板 =====================
def _load_templates() -> dict:
    if not os.path.exists(TEMPLATES_FILE):
        return {}
    try:
        with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_templates(data: dict):
    with TEMPLATES_LOCK:
        os.makedirs(os.path.dirname(TEMPLATES_FILE), exist_ok=True)
        with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


@app.get("/api/templates")
def list_templates():
    return {"templates": list(_load_templates().values())}


class TemplateReq(BaseModel):
    name: str
    boxes: list
    engine: str = "auto"
    quality: str = "balanced"
    device: str = "cuda"


@app.post("/api/templates")
def save_template(req: TemplateReq):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="模板名称不能为空")
    if not req.boxes:
        raise HTTPException(status_code=400, detail="模板至少需要一个水印框")
    tpl = {
        "name": name,
        "boxes": req.boxes,
        "engine": req.engine,
        "quality": req.quality,
        "device": req.device,
        "updated": int(time.time()),
    }
    data = _load_templates()
    data[name] = tpl
    _save_templates(data)
    return {"ok": True, "template": tpl}


@app.delete("/api/templates/{name}")
def delete_template(name: str):
    data = _load_templates()
    if name in data:
        del data[name]
        _save_templates(data)
    return {"ok": True}


class TemplateRenameReq(BaseModel):
    old_name: str
    new_name: str


@app.put("/api/templates/rename")
def rename_template(req: TemplateRenameReq):
    old = (req.old_name or "").strip()
    new = (req.new_name or "").strip()
    if not old:
        raise HTTPException(status_code=400, detail="原模板名称不能为空")
    if not new:
        raise HTTPException(status_code=400, detail="新模板名称不能为空")
    data = _load_templates()
    if old not in data:
        raise HTTPException(status_code=404, detail=f"模板「{old}」不存在")
    if new in data and new != old:
        raise HTTPException(status_code=400, detail=f"已存在同名模板「{new}」")
    tpl = data.pop(old)
    tpl["name"] = new
    data[new] = tpl
    # 避免误写成并发修改的深层引用，单独再存一份干净对象
    _save_templates(data)
    return {"ok": True, "template": tpl}


# ===================== 基础接口 =====================
@app.get("/")
def index():
    from fastapi.responses import Response
    from fastapi.staticfiles import StaticFiles
    path = os.path.join(FRONTEND_DIR, "index.html")
    resp = FileResponse(path)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/api/engines")
def engines():
    import inpaint as _inpaint

    ai = _inpaint.lama_available()
    mat = _inpaint.mat_available()
    try:
        import propainter_engine as _pp
        pp_ok = _pp.is_available()
    except Exception:
        pp_ok = False
    return {
        "ai_available": ai,
        "mat_available": mat,
        "mat_model": _inpaint.find_local_mat() if mat else None,
        "pkg": _inpaint._PKG,
        "local_model": _inpaint._LOCAL_MODEL,
        "model_dir": _inpaint.MODEL_DIR,
        "propainter_available": pp_ok,
        "propainter_model": _pp.MODEL_DIR if pp_ok else None,
        "nvenc": video_io.has_nvenc(),
        "cpu_count": os.cpu_count() or 1,
    }


class ProcessReq(BaseModel):
    staging_id: str
    boxes: Optional[list] = None
    box: Optional[dict] = None  # 兼容旧的单框
    engine: str = "auto"
    quality: str = "balanced"
    device: str = "cuda"
    seg_start: Optional[float] = None  # 只处理片段起始秒（含）
    seg_end: Optional[float] = None      # 只处理片段结束秒（不含）


def _resolve_boxes(req: ProcessReq) -> list:
    if req.boxes:
        return req.boxes
    if req.box:
        return [req.box]
    return []


def _stage_video(tid: str, video_path: str) -> dict:
    try:
        info = video_io.probe(video_path)
    except Exception as e:
        update(tid, status="error", message=f"视频读取失败：{e}")
        raise HTTPException(status_code=400, detail=f"视频读取失败：{e}")

    boxes = _detect_video_boxes(video_path)
    preview_url = None
    prev_path = os.path.join(BASE, "preview", f"{tid}.jpg")
    video_io.make_thumbnail(video_path, prev_path)
    if os.path.exists(prev_path):
        preview_url = f"/files/preview/{tid}.jpg"

    return {
        "staging_id": tid,
        "video_url": f"/video/uploads/{os.path.basename(video_path)}",
        "preview": preview_url,
        "boxes": boxes,
        "box": boxes[0] if boxes else None,
        "width": info["width"],
        "height": info["height"],
        "fps": info["fps"],
    }


def _stage_image(tid: str, image_path: str) -> dict:
    img = cv2.imread(image_path)
    if img is None:
        update(tid, status="error", message="图片读取失败")
        raise HTTPException(status_code=400, detail="图片读取失败，请确认格式受支持（jpg/png/webp 等）")
    h, w = img.shape[:2]
    boxes = maskmod.auto_detect_all(img)
    return {
        "staging_id": tid,
        "image_url": f"/files/uploads/{os.path.basename(image_path)}",
        "boxes": boxes,
        "box": boxes[0] if boxes else None,
        "width": w,
        "height": h,
    }


_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    name = (file.filename or "video.mp4").lower()
    is_img = name.endswith(_IMAGE_EXT)
    tid = new_task("staging")
    ext = os.path.splitext(name)[1] or (".png" if is_img else ".mp4")
    dest = os.path.join(BASE, "uploads", f"{tid}{ext}")
    with open(dest, "wb") as f:
        f.write(await file.read())
    if is_img:
        return _stage_image(tid, dest)
    return _stage_video(tid, dest)


@app.post("/api/upload_multi")
async def upload_multi(files: List[UploadFile] = File(...)):
    results = []
    for f in files:
        tid = new_task("staging")
        ext = os.path.splitext(f.filename or "video.mp4")[1] or ".mp4"
        dest = os.path.join(BASE, "uploads", f"{tid}{ext}")
        with open(dest, "wb") as out:
            out.write(await f.read())
        results.append({"staging_id": tid, "name": f.filename, "video_url": f"/files/uploads/{os.path.basename(dest)}"})
    return {"items": results}


class DetectReq(BaseModel):
    staging_id: str
    time: float = 0.0   # 视频：要检测的时间点（秒）


def _sample_video_frames(path: str, t: float = 0.0, max_frames: int = 7) -> list:
    """从视频中抽出若干 BGR 帧，供时域检测使用。

    帧尽量分散在整段视频（片头 / 中段 / 片尾），以便区分“静止的水印”与“运动的内容”：
    - 优先覆盖 t 指定的时间点（若 t>0）；
    - 再在片头、中段、片尾补足其余帧。
    返回 BGR 帧列表（最少 1 帧），读取失败返回空列表。
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return []
    try:
        total = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        if not total or total < 1:
            ret, f0 = cap.read()
            return [f0] if ret else []

        times = []
        if t and t > 0:
            times.append(t)
        # 分布均匀的时间点（秒）
        ts = [total / (max_frames + 1) * (i + 1) / fps for i in range(max_frames)]
        # 与 t 去重后合入
        for x in ts:
            if x >= 0 and len(times) < max_frames:
                times.append(x)
        times = times[:max_frames]

        frames = []
        for mt in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, mt * 1000.0)
            ret, f = cap.read()
            if ret and f is not None:
                frames.append(f)
        if not frames:  # 全部读取失败，退回第 0 帧
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, f = cap.read()
            if ret and f is not None:
                frames.append(f)
        return frames
    finally:
        cap.release()


def _detect_video_boxes(path: str, t: float = 0.0) -> list:
    """对视频做统一的“时域水印检测”（抽多帧分析静止叠加的水印）。

    上传即检测、以及点「自动检测」时都走这里，保证结果一致、更准；
    时域检不出时退化为单帧启发式检测。
    """
    frames = _sample_video_frames(path, t, max_frames=7)
    if len(frames) >= 3:
        boxes = maskmod.auto_detect_video_frames(frames)
        if not boxes:  # 时域没检出，退化到单帧启发式
            boxes = maskmod.auto_detect_all(frames[0])
    else:
        boxes = maskmod.auto_detect_all(frames[0]) if frames else []
    return boxes


@app.post("/api/detect")
def detect(req: DetectReq):
    """实时自动检测水印。

    前端点「自动检测」时调用：对图片或视频的**当前帧**重新跑一次检测，
    视频可指定 time 跳到对应帧，方便对准水印出现的位置。
    返回归一化 boxes 列表。
    """
    uploads = os.path.join(BASE, "uploads")
    cand = [f for f in os.listdir(uploads) if f.startswith(req.staging_id)]
    if not cand:
        raise HTTPException(status_code=404, detail="未找到已上传的文件")
    path = os.path.join(uploads, cand[0])
    name = cand[0].lower()

    try:
        if name.endswith(_IMAGE_EXT):
            frame = cv2.imread(path)
            if frame is None:
                raise HTTPException(status_code=400, detail="图片读取失败")
            boxes = maskmod.auto_detect_all(frame)
        else:
            # 视频：用**时域检测**（更准）——抽多帧分析静止水印。
            boxes = _detect_video_boxes(path, req.time)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"检测失败：{e}")

    if not boxes:   # 兜底：给出一个右下角建议框，避免"点了没反应"
        boxes = [{"x": 0.70, "y": 0.78, "w": 0.25, "h": 0.17}]
    return {"boxes": boxes}


@app.post("/api/process")
def process(req: ProcessReq):
    uploads = os.path.join(BASE, "uploads")
    cand = [f for f in os.listdir(uploads) if f.startswith(req.staging_id)]
    if not cand:
        raise HTTPException(status_code=404, detail="未找到已上传/解析的视频")
    input_path = os.path.join(uploads, cand[0])

    boxes = _resolve_boxes(req)
    task_id = new_task("job")
    t = threading.Thread(
        target=processor.run_job,
        args=(task_id, input_path, boxes, req.engine, req.device, req.quality,
              req.seg_start, req.seg_end),
    )
    t.daemon = True
    t.start()
    return {"task_id": task_id}


# ===================== 图片去水印 =====================
class BatchReq(BaseModel):
    items: List[str]                       # staging_id 列表（来自 /api/upload_multi 或 /api/upload_images）
    names: Optional[List[str]] = None      # 与 items 对齐的原始文件名
    boxes: Optional[list] = None
    engine: str = "auto"
    quality: str = "balanced"
    device: str = "cuda"
    template_name: Optional[str] = None
    per_item: Optional[List[Optional[dict]]] = None  # 与 items 对齐；null=跟随全局；手动 boxes 优先于模板


def _resolve_batch_config(req: BatchReq, idx: int, tpls: dict):
    """逐项解析该素材的 (boxes, engine, quality, device)。

    优先级：手动 boxes > 该项 template_name > 全局 template_name > 全局 boxes。
    """
    cfg = (req.per_item or [])[idx] if req.per_item else None
    if cfg and cfg.get("boxes"):  # 手动框选优先于模板
        return cfg["boxes"], req.engine, req.quality, req.device
    tname = (cfg and cfg.get("template_name")) or req.template_name
    if tname:
        tpl = tpls.get(tname)
        if not tpl:
            raise HTTPException(status_code=404, detail="模板不存在")
        return (tpl.get("boxes"), tpl.get("engine", req.engine),
                tpl.get("quality", req.quality), tpl.get("device", req.device))
    return req.boxes, req.engine, req.quality, req.device


@app.post("/api/upload_images")
async def upload_images(files: List[UploadFile] = File(...)):
    results = []
    for f in files:
        tid = new_task("staging")
        name = (f.filename or "image.png").lower()
        ext = os.path.splitext(name)[1] or ".png"
        dest = os.path.join(BASE, "uploads", f"{tid}{ext}")
        with open(dest, "wb") as out:
            out.write(await f.read())
        results.append({"staging_id": tid, "name": f.filename,
                        "image_url": f"/files/uploads/{os.path.basename(dest)}"})
    return {"items": results}


@app.post("/api/process_image")
def process_image(req: ProcessReq):
    uploads = os.path.join(BASE, "uploads")
    cand = [f for f in os.listdir(uploads) if f.startswith(req.staging_id)]
    if not cand:
        raise HTTPException(status_code=404, detail="未找到已上传的图片")
    input_path = os.path.join(uploads, cand[0])
    boxes = _resolve_boxes(req)
    task_id = new_task("job")
    t = threading.Thread(
        target=processor.process_image,
        args=(task_id, input_path, boxes, req.engine, req.device),
    )
    t.daemon = True
    t.start()
    return {"task_id": task_id}


@app.post("/api/image_batch")
def image_batch(req: BatchReq):
    engine, device = req.engine, req.device
    tpls = _load_templates()
    uploads = os.path.join(BASE, "uploads")
    items = []
    for i, sid in enumerate(req.items):
        cand = [f for f in os.listdir(uploads) if f.startswith(sid)]
        if not cand:
            continue
        input_path = os.path.join(uploads, cand[0])
        task_id = new_task("job")
        name = (req.names[i] if req.names and i < len(req.names) else None) or sid
        items.append({"staging_id": sid, "task_id": task_id,
                      "input_path": input_path, "name": name})
    if not items:
        raise HTTPException(status_code=400, detail="没有可处理的图片")
    batch_id = new_task("batch")
    with BATCH_LOCK:
        BATCHES[batch_id] = {"batch_id": batch_id, "total": len(items),
                             "done": 0, "status": "queued", "items": items}

    def worker():
        for idx, it in enumerate(items):
            with BATCH_LOCK:
                BATCHES[batch_id]["status"] = "processing"
            bcs, eng, _, dev = _resolve_batch_config(req, idx, tpls)
            processor.process_image(it["task_id"], it["input_path"], bcs, eng, dev)
            with BATCH_LOCK:
                BATCHES[batch_id]["done"] += 1
        with BATCH_LOCK:
            BATCHES[batch_id]["status"] = "done"

    t = threading.Thread(target=worker)
    t.daemon = True
    t.start()
    return {"batch_id": batch_id, "total": len(items)}


# ===================== 批量处理 =====================
BATCHES = {}
BATCH_LOCK = threading.Lock()


@app.post("/api/batch")
def batch(req: BatchReq):
    tpls = _load_templates()
    uploads = os.path.join(BASE, "uploads")
    items = []
    for i, sid in enumerate(req.items):
        cand = [f for f in os.listdir(uploads) if f.startswith(sid)]
        if not cand:
            continue
        input_path = os.path.join(uploads, cand[0])
        task_id = new_task("job")
        name = (req.names[i] if req.names and i < len(req.names) else None) or sid
        items.append({"staging_id": sid, "task_id": task_id, "input_path": input_path, "name": name})

    if not items:
        raise HTTPException(status_code=400, detail="没有可处理的视频")

    batch_id = new_task("batch")
    with BATCH_LOCK:
        BATCHES[batch_id] = {"batch_id": batch_id, "total": len(items), "done": 0,
                            "status": "queued", "items": items}

    def worker():
        for idx, it in enumerate(items):
            with BATCH_LOCK:
                BATCHES[batch_id]["status"] = "processing"
            bcs, eng, qual, dev = _resolve_batch_config(req, idx, tpls)
            processor.run_job(it["task_id"], it["input_path"], bcs, eng, dev, qual)
            with BATCH_LOCK:
                BATCHES[batch_id]["done"] += 1
        with BATCH_LOCK:
            BATCHES[batch_id]["status"] = "done"

    t = threading.Thread(target=worker)
    t.daemon = True
    t.start()
    return {"batch_id": batch_id, "total": len(items)}


@app.get("/api/batch/{batch_id}")
def batch_status(batch_id: str):
    with BATCH_LOCK:
        b = BATCHES.get(batch_id)
    if not b:
        raise HTTPException(status_code=404, detail="批量任务不存在")
    tasks = []
    for it in b["items"]:
        s = get(it["task_id"])
        tasks.append({
            "staging_id": it["staging_id"],
            "task_id": it["task_id"],
            "name": it.get("name", it["staging_id"]),
            "status": s.get("status"),
            "progress": s.get("progress", 0),
            "message": s.get("message", ""),
            "download_url": f"/api/download/{it['task_id']}" if s.get("status") == "done" else None,
        })
    return {"batch_id": batch_id, "total": b["total"], "done": b["done"],
            "status": b["status"], "tasks": tasks}


@app.get("/api/batch/{batch_id}/zip")
def batch_zip(batch_id: str):
    with BATCH_LOCK:
        b = BATCHES.get(batch_id)
    if not b:
        raise HTTPException(status_code=404, detail="批量任务不存在")
    out_dir = os.path.join(BASE, "output")
    tmp = os.path.join(BASE, "output", f"batch_{batch_id}.zip")
    done = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for it in b["items"]:
            s = get(it["task_id"])
            out = s.get("output")
            if out and os.path.exists(out):
                stem = os.path.splitext(it.get("name", it["staging_id"]))[0]
                ext = os.path.splitext(out)[1] or ".mp4"
                z.write(out, f"{stem}_去水印{ext}")
                done += 1
    if done == 0:
        raise HTTPException(status_code=404, detail="尚无已完成的文件可打包")
    return FileResponse(tmp, filename=f"watermark_removed_{batch_id}.zip", media_type="application/zip")


# ===================== 状态 / 下载 =====================
@app.get("/api/status/{task_id}")
def status(task_id: str):
    return get(task_id)


@app.get("/api/download/{task_id}")
def download(task_id: str, request: Request):
    t = get(task_id)
    out = t.get("output")
    if not out or not os.path.exists(out):
        raise HTTPException(status_code=404, detail="文件不存在或尚未完成")
    # 按输出文件扩展名推断媒体类型（视频 mp4 / 图片 png 等）；
    # 仍走支持 Range 的响应，视频可拖动进度条，图片预览亦可加载。
    mt = mimetypes.guess_type(out)[0] or "application/octet-stream"
    return _range_file_response(out, request, media_type=mt)


# ===================== DLSS5 视觉增强（已并入后端进程，占位说明） =====================
# DLSS 处理能力已通过 enhance_api（/api/enhance/*）直接并入本后端进程，
# 不再单独启动 Gradio 服务或使用 iframe。此处仅保留响应式状态别名供旧调用兼容。
@app.get("/api/dlss/status")
def dlss_status():
    try:
        from enhance_api import _init_state
        return {"running": True, "integrated": True,
                "ready": _init_state.get("ready", False),
                "initializing": not _init_state.get("done", False)}
    except Exception:  # noqa: BLE001
        return {"running": True, "integrated": True, "ready": False}
