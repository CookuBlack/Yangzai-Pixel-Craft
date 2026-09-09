# 深度学习水印检测方案

## Context（背景）

用户反复反馈「自动检测」不准：手工特征（MSER / 大津 / Sobel 边缘 / 光谱残差）对**文字类水印**（台标、网址、工作室名称、@账号——中文最常见）检不准、经常漏检或误检中央水印。用户明确要求：「还是不行，上深度学习的模型吧」。

本方案引入一个**真正学出来的文本检测模型**（百度 PP-OCRv4 的 DBnet 检测头），专门负责圈出画面里的中英文/任意位置文字区域，再和现有显著性/启发式候选融合打分。Watermark 绝大多数是"叠加文字/带字 Logo"，因此这是最对症、提升最明显的深度学习升级。

已验证环境：运行时 Python 为 `D:\App\Miniconda\envs\exp311`（torch 2.5.1 CUDA + cv2 4.10）。onnxruntime 未安装，需要安装（轻量 CPU wheel）。

已验证模型源（HTTP 200 可达）：
`https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.3.0/onnx/PP-OCRv4/det/ch_PP-OCRv4_det_infer.onnx`（约 4.6MB）

## 方案选择理由

- 用 **onnxruntime + PP-OCRv4 det ONNX**，而非 EAST/torch 模型：模型仅 ~4.6MB、对中文更准、支持任意位置与旋转文字；onnxruntime 是单包轻依赖，不引入沉重的 paddlepaddle/tensorflow。
- 不替换、而是**增强**现有管线：把深度学习文字框作为"候选源"，并入现有的 `_concat_boxes` + `_score_box` 打分排序，保留显著性兜底用于非文字 Logo。这样即使模型缺失也能优雅退化到旧逻辑，后端 API 不变（满足硬约束）。
- 仅用 CPU 推理（960px 检测约几十 ms），避免 onnxruntime-CUDA 与 cudnn 版本匹配难题。

## 实施步骤

### 1. 安装依赖
- 在 `D:\App\Miniconda\envs\exp311` 执行：`pip install onnxruntime`
- 下载 det 模型到 `video-watermark-remover/model/det/ch_PP-OCRv4_det_infer.onnx`
- 在 `requirements.txt` 追加 `onnxruntime>=1.15`

### 2. 新增文件 `backend/det_onnx.py`（深度学习文本检测器）
对外：
- `detect_text_boxes(bgr_frame) -> list[(x, y, w, h)]`（像素坐标）；模型不可用/推理失败返回 `[]`。
- 内部 `_get_session()` 惰性单例加载 onnx，`try` 包裹、失败返回 None，避免拖垮已有检测。

预处理：BGR→RGB → 等比 resize（最长边 960，且取 32 的倍数）→ `/255` → 按 ImageNet 归一化(mean=[0.485,0.456,0.406], std=[0.229,0.224,0.225]) → NCHW float32。

后处理（DB postprocess 的简化稳健版）：
- prob 图二值化（thresh≈0.3）
- 找轮廓 → `cv2.minAreaRect` 旋转框 → 按 unclip_ratio≈1.6 略外扩 → 过滤过小/过扁/占比过大的框 → `cv2.boundingRect` 得到轴对齐框
- 框坐标按缩放比例映射回原分辨率

### 3. 修改 `backend/mask.py`（篡改点，复用现有打分）
- 在 `auto_detect_all`：`import det_onnx`，调用 `det_onnx.detect_text_boxes(frame)` 得到 DL 文字候选，追加进 `candidates`，再走原有 `_concat_boxes` + `_score_box` 排序。给 DL 框在 `_score_box` 里加一个"文字置信度加成"（新参数 `dl_boost`），避免被弱启发式框压下去。
- 在 `auto_detect_video_frames`：对抽样帧在多个时间点跑 det，汇总各帧返回的文字框，仅保留"多数帧都出现/坐标稳定"的框并入候选，其余逻辑不变。

### 4. 修改 `backend/main.py`
- 无需改 API；仅在 `detect` 处（图片/单帧/视频时域路径）自然经过 mask 新逻辑即可。确认无破坏。

### 5. 前端 app.js / styles.css
- 无需改动：返回的仍是归一化 boxes，前端画框逻辑不变。

## 关键改动文件
- `backend/det_onnx.py`（新增）：onnx 加载 + 预处理/后处理，返回像素文字框
- `backend/mask.py`（修改）：`auto_detect_all` / `auto_detect_video_frames` 并入 DL 候选；`_score_box` 增加 DL 文字加成
- `requirements.txt`（修改）：追加 `onnxruntime`
- `model/det/ch_PP-OCRv4_det_infer.onnx`（新增，下载）

## 验证
1. 启动 `D:\App\Miniconda\envs\exp311\python.exe start.py`（或重启现有后端）
2. 前端上传一张含中文文字水印的图片 → 点「自动检测」→ 应圈出文字水印
3. 上传含文字/台标水印的视频 → 点「自动检测」→ 时域路径应稳定圈出水印
4. 上传纯 Logo（非文字）图片 → 应仍能通过显著性管道检出（退化路径正常）
5. 确认 `curl -X POST /api/detect` 返回格式仍是 `{"boxes":[...]}`（API 未变）