"""
深度学习文本检测（水印检测主增强）。

基于 PP-OCRv4 的 DBnet 检测头（onnx 格式，经 onnxruntime CPU 推理），
专门定位画面中的中英文/任意位置文字区域——这是水印最常见的形态。

设计要点：
  - onnxruntime 惰性加载（单例），导入本模块不触发任何依赖；
  - 任何异常（模型缺失 / onnxruntime 未装 / 推理失败）都优雅返回 []，
    由调用方回退到原有启发式管道，绝不破坏既有检测；
  - 返回的框为**输入帧同一坐标系**下的轴对齐像素框 (x, y, w, h)。

依赖：onnxruntime（可手动安装，见 requirements.txt）。
"""
import os
import math

import cv2
import numpy as np

# 与输入帧坐标系对应，检测结果直接可用于 mask.py 的候选空间
_DETECT_LIMIT_SIDE = 960     # 检测时最长边（足够定位文字，速度快）
_PROB_THRESH = 0.3           # 概率图二值化阈值（DB postprocess det_db_thresh）
_BOX_THRESH = 0.5            # 框内平均概率阈值（det_db_box_thresh）
_UNCLIP_RATIO = 1.6          # 文本框外扩比例
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)

_PROJECT_ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MODEL_PATH = os.path.join(_PROJECT_ROOT, "model", "det", "ch_PP-OCRv4_det_infer.onnx")

_session = None
_session_failed = False
_input_name = None


def _get_session():
    """惰性加载 onnx 会话；失败（未装 onnxruntime / 模型缺失 / 加载错误）则返回 None。"""
    global _session, _session_failed, _input_name
    if _session is not None or _session_failed:
        return _session
    try:
        import onnxruntime as ort
        if not os.path.exists(_MODEL_PATH):
            _session_failed = True
            return None
        providers = ["CPUExecutionProvider"]
        # 若用户恰好装了 onnxruntime-gpu 且 CUDA 可用，则优先 GPU（纯加速，不影响结果）
        try:
            import onnxruntime as ort_check
            if ort_check.get_available_providers() and "CUDAExecutionProvider" in ort_check.get_available_providers():
                providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        except Exception:
            pass
        sess = ort.InferenceSession(_MODEL_PATH, providers=providers)
        _input_name = sess.get_inputs()[0].name
        _session = sess
    except Exception:
        _session = None
        _session_failed = True
    return _session


def _preprocess(bgr: np.ndarray):
    """按 PP-OCR 的 DetResizeForTest 方式处理：等比缩放 + 对齐 32 + 归一化。"""
    h, w = bgr.shape[:2]
    ratio = _DETECT_LIMIT_SIDE * 1.0 / max(h, w)
    new_h = int(math.ceil(h * ratio / 32.0)) * 32
    new_w = int(math.ceil(w * ratio / 32.0)) * 32
    img = cv2.resize(bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    for c in range(3):
        rgb[:, :, c] = (rgb[:, :, c] - _MEAN[c]) / _STD[c]
    blob = np.transpose(rgb, (2, 0, 1))[np.newaxis, ...].astype(np.float32)
    return blob


def _boxes_from_bitmap(prob: np.ndarray, box_thresh: float, unclip_ratio: float):
    """DB postprocess 的稳健简化版：从概率图里提取轴对齐文字框。

    返回 (x, y, w, h) 列表，坐标位于 prob 图自身像素空间。
    """
    bitmap = (prob > _PROB_THRESH).astype(np.uint8) * 255
    # 轻微闭运算，缝合离散字符断点
    k = np.ones((3, 3), np.uint8)
    bitmap = cv2.morphologyEx(bitmap, cv2.MORPH_CLOSE, k)

    cnts, _ = cv2.findContours(bitmap, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in cnts:
        area = cv2.contourArea(c)
        if area < 20:
            continue
        rect = cv2.minAreaRect(c)
        (cx, cy), (bw, bh), angle = rect
        if bw < 1 or bh < 1:
            continue

        # 框内平均概率校验（在**原始矩形**上算，而不是外扩后的框，
        # 否则背景被混入会拉低均分，误杀真实文字框）。
        mask = np.zeros(bitmap.shape, np.uint8)
        cv2.fillPoly(mask, [cv2.boxPoints(rect).astype(np.int64)], 255)
        mean_prob = float(prob[mask > 0].mean()) if np.count_nonzero(mask) else 0.0
        if mean_prob < box_thresh:
            continue

        # unclip：绕中心按比例外扩，得到最终文本框
        bw2 = bw * unclip_ratio
        bh2 = bh * unclip_ratio
        pts = cv2.boxPoints(((cx, cy), (bw2, bh2), angle))

        x0, y0 = pts[:, 0].min(), pts[:, 1].min()
        x1, y1 = pts[:, 0].max(), pts[:, 1].max()
        bw2i, bh2i = x1 - x0, y1 - y0
        if bw2i < 3 or bh2i < 3:
            continue

        # 排除占满整帧的误检
        bh_px, bw_px = prob.shape[:2]
        if bw2i > bw_px * 0.95 and bh2i > bh_px * 0.95:
            continue

        boxes.append((int(round(x0)), int(round(y0)), int(round(bw2i)), int(round(bh2i))))
    return boxes


def _union_overlap(boxes, iou_thresh=0.25):
    """合并高重叠框（同一水印文字常被切成多条），返回合并后的框。"""
    if not boxes:
        return []

    def iou(a, b):
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        iw = min(ax + aw, bx + bw) - max(ax, bx)
        ih = min(ay + ah, by + bh) - max(ay, by)
        if iw <= 0 or ih <= 0:
            return 0.0
        ia = iw * ih
        ua = aw * ah + bw * bh - ia
        return ia / ua if ua > 0 else 0.0

    merged = []
    remaining = [list(b) for b in boxes]
    while remaining:
        cur = remaining.pop(0)
        x0, y0, x1, y1 = cur[0], cur[1], cur[0] + cur[2], cur[1] + cur[3]
        again = True
        while again:
            again = False
            rest = []
            for o in remaining:
                if iou(cur, o) >= iou_thresh:
                    ox0, oy0 = o[0], o[1]
                    ox1, oy1 = o[0] + o[2], o[1] + o[3]
                    x0, y0 = min(x0, ox0), min(y0, oy0)
                    x1, y1 = max(x1, ox1), max(y1, oy1)
                    again = True
                else:
                    rest.append(o)
            remaining = rest
        merged.append((x0, y0, max(3, x1 - x0), max(3, y1 - y0)))
    return merged


def detect_text_boxes(bgr: np.ndarray):
    """对一帧 BGR 图像做文字检测，返回与输入同坐标系的 (x, y, w, h) 列表。

    任何异常都返回空列表（由调用方兜底），不会抛错。
    """
    sess = _get_session()
    if sess is None:
        return []
    try:
        blob = _preprocess(bgr)
        ph, pw = blob.shape[2], blob.shape[3]
        out = sess.run(None, {_input_name: blob})[0]
        if out.ndim == 4:
            prob = out[0, 0]
        elif out.ndim == 3:
            prob = out[0]
        else:
            prob = out
        prob = np.asarray(prob, dtype=np.float32)

        # 概率图分辨率与缩放后输入可能不一致（模型下采样），按实际比例映射回原帧坐标
        o_h, o_w = prob.shape[:2]
        orig_h, orig_w = bgr.shape[:2]
        dx = orig_w / o_w
        dy = orig_h / o_h

        boxes = _boxes_from_bitmap(prob, _BOX_THRESH, _UNCLIP_RATIO)
        boxes = _union_overlap(boxes)

        scaled = []
        for x, y, w, h in boxes:
            x0 = int(round(x * dx))
            y0 = int(round(y * dy))
            x1 = int(round((x + w) * dx))
            y1 = int(round((y + h) * dy))
            x0 = max(0, min(x0, orig_w - 1))
            y0 = max(0, min(y0, orig_h - 1))
            x1 = max(x0 + 2, min(x1, orig_w))
            y1 = max(y0 + 2, min(y1, orig_h))
            scaled.append((x0, y0, x1 - x0, y1 - y0))
        return scaled
    except Exception:
        return []