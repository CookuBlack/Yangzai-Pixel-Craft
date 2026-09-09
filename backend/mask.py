"""
水印区域检测与遮罩生成。
box 采用归一化坐标 {x, y, w, h}（相对整帧尺寸，范围 0~1），
便于前端按比例绘制、后端按原分辨率生成遮罩。
支持多个水印框（boxes 列表），遮罩为各框的并集。
"""
import cv2
import numpy as np

import det_onnx  # 深度学习文本检测（水印主增强）；其内部惰性加载 onnxruntime


def _overlaps_any(box: tuple, refs: list, thr: float = 0.4) -> bool:
    """判断 box 是否与 refs 中任一框显著重叠（面积占比 ≥ thr）。

    用于把深度学习检出的文字框在排序时加权，避免被弱启发式框压下去。
    """
    bx, by, bw, bh = box
    bar = max(1, bw * bh)
    for rx, ry, rw, rh in refs:
        iw = min(bx + bw, rx + rw) - max(bx, rx)
        ih = min(by + bh, ry + rh) - max(by, ry)
        if iw > 0 and ih > 0 and (iw * ih) / bar >= thr:
            return True
    return False


def _overlap_ratio(a: tuple, b: tuple) -> float:
    """两个框的交并比（IoU），用于 NMS / 去重。"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    iw = min(ax + aw, bx + bw) - max(ax, bx)
    ih = min(ay + ah, by + bh) - max(ay, by)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _nms_boxes(boxes: list, thr: float = 0.5) -> list:
    """对已按分数从高到低排好的框做 NMS：保留高分框，抑制与之重叠的低分框。"""
    keep = []
    for b in boxes:
        if all(_overlap_ratio(b, k) < thr for k in keep):
            keep.append(b)
    return keep


def _merge_baseline_boxes(boxes: list) -> list:
    """把处于同一水平基线的相邻框合并成一条（一行文字常被切成多个块）。

    标准：垂直方向有显著重叠（同一行）+ 水平间隙不超过行高一倍 → 合并。
    这样一条水印文字只会给出一个框，避免“多余框选”；而相距很远的两处
    水印（间隙远大于行高）不会被合到一起。
    """
    if not boxes:
        return []
    boxes = [list(b) for b in boxes]
    merged = True
    while merged:
        merged = False
        out = []
        used = set()
        for i in range(len(boxes)):
            if i in used:
                continue
            cur = boxes[i]
            cx0, cy0, cy1 = cur[0], cur[1], cur[1] + cur[3]
            cx1, ch = cur[0] + cur[2], cur[3]
            for j in range(len(boxes)):
                if j == i or j in used:
                    continue
                o = boxes[j]
                ox1, oh = o[0] + o[2], o[3]
                ov = min(cy1, o[1] + o[3]) - max(cy0, o[1])
                if ov > 0.4 * max(ch, oh):
                    # 水平间隙 = 两框最近边缘的距离
                    gap = max(0, max(cx0, o[0]) - min(cx1, ox1))
                    if gap <= 0 or gap < 0.6 * max(ch, oh):
                        # 合并
                        n0 = min(cx0, o[0])
                        n1 = max(cx1, ox1)
                        n2 = min(cy0, o[1])
                        n3 = max(cy1, o[1] + o[3])
                        cur = [n0, n2, n1 - n0, n3 - n2]
                        cy0, cy1 = cur[1], cur[1] + cur[3]
                        cx0, cx1, ch = cur[0], cur[0] + cur[2], cur[3]
                        used.add(j)
                        merged = True
            out.append(cur)
        boxes = out
    return boxes


def _edge_prior(bx: int, by: int, bw: int, bh: int, w: int, h: int) -> float:
    """贴近边缘/角落的框加分先验。

    水印几乎都叠加在画面边缘或四角；而画面里的场景文字（字幕、招牌等）多在
    画面中央。用"框离最近边缘的归一化距离"给边缘文字更高置信度，让角落的
    真水印排在中央的场景文字前面。中央水印即便得分 0，也仍是唯一候选，不受影响。
    """
    d = min(bx, by, w - (bx + bw), h - (by + bh)) / max(1, max(w, h))
    return max(0.0, 1.0 - d * 6.0)


def _rank_boxes(heuristic_px: list, dl_px: list, gray: np.ndarray,
                grad: np.ndarray, saliency: np.ndarray, w: int, h: int,
                max_boxes: int) -> list:
    """排名候选框：含文字则以 DL 文字框为准，无文字才退回启发式。

    水印绝大多数是"文字/带字台标"，DL 能稳定、精确地把**多个**文字水印都找全；
    因此只要 DL 检出了文字，就只用这些文字框（按贴边程度排序），不掺入启发式，
    避免 "很多无用的检测框"。只有当画面连一个文字都检不到（纯图形 Logo）时，
    才退回"贴边"的启发式候选（允许多个角落 Logo），且整幅/超大误框被剔除。
    """
    def base(b, boost):
        return _score_box(b[0], b[1], b[2], b[3], gray, grad, saliency, dl_boost=boost)

    dl = [(bx, by, bw, bh) for (bx, by, bw, bh) in dl_px if base((bx, by, bw, bh), 1.0) > 0.0]
    if dl:
        dl.sort(key=lambda b: base(b, 1.0) + _edge_prior(*b, w, h), reverse=True)
        out = _merge_baseline_boxes(dl)
        out = _nms_boxes(out[:max_boxes * 3])
        return out[:max_boxes]

    # 无文字 → 纯 Logo 场景：仅保留贴边强候选（允许多个），丢弃整幅误框
    hx = []
    for bx, by, bw, bh in heuristic_px:
        if base((bx, by, bw, bh), 0.0) <= 0.0:
            continue
        if _edge_prior(bx, by, bw, bh, w, h) <= 0.0:
            continue
        hx.append((bx, by, bw, bh))
    hx.sort(key=lambda b: base(b, 0.0) + _edge_prior(*b, w, h), reverse=True)
    hx = _merge_baseline_boxes(hx)
    hx = _nms_boxes(hx[:max_boxes * 3])
    return hx[:max_boxes]


def _stable_dl_boxes(per_frame: list, min_frames: int = 2):
    """把多帧 DL 文字框聚成「稳定出现」的框集合（水印在每帧都固定出现）。

    per_frame: 每帧返回的 [(x,y,w,h)] 列表（均已映射到同一工作坐标系）。
    返回稳定框列表；这些框用于在时域检测里赋予 DL 加权。
    """
    clusters = []
    for boxes in per_frame:
        for box in boxes:
            hit = None
            for c in clusters:
                bx, by, bw, bh = box
                cx, cy, cw, ch, cnt = c
                iw = min(bx + bw, cx + cw) - max(bx, cx)
                ih = min(by + bh, cy + ch) - max(by, cy)
                if iw > 0 and ih > 0:
                    ua = bw * bh + cw * ch - iw * ih
                    iou = (iw * ih) / ua if ua > 0 else 0.0
                    if iou >= 0.4:
                        hit = c
                        break
            if hit is None:
                clusters.append([box[0], box[1], box[2], box[3], 1])
            else:
                hit[0] = min(hit[0], box[0])
                hit[1] = min(hit[1], box[1])
                hit[2] = max(hit[2], box[0] + box[2]) - hit[0]
                hit[3] = max(hit[3], box[1] + box[3]) - hit[1]
                hit[4] += 1
    return [(c[0], c[1], c[2], c[3]) for c in clusters if c[4] >= min_frames]


def _clip_box(box: dict, width: int, height: int):
    x = int(box["x"] * width)
    y = int(box["y"] * height)
    w = int(box["w"] * width)
    h = int(box["h"] * height)
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))
    return x, y, w, h


def box_to_mask(width: int, height: int, box: dict) -> np.ndarray:
    """将单个归一化 box 转换为单通道 0/255 遮罩，并轻微膨胀使边缘过渡更自然。"""
    x, y, w, h = _clip_box(box, width, height)
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[y:y + h, x:x + w] = 255
    return _dilate(mask, w, h)


def boxes_to_mask(width: int, height: int, boxes: list) -> np.ndarray:
    """将多个归一化 box 合并为一张并集遮罩（0/255）。"""
    mask = np.zeros((height, width), dtype=np.uint8)
    for box in (boxes or []):
        x, y, w, h = _clip_box(box, width, height)
        mask[y:y + h, x:x + w] = 255
    if not boxes:
        return mask
    maxw = max(b["w"] for b in boxes)
    maxh = max(b["h"] for b in boxes)
    kw = max(3, int(maxw * width // 20))
    kh = max(3, int(maxh * height // 20))
    kernel = np.ones((kw, kh), np.uint8)
    return cv2.dilate(mask, kernel, iterations=1)


def _dilate(mask: np.ndarray, w: int, h: int) -> np.ndarray:
    kw = max(3, w // 20)
    kh = max(3, h // 20)
    kernel = np.ones((kw, kh), np.uint8)
    return cv2.dilate(mask, kernel, iterations=1)


def auto_detect(frame: np.ndarray) -> dict:
    """返回最可能的单个水印框（兼容旧调用）。"""
    boxes = auto_detect_all(frame)
    return boxes[0] if boxes else {"x": 0.7, "y": 0.75, "w": 0.25, "h": 0.18}


def _norm_boxes(scored: list, W: int, H: int) -> list:
    """把像素框转成归一化框，并**水平/垂直各外扩一点**以应对动态水印略微越框。

    外扩幅度：宽 2%、高 4%。文字水印横向排版（窄高），垂直方向更易被切掉一点，
    故高度外扩更大。同时把边缘钳制到 [0, size] 内保证框不越界，并保留最小尺寸。
    """
    pad_w, pad_h = int(W * 0.02), int(H * 0.04)
    out = []
    for pxw, pyw, bw, bh in scored:
        x0 = max(0, pxw - pad_w)
        right = min(W, pxw + bw + pad_w)
        y0 = max(0, pyw - pad_h)
        bot = min(H, pyw + bh + pad_h)
        wb = max(2, right - x0)
        hb = max(2, bot - y0)
        out.append({"x": x0 / W, "y": y0 / H, "w": wb / W, "h": hb / H})
    return out


def _mser_boxes(img: np.ndarray, w: int, h: int) -> list:
    """用 MSER（极值稳定区域，擅长找文字/LOGO）检测候选区域。

    返回像素坐标 (x, y, bw, bh) 列表；MSER 属于 opencv-contrib，
    未安装时返回空列表，由调用方走纯核心的退化路径。
    """
    try:
        mser = cv2.MSER_create(
            _delta=5,
            _min_area=int(w * h * 0.00002),
            _max_area=int(w * h * 0.06),
            _max_variation=0.35,
        )
        regions, _ = mser.detectRegions(img)
    except Exception:
        return []
    out = []
    for r in regions:
        x, y, bw, bh = cv2.boundingRect(r.reshape(-1, 2))
        if bw < w * 0.012 or bh < h * 0.006:
            continue
        out.append((x, y, int(bw), int(bh)))
    return out


def _text_blobs(gray: np.ndarray, w: int, h: int) -> list:
    """纯核心 OpenCV 的文字/LOGO 候选检测（不依赖 MSER）。

    基于 Sobel 梯度能量 + 形态学闭合 + 连通域，返回候选像素框。
    """
    gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    mag = cv2.normalize(mag, None, 0, 255, cv2.NORM_MINMAX).astype("uint8")
    _, edge = cv2.threshold(mag, 70, 255, cv2.THRESH_BINARY)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 5))
    edge = cv2.morphologyEx(edge, cv2.MORPH_CLOSE, k)
    n, _, stats, _ = cv2.connectedComponentsWithStats(edge)
    out = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        bw, bh = int(bw), int(bh)
        if bw < w * 0.015 or bh < h * 0.008:
            continue
        if area < 0.12 * bw * bh:
            continue
        if bw > w * 0.9 and bh > h * 0.9:
            continue
        out.append((x, y, bw, bh))
    return out


def _near(a: tuple, b: tuple) -> bool:
    """判断两个像素框是否为“相邻文字块”（允许随文本高度缩放的极小间隙）。

    相邻的文字字符/单词通常同行且垂直高度相近，用**高度**推导间隙更稳，
    避免把画面里相距很远的两处水印误并成一个巨框。
    """
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    pad = max(2, min(ah, bh) // 2)
    iw = min(ax + aw, bx + bw) + pad - max(ax, bx)
    ih = min(ay + ah, by + bh) + pad - max(ay, by)
    return iw > 0 and ih > 0


def _otsu_blobs(gray: np.ndarray, w: int, h: int) -> list:
    """大津阈值二值化后的连通域文字/水印候选。

    对亮底暗字与暗底亮字各跑一次，专门捕捉与背景对比明显的水印/字幕。
    """
    out = []
    for mode in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
        thr = cv2.threshold(gray, 0, 255, mode + cv2.THRESH_OTSU)[1]
        n, _, stats, _ = cv2.connectedComponentsWithStats(thr)
        for i in range(1, n):
            x, y, bw, bh, area = stats[i]
            bw, bh = int(bw), int(bh)
            if bw < w * 0.015 or bh < h * 0.006:
                continue
            if bw > w * 0.9 and bh > h * 0.9:
                continue
            out.append((x, y, bw, bh))
    return out


def _concat_boxes(boxes: list, w: int, h: int, max_boxes: int) -> list:
    """把相互靠近的文字块聚类合并成整块水印框。

    加了面积上限：连锁合并时若结果面积超过画面 38% 便停止吸收，
    避免纹理噪声在相邻像素上连锁成"整幅误框"。
    """
    boxes = [list(b) for b in boxes]
    max_area = w * h * 0.38
    out = []
    while boxes:
        cur = boxes.pop(0)
        x0, y0, x1, y1 = cur[0], cur[1], cur[0] + cur[2], cur[1] + cur[3]
        changed = True
        while changed:
            changed = False
            rest = []
            for o in boxes:
                cr = (o[0], o[1], o[0] + o[2], o[1] + o[3])
                if _near((x0, y0, x1 - x0, y1 - y0), o):
                    nx0, ny0 = min(x0, cr[0]), min(y0, cr[1])
                    nx1, ny1 = max(x1, cr[2]), max(y1, cr[3])
                    if (nx1 - nx0) * (ny1 - ny0) > max_area:
                        rest.append(o)   # 再合并会超限 → 保留为独立簇
                        continue
                    x0, y0, x1, y1 = nx0, ny0, nx1, ny1
                    changed = True
                else:
                    rest.append(o)
            boxes = rest
        bw, bh = x1 - x0, y1 - y0
        if bw >= max(2, w * 0.02) and bh >= max(2, h * 0.01):
            out.append((x0, y0, bw, bh))
    return out  # 不在这里排序/截断；评分排序交给 auto_detect_all


def _saliency_blobs(saliency: np.ndarray, w: int, h: int) -> list:
    """把显著图的高响应区域直接二值化，作为候选框。

    这是**保证中央 / 低对比水印进入候选集**的关键：MSER/大津/文字边缘
    都依赖边缘强度，中央低对比水印常漏检；而显著图在频域里能把这些
    "叠加感强"的区域点亮，直接圈成候选，再由综合评分去挑。
    """
    _, thr = cv2.threshold(saliency, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    n, _, stats, _ = cv2.connectedComponentsWithStats(thr)
    out = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        bw, bh = int(bw), int(bh)
        if bw < w * 0.02 or bh < h * 0.012:
            continue
        if bw > w * 0.95 and bh > h * 0.95:
            continue
        out.append((x, y, bw, bh))
    return out


def _spectral_saliency(gray: np.ndarray) -> np.ndarray:
    """光谱残差显著性（Spectral Residual Saliency），纯核心 OpenCV + numpy。

    通过对 log 幅度谱减去其局部均值（本质是——抑制画面里"常见/反复出现"的
    谱分量，保留"突兀/叠加"的分量）再反变换，得到一张**显著图**：水印 / LOGO /
    叠加字幕这类"叠加感强"的元素会在图上被点亮，无论它在画面中央还是角落、
    是文字还是纹理。这与水下常见的"半透明叠加标记"高度契合，是提升中央水印
    与低对比水印检出的关键。
    """
    size = 64
    h, w = gray.shape[:2]
    r = cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32)
    fft = np.fft.fft2(r)
    mag = np.abs(fft)
    pha = np.angle(fft)
    log = np.log(mag + 1e-8)
    k = np.ones((9, 9), np.float32) / 81.0
    avg = cv2.filter2D(log, -1, k)
    sr_log = log - avg                       # 光谱残差
    img = np.fft.ifft2(np.exp(sr_log + 1j * pha)).real
    img = (img - img.min()) / (img.max() - img.min() + 1e-8)
    img = (img * 255).astype(np.uint8)
    # 平滑 + 归一化回原尺寸
    img = cv2.GaussianBlur(img, (5, 5), 0)
    img = cv2.resize(img, (w, h), interpolation=cv2.INTER_LINEAR)
    return img


def _score_box(px: int, py: int, pw: int, ph: int,
               gray: np.ndarray, grad: np.ndarray,
               saliency: np.ndarray = None, dl_boost: float = 0.0) -> float:
    """给单个候选水印框打分，分越高越“像真的水印”。

    综合 5 个信号（互相独立、都不需要外部依赖）：
      1) 尺寸合理性：占画面 0.15%~20% 最像水印，过小或过大都扣分；
      2) 显著性：框内显著度均值相对画面整体越高 → 越像“叠加的水印”。这是
         提升**中央水印**与低对比水印检出精度的主要来源；
      3) 局部对比：框内平均亮度与紧邻外框亮度差异越大越显眼；
      4) 内部纹理：框内边缘密度若明显高于其周边 → 半透明文字/LOGO 特征；
      5) 位置优先级：越靠边缘/四角附加小分（水印常见位置，作为弱先验）。
    """
    h, w = gray.shape[:2]
    # ---- 1) 尺寸合理性 ----
    surf = (pw * ph) / max(1, w * h)
    if surf < 0.0015:        size = 0.0
    elif surf <= 0.20:       size = 1.0
    elif surf <= 0.40:       size = max(0.0, 1.0 - (surf - 0.20) / 0.20)
    else:                    size = 0.0  # >40% 直接当作误框

    x0 = max(0, px); y0 = max(0, py)
    x1 = min(w, px + pw); y1 = min(h, py + ph)
    inner = grad[y0:y1, x0:x1]
    bx0, by0 = max(0, px - int(pw * 0.2)), max(0, py - int(ph * 0.2))
    bx1, by1 = min(w, px + int(pw * 1.2)), min(h, py + int(ph * 1.2))

    # ---- 2) 显著性（框内 vs 紧邻外框环带，局部对比更鲁棒） ----
    # 注意：显著性只作“有则加分”，不显著时保持中性 0.5，避免反向压低
    # 中央/低对比水印（光谱残差对高频文字敏感，对平滑色块不敏感）。
    if saliency is not None and inner.size:
        in_sal = float(np.mean(saliency[y0:y1, x0:x1]))
        ring = saliency[by0:by1, bx0:bx1]
        ring_sal = float(np.mean(ring)) if ring.size else in_sal
        boost = (in_sal - ring_sal) / (ring_sal + 12.0) * 2.0   # 相对周边越突出越像水印
        sal = 0.5 + 0.5 * min(1.0, max(0.0, boost))
    else:
        sal = 0.5

    # ---- 3) 局部对比 ----
    inner_b = float(np.mean(gray[y0:y1, x0:x1])) if inner.size else 0.0
    outer_b = float(np.mean(gray[by0:by1, bx0:bx1])) if (by1 > by0 and bx1 > bx0) else inner_b
    contrast = min(1.0, abs(inner_b - outer_b) / 60.0)

    # ---- 4) 内部纹理 ----
    inner_dens = float(np.mean(inner > 60)) if inner.size else 0.0
    outer_dens = float(np.mean(grad[by0:by1, bx0:bx1] > 60)) if (by1 > by0 and bx1 > bx0) else 0.0
    if outer_dens > 0:
        ratio = inner_dens / max(1e-6, outer_dens)
        text = min(1.0, max(0.0, (ratio - 0.7) / 1.0))
    else:
        text = 0.5

    # ---- 5) 位置优先级（弱先验） ----
    cx, cy = px + pw / 2, py + ph / 2
    d_left, d_right = cx, w - cx
    d_top, d_bot = cy, h - cy
    if min(d_left, d_right) < 0.20 * w and min(d_top, d_bot) < 0.20 * h:
        pos = 1.0                                # 四角
    elif min(d_left, d_right) < 0.28 * w or min(d_top, d_bot) < 0.28 * h:
        pos = 0.5                                # 边缘
    else:
        pos = 0.0                                # 中央（不靠位置，靠显著性/对比）

    if size <= 0:
        return 0.0
    base = (0.40 * sal + 0.25 * text + 0.20 * contrast
            + 0.15 * pos + (0.15 if size < 1 else 0.0))
    # 深度学习检出的文字框加权，使其优先于纯启发式候选
    return base + 0.35 * dl_boost


def auto_detect_all(frame: np.ndarray, max_boxes: int = 4) -> list:
    """
    自动检测多处水印 / LOGO / 叠加文字，框可出现在画面任意位置（含中央）。

    策略：
      1. 若分辨率过大先等比缩小（文字检测在适中分辨率下更稳、更快）；
      2. 灰度 + 中值滤波 + CLAHE 增强低对比水印，并计算**光谱残差显著性图**（频域，
         能点亮中央 / 低对比的叠加水印，这是提升准确率的关键新增）；
      3. 用 MSER 检测文字/LOGO 候选（正、反色两次，覆盖深字与浅字），
         未安装 opencv-contrib 时自动退化；
      4. 增加大津阈值二值化连通域候选 + 纯核心文字边缘候选；
      5. 显著图本身也作为候选源：高显著区域二值化连通域 → 确保中央水印进入候选集；
      6. 四个角各取最强文字块兜底，确保角落水印不漏；
      7. 聚类合并相近块，用 **显著性优先的综合评分** 取最可信的前几个。
    """
    h, w = frame.shape[:2]
    if h < 8 or w < 8:
        return []
    if max(h, w) > 1400:
        scale = 1400.0 / max(h, w)
        frame = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    hh, ww = frame.shape[:2]

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.medianBlur(gray, 3)
    enh = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    # 梯度幅度图：供评分用（判断"内部文字纹理 vs 周边"）
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    grad = cv2.magnitude(gx, gy)
    saliency = _spectral_saliency(gray)

    candidates = []
    candidates += _mser_boxes(enh, ww, hh)
    candidates += _mser_boxes(255 - enh, ww, hh)
    candidates += _otsu_blobs(gray, ww, hh)          # 大津阈值连通域
    candidates += _text_blobs(gray, ww, hh)          # 纯核心文字边缘兜底
    candidates += _saliency_blobs(saliency, ww, hh)  # 显著图候选：兜住中央/低对比水印
    candidates += _corner_text(gray, ww, hh)         # 四角最强文字块

    # 深度学习文本检测：水印（文字/带字台标）的主增强候选源。
    # DL 框单独聚类（一行文字连成一条），绝不与大量纹理噪声候选合并，
    # 避免 DL 框被“吞并”成整幅误框；两部分汇合后再统一评分。
    dl_boxes = det_onnx.detect_text_boxes(frame)
    dl_px = _concat_boxes(dl_boxes, ww, hh, max_boxes) if dl_boxes else []
    boxes_px = _concat_boxes(candidates, ww, hh, max_boxes) + dl_px
    if not boxes_px:
        return []

    # 评分排序：只信任深度学习检出的文字框（水印几乎都是文字）；
    # DL 检不到才退回最可信的一个启发式框（纯 Logo 场景兜底）。
    scored = _rank_boxes(boxes_px, dl_px, gray, grad, saliency, ww, hh, max_boxes)
    return _norm_boxes(scored, ww, hh)


def _corner_text(gray: np.ndarray, w: int, h: int) -> list:
    """在画面四角附近取最显著的单个文字/边缘块，作为角落水印兜底。"""
    cw, ch = int(w * 0.35), int(h * 0.28)
    strips = {
        "tl": (0, 0), "tr": (w - cw, 0),
        "bl": (0, h - ch), "br": (w - cw, h - ch),
    }
    out = []
    for x0, y0 in strips.values():
        crop = gray[y0:y0 + ch, x0:x0 + cw]
        if crop.size == 0:
            continue
        blobs = _text_blobs(crop, cw, ch)
        if not blobs:
            continue
        bx0, by0, bw, bh = max(blobs, key=lambda b: b[2] * b[3])
        out.append((x0 + bx0, y0 + by0, bw, bh))
    return out


def auto_detect_video_frames(frames: list, max_boxes: int = 4) -> list:
    """视频水印的**时域**检测——更先进、更准的方案。

    原理：水印是烧录在每一帧上的**静态叠加层**（像素位置固定），
    而画面内容在动。于是：
      - 对同一像素位置跨多帧计算时域方差：水印处**方差低（静止）**，
        移动内容处**方差高**；
      - 同时水印具备清晰的边缘，且这些边缘在**每一帧都出现**：
        对所有帧的梯度做 `min` 归约，水印边缘恒为高值，移动物体边缘被拉低。
      综合“低方差 + 恒现边缘”即可精确圈出水印，基本不受内容运动干扰
      （甚至摄像机平移也不影响——水印相对画面固定，内容才在动）。

    frames: BGR 帧列表（建议 5~8 帧，分散在片头/中段/片尾）。
    返回归一化 boxes。
    """
    if not frames:
        return []

    # 统一到适中的工作尺寸，控制内存与耗时
    ref = frames[0]
    h, w = ref.shape[:2]
    scale = 1.0
    if max(h, w) > 700:
        scale = 700.0 / max(h, w)
    w2, h2 = max(1, int(w * scale)), max(1, int(h * scale))
    if w2 < 8 or h2 < 8:
        return []

    grays, grads, work_frames = [], [], []
    for f in frames:
        fh, fw = f.shape[:2]
        if fh != h2 or fw != w2:
            f = cv2.resize(f, (w2, h2), interpolation=cv2.INTER_AREA)
        work_frames.append(f)
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        g = cv2.medianBlur(g, 3)
        grays.append(g)
        gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
        grads.append(cv2.magnitude(gx, gy))

    gstack = np.stack(grays).astype(np.float32)   # (N, h2, w2)
    var = gstack.var(axis=0)
    var_n = cv2.normalize(var, None, 0, 255, cv2.NORM_MINMAX).astype("uint8")

    # 静止区域 = 时域方差低
    _, static = cv2.threshold(var_n, 90, 255, cv2.THRESH_BINARY_INV)

    # 恒现边缘 = 所有帧梯度 min 归约（水印边缘恒定，内容边缘被拉低）
    static_edge = np.min(np.stack(grads), axis=0)          # float
    smax = float(np.max(static_edge)) or 1.0
    static_edge_n = (static_edge / smax * 255).astype("uint8")
    _, edge_mask = cv2.threshold(static_edge_n, 70, 255, cv2.THRESH_BINARY)

    # 候选 = 静止区域 ∧ 恒现边缘
    cand = cv2.bitwise_and(edge_mask, edge_mask, mask=static)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 5))
    cand = cv2.morphologyEx(cand, cv2.MORPH_CLOSE, k)

    n, _, stats, _ = cv2.connectedComponentsWithStats(cand)
    boxes_px = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        bw, bh = int(bw), int(bh)
        if bw < w2 * 0.02 or bh < h2 * 0.01:
            continue
        if bw > w2 * 0.9 and bh > h2 * 0.9:
            continue
        boxes_px.append((x, y, bw, bh))

    # 深度学习文本检测：对多帧各检一次文字，保留在≥2个采样帧同一位置稳定出现的
    # 框——水印在整段视频里是固定叠加的，且同一位置能抵抗 OCR 偶发漏检，便于
    # 把多个水印都找全；只闪过个别帧的随机文字不会达到 2 次阈值。
    dl_boxes = _stable_dl_boxes(
        [det_onnx.detect_text_boxes(f) for f in work_frames],
        min_frames=2,  # 只要在≥2个采样帧同位置出现即视为稳定水印，防 OCR 偶发漏检
    )
    heuristic_px = boxes_px  # 纯启发式（静止∧恒现边缘）候选

    if not heuristic_px and not dl_boxes:
        return []

    # 显著图（用首帧）供评分：增强中央 / 低对比水印的置信度
    saliency = _spectral_saliency(grays[0])

    # 与单帧一致：只信任跨帧稳定的 DL 文字框；无文字时退回最可信启发式框。
    scored = _rank_boxes(heuristic_px, dl_boxes, grays[0], grads[0], saliency, w2, h2, max_boxes)
    if not scored:
        return []
    return _norm_boxes(scored, w2, h2)
