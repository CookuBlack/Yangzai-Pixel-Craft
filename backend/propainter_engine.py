"""
ProPainter 视频级去水印引擎（ICCV 2023, SOTA video inpainting）。

与 LaMa（单帧生成式）不同，ProPainter 通过 dual-domain propagation + 光流，
从相邻帧"传播真实像素"填补遮罩区域 —— 对水印压在角色/物体上的场景，
恢复的是真实内容而非重绘，角色保持更好。

4GB 显存适配：
- 局部裁剪区域通常远小于 320x240（官方实测 fp16 仅需 2~3GB）
- fp16 推理 + neighbor_length/subvideo_length 减小 + 逐段 empty_cache

依赖：纯 torch 实现（用户环境已具备 torch/torchvision/einops/scipy/PIL），零新增。
权重（3 个文件，共约 190MB）放在项目 model/propainter/ 目录：
  ProPainter.pth / recurrent_flow_completion.pth / raft-things.pth
"""
import os
import sys
import threading

import numpy as np

# 必须在 import torch 之前设置：开启显存分段扩展，显著减少碎片导致的小块 OOM
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

_PROJECT_ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROPAINTER_DIR = os.path.join(_PROJECT_ROOT, "third_party", "ProPainter")
MODEL_DIR = os.path.join(_PROJECT_ROOT, "model", "propainter")

_WEIGHTS = {
    "raft": os.path.join(MODEL_DIR, "raft-things.pth"),
    "flow_complete": os.path.join(MODEL_DIR, "recurrent_flow_completion.pth"),
    "inpaint": os.path.join(MODEL_DIR, "ProPainter.pth"),
}

_LOCK = threading.Lock()
_MODELS = None          # {raft, flow_complete, model, device}
_IMPORTED = False

# 按显存动态调参（4GB 激进、8GB 适中、12GB+ 全量）
_PARAM_PRESETS = {
    "low":  dict(max_side=320,  neighbor_length=6,  subvideo_length=16,
                 ref_stride=10, raft_iter=12, short_clip_len=6),
    "mid":  dict(max_side=384,  neighbor_length=8,  subvideo_length=24,
                 ref_stride=10, raft_iter=16, short_clip_len=8),
    "high": dict(max_side=512,  neighbor_length=10, subvideo_length=40,
                 ref_stride=10, raft_iter=20, short_clip_len=12),
}


def _gpu_memory_mb(device: str) -> int:
    """探测 GPU 显存（MB）；无 CUDA 返回 0。"""
    try:
        import torch
        if device.startswith("cuda") and torch.cuda.is_available():
            return int(torch.cuda.get_device_properties(0).total_memory // (1024 * 1024))
    except Exception:
        pass
    return 0


def _auto_params(device: str) -> dict:
    """按可用显存选择保守参数，并让调用方可覆盖。"""
    mb = _gpu_memory_mb(device)
    if mb == 0:
        return dict(_PARAM_PRESETS["low"], max_side=288)  # CPU 更保守
    if mb < 5000:      # 4GB 卡
        return _PARAM_PRESETS["low"]
    if mb < 9000:      # 6~8GB 卡
        return _PARAM_PRESETS["mid"]
    return _PARAM_PRESETS["high"]


def _import_pp():
    """把 ProPainter 仓库加入 sys.path 并导入所需模块（懒加载）。"""
    global _IMPORTED
    if _IMPORTED:
        return
    if not os.path.isdir(PROPAINTER_DIR):
        raise RuntimeError(
            "未找到 ProPainter 源码目录 third_party/ProPainter，请先拉取官方仓库。"
        )
    if PROPAINTER_DIR not in sys.path:
        sys.path.insert(0, PROPAINTER_DIR)
    # 触发依赖导入，缺包时报出明确错误
    import model.propainter           # noqa
    import model.recurrent_flow_completion  # noqa
    import model.modules.flow_comp_raft     # noqa
    _IMPORTED = True


def weights_ready() -> bool:
    """三个权重文件是否都已就位。"""
    return all(os.path.exists(p) and os.path.getsize(p) > 1024 * 1024
               for p in _WEIGHTS.values())


def is_available() -> bool:
    """引擎是否可用：权重齐 + 源码在 + 能导入。"""
    try:
        _import_pp()
        return weights_ready()
    except Exception:
        return False


def _ensure_loaded(device: str):
    global _MODELS
    _import_pp()
    if not weights_ready():
        raise RuntimeError(
            "ProPainter 权重缺失，请将 ProPainter.pth / recurrent_flow_completion.pth / "
            "raft-things.pth 放入 model/propainter/ 目录。"
        )
    if _MODELS is not None and _MODELS["device"] == device:
        return _MODELS

    import torch
    from model.modules.flow_comp_raft import RAFT_bi
    from model.recurrent_flow_completion import RecurrentFlowCompleteNet
    from model.propainter import InpaintGenerator

    dev = torch.device(device if device.startswith("cuda") and torch.cuda.is_available() else "cpu")
    torch.set_grad_enabled(False)

    raft = RAFT_bi(_WEIGHTS["raft"], dev)
    flow_complete = RecurrentFlowCompleteNet(_WEIGHTS["flow_complete"])
    for p in flow_complete.parameters():
        p.requires_grad = False
    flow_complete.to(dev).eval()
    model = InpaintGenerator(model_path=_WEIGHTS["inpaint"]).to(dev).eval()

    _MODELS = {"raft": raft, "flow_complete": flow_complete,
               "model": model, "device": dev}
    return _MODELS


def _resize_to8(frames_pil, w, h):
    """把帧 resize 到 8 对齐尺寸（ProPainter 要求），避免拉伸尽量取整。"""
    pw, ph = w - w % 8, h - h % 8
    if pw == w and ph == h:
        return frames_pil, (pw, ph)
    out = []
    for img in frames_pil:
        img = img.resize((pw, ph), 2)  # Image.BILINEAR
        out.append(img)
    return out, (pw, ph)


def inpaint_clip(frames_bgr, mask, device="cuda",
                 max_side=None, progress_cb=None, **overrides):
    """
    对一段帧序列做视频级水印去除（自动显存适配 + OOM 自动降级重试）。

    Args:
        frames_bgr: list[np.ndarray]，BGR 帧序列（同尺寸）。
        mask: np.ndarray (H,W) uint8，0/255 水印遮罩（广播到所有帧）。
        device: 'cuda' / 'cpu'。
        max_side: 处理时最长边上限（像素）。默认按显存自动：
            4GB→320 / 8GB→384 / 12GB+→512；超限自动等比缩小后处理再放大贴回。
            传入 0 表示不限制。
        progress_cb: 可选回调，progress_cb(percent: 0~100)。
        **overrides: neighbor_length / ref_stride / subvideo_length / raft_iter /
            mask_dilation / short_clip_len，手动覆盖自动参数（一般不传）。

    Returns:
        list[np.ndarray]：修复后的 BGR 帧序列，仅遮罩区域被修改，其余原样。
        输出尺寸与原输入一致（内部缩放的会放大回原尺寸）。
    """
    import cv2
    import torch
    import scipy.ndimage
    from PIL import Image
    from core.utils import to_tensors
    from model.misc import get_device as pp_get_device

    auto = _auto_params(device)
    params = dict(auto)
    params.update({k: v for k, v in overrides.items() if v is not None})
    if max_side is not None:
        params["max_side"] = max_side
    elif params["max_side"] == 0:
        params["max_side"] = 4096  # 不限制

    scale = 1.0
    for attempt in range(3):  # OOM 自动降级重试，最多 3 次
        with _LOCK:
            try:
                return _inpaint_clip_locked(frames_bgr, mask, device, params, scale,
                                            progress_cb, cv2, torch, scipy.ndimage,
                                            Image, to_tensors, pp_get_device)
            except torch.cuda.OutOfMemoryError:
                if scale <= 0.25 or attempt >= 2:
                    raise
                scale *= 0.6
                torch.cuda.empty_cache()
                # 立即重试（同一次调用内自动降分辨率）
                continue


def _inpaint_clip_locked(frames_bgr, mask, device, params, scale,
                         progress_cb, cv2, torch, scipy_ndi, Image, to_tensors,
                         pp_get_device):
    models = _ensure_loaded(device)
    dev = models["device"]
    raft = models["raft"]
    flow_complete = models["flow_complete"]
    model = models["model"]

    use_half = dev.type == "cuda"
    length = len(frames_bgr)
    if length < 2:
        raise RuntimeError("ProPainter 需要至少 2 帧（视频级修复）")

    neighbor_length = params.get("neighbor_length", 8)
    ref_stride = params.get("ref_stride", 10)
    subvideo_length = params.get("subvideo_length", 30)
    raft_iter = params.get("raft_iter", 20)
    mask_dilation = params.get("mask_dilation", 4)
    short_clip_len = params.get("short_clip_len", 12)

    h, w = frames_bgr[0].shape[:2]
    # 等比缩放到 max_side（保持宽高比 + 8 对齐），处理完放大回原尺寸贴回
    max_side = params.get("max_side", 320)
    target = max(8.0, max_side * scale)
    rw, rh = w, h
    if max(w, h) > target:
        k = target / max(w, h)
        rw = max(8, int(w * k) // 8 * 8)
        rh = max(8, int(h * k) // 8 * 8)

    # ---- 1. 帧 → PIL(RGB)，缩放 + 8 对齐 ----
    frames_pil = [Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)) for f in frames_bgr]
    if (rw, rh) != (w, h):
        frames_pil = [f.resize((rw, rh), 2) for f in frames_pil]  # Image.BILINEAR
    frames_pil, (pw, ph) = _resize_to8(frames_pil, rw, rh)

    # ---- 2. 遮罩：缩放 + 膨胀 → flow_mask / 修复 mask，广播到所有帧 ----
    m = cv2.resize(mask, (pw, ph), interpolation=cv2.INTER_NEAREST)
    flow_mask = scipy_ndi.binary_dilation(m, iterations=mask_dilation).astype(np.uint8)
    m_dil = scipy_ndi.binary_dilation(m, iterations=mask_dilation).astype(np.uint8)
    flow_masks = [Image.fromarray(flow_mask * 255)] * length
    masks_dilated = [Image.fromarray(m_dil * 255)] * length

    # ---- 3. 转张量 ----
    frames_t = to_tensors()(frames_pil).unsqueeze(0) * 2 - 1
    fm_t = to_tensors()(flow_masks).unsqueeze(0)
    md_t = to_tensors()(masks_dilated).unsqueeze(0)
    frames_t, fm_t, md_t = frames_t.to(dev), fm_t.to(dev), md_t.to(dev)

    def _cb(p):
        if progress_cb:
            try:
                progress_cb(max(0, min(100, p)))
            except Exception:
                pass

    # ---- 4. RAFT 双向光流（fp32）----
    video_length = frames_t.size(1)
    gt_flows_f_list, gt_flows_b_list = [], []
    for f in range(0, video_length, short_clip_len):
        end_f = min(video_length, f + short_clip_len)
        if f == 0:
            flows_f, flows_b = raft(frames_t[:, f:end_f], iters=raft_iter)
        else:
            flows_f, flows_b = raft(frames_t[:, f - 1:end_f], iters=raft_iter)
        gt_flows_f_list.append(flows_f)
        gt_flows_b_list.append(flows_b)
        torch.cuda.empty_cache()
        _cb(8)
    gt_flows_f = torch.cat(gt_flows_f_list, dim=1)
    gt_flows_b = torch.cat(gt_flows_b_list, dim=1)
    gt_flows_bi = (gt_flows_f, gt_flows_b)

    # ---- 5. fp16 ----
    if use_half:
        frames_t, fm_t, md_t = frames_t.half(), fm_t.half(), md_t.half()
        gt_flows_bi = (gt_flows_bi[0].half(), gt_flows_bi[1].half())
        flow_complete = flow_complete.half()
        model = model.half()
    _cb(12)

    # ---- 6. 光流补全 ----
    flow_length = gt_flows_bi[0].size(1)
    if flow_length > subvideo_length:
        pred_flows_f, pred_flows_b = [], []
        pad_len = 5
        for f in range(0, flow_length, subvideo_length):
            s_f = max(0, f - pad_len)
            e_f = min(flow_length, f + subvideo_length + pad_len)
            pad_len_s = max(0, f) - s_f
            pad_len_e = e_f - min(flow_length, f + subvideo_length)
            pred_bi_sub, _ = flow_complete.forward_bidirect_flow(
                (gt_flows_bi[0][:, s_f:e_f], gt_flows_bi[1][:, s_f:e_f]),
                fm_t[:, s_f:e_f + 1])
            pred_bi_sub = flow_complete.combine_flow(
                (gt_flows_bi[0][:, s_f:e_f], gt_flows_bi[1][:, s_f:e_f]),
                pred_bi_sub, fm_t[:, s_f:e_f + 1])
            pred_flows_f.append(pred_bi_sub[0][:, pad_len_s:e_f - s_f - pad_len_e])
            pred_flows_b.append(pred_bi_sub[1][:, pad_len_s:e_f - s_f - pad_len_e])
            torch.cuda.empty_cache()
            _cb(18)
        pred_flows_f = torch.cat(pred_flows_f, dim=1)
        pred_flows_b = torch.cat(pred_flows_b, dim=1)
        pred_flows_bi = (pred_flows_f, pred_flows_b)
    else:
        pred_flows_bi, _ = flow_complete.forward_bidirect_flow(gt_flows_bi, fm_t)
        pred_flows_bi = flow_complete.combine_flow(gt_flows_bi, pred_flows_bi, fm_t)
        torch.cuda.empty_cache()
    _cb(25)

    # ---- 7. 图像传播 ----
    masked_frames = frames_t * (1 - md_t)
    img_prop_len = min(100, subvideo_length)
    if video_length > img_prop_len:
        updated_frames, updated_masks = [], []
        pad_len = 10
        for f in range(0, video_length, img_prop_len):
            s_f = max(0, f - pad_len)
            e_f = min(video_length, f + img_prop_len + pad_len)
            pad_len_s = max(0, f) - s_f
            pad_len_e = e_f - min(video_length, f + img_prop_len)
            b, t, _, _, _ = md_t[:, s_f:e_f].size()
            pred_bi_sub = (pred_flows_bi[0][:, s_f:e_f - 1], pred_flows_bi[1][:, s_f:e_f - 1])
            prop_imgs_sub, upd_masks_sub = model.img_propagation(
                masked_frames[:, s_f:e_f], pred_bi_sub, md_t[:, s_f:e_f], "nearest")
            upd_frames_sub = frames_t[:, s_f:e_f] * (1 - md_t[:, s_f:e_f]) + \
                prop_imgs_sub.view(b, t, 3, ph, pw) * md_t[:, s_f:e_f]
            updated_frames.append(upd_frames_sub[:, pad_len_s:e_f - s_f - pad_len_e])
            updated_masks.append(upd_masks_sub.view(b, t, 1, ph, pw)[:, pad_len_s:e_f - s_f - pad_len_e])
            torch.cuda.empty_cache()
            _cb(35)
        updated_frames = torch.cat(updated_frames, dim=1)
        updated_masks = torch.cat(updated_masks, dim=1)
    else:
        b, t, _, _, _ = md_t.size()
        prop_imgs, upd_local_masks = model.img_propagation(masked_frames, pred_flows_bi, md_t, "nearest")
        updated_frames = frames_t * (1 - md_t) + prop_imgs.view(b, t, 3, ph, pw) * md_t
        updated_masks = upd_local_masks.view(b, t, 1, ph, pw)
        torch.cuda.empty_cache()
    _cb(40)

    # ---- 8. 特征传播 + Transformer ----
    def _get_ref_index(mid_neighbor_id, neighbor_ids, ln, stride, ref_num):
        ref_index = []
        if ref_num == -1:
            for i in range(0, ln, stride):
                if i not in neighbor_ids:
                    ref_index.append(i)
        else:
            start_idx = max(0, mid_neighbor_id - stride * (ref_num // 2))
            end_idx = min(ln, mid_neighbor_id + stride * (ref_num // 2))
            for i in range(start_idx, end_idx, stride):
                if i not in neighbor_ids:
                    if len(ref_index) > ref_num:
                        break
                    ref_index.append(i)
        return ref_index

    # 关键：ori_frames 必须使用缩放后（frames_pil）的 RGB 帧，
    # 与 pred_img / bin_masks（处理尺寸 ph×pw）保持同尺寸才能广播。
    ori_frames = [np.array(f) for f in frames_pil]
    comp_frames = [None] * video_length
    neighbor_stride = neighbor_length // 2
    ref_num = subvideo_length // ref_stride if video_length > subvideo_length else -1

    n_windows = len(range(0, video_length, neighbor_stride))
    for wi, f in enumerate(range(0, video_length, neighbor_stride)):
        neighbor_ids = [
            i for i in range(max(0, f - neighbor_stride),
                             min(video_length, f + neighbor_stride + 1))
        ]
        ref_ids = _get_ref_index(f, neighbor_ids, video_length, ref_stride, ref_num)
        sel_imgs = updated_frames[:, neighbor_ids + ref_ids, :, :, :]
        sel_masks = md_t[:, neighbor_ids + ref_ids, :, :, :]
        sel_upd_masks = updated_masks[:, neighbor_ids + ref_ids, :, :, :]
        sel_flows = (pred_flows_bi[0][:, neighbor_ids[:-1], :, :, :],
                     pred_flows_bi[1][:, neighbor_ids[:-1], :, :, :])
        l_t = len(neighbor_ids)

        pred_img = model(sel_imgs, sel_flows, sel_masks, sel_upd_masks, l_t)
        pred_img = pred_img.view(-1, 3, ph, pw)
        pred_img = (pred_img + 1) / 2
        pred_img = pred_img.cpu().permute(0, 2, 3, 1).numpy() * 255
        bin_masks = md_t[0, neighbor_ids, :, :, :].cpu().permute(0, 2, 3, 1).numpy().astype(np.uint8)
        for i in range(len(neighbor_ids)):
            idx = neighbor_ids[i]
            img = pred_img[i].astype(np.uint8) * bin_masks[i] + \
                ori_frames[idx] * (1 - bin_masks[i])
            if comp_frames[idx] is None:
                comp_frames[idx] = img
            else:
                comp_frames[idx] = comp_frames[idx].astype(np.float32) * 0.5 + \
                    img.astype(np.float32) * 0.5
            comp_frames[idx] = comp_frames[idx].astype(np.uint8)
        del pred_img, sel_imgs, sel_masks, sel_upd_masks, sel_flows
        torch.cuda.empty_cache()
        _cb(40 + int(58 * (wi + 1) / max(1, n_windows)))

    # ---- 9. 回原尺寸，转 BGR ----
    out = []
    for i in range(video_length):
        f = comp_frames[i]
        if f is None:
            out.append(frames_bgr[i])
            continue
        if (ph, pw) != (h, w):
            f = cv2.resize(f, (w, h), interpolation=cv2.INTER_CUBIC)
        out.append(cv2.cvtColor(f, cv2.COLOR_RGB2BGR))
    _cb(100)
    torch.cuda.empty_cache()
    return out
