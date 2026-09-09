# 批量处理：逐项预览 + 模板/手动框选混合

## Context（为什么做）

当前批量处理只有「一个全局模板 OR 一份全局水印框」应用到全部素材：
- 前端 [app.js](file:///c:/Users/CooKu/Desktop/Creation/Remove/video-watermark-remover/frontend/app.js) `batchBtn` 提交 `template_name` 或 `boxes` 全局一套；
- 后端 [main.py](file:///c:/Users/CooKu/Desktop/Creation/Remove/video-watermark-remover/backend/main.py) `image_batch`/`batch` 把这份全局配置在 worker 里统一应用。

用户需求：
1. 批量里点击**每个素材**可预览（图片/视频）；
2. 每个素材可**独立选模板 或 手动框选**；
3. 支持**混合**（部分用模板、部分手动、部分跟随全局）；
4. **手动框选优先于模板**（某素材若手动画了框，就用它，即使也套了模板）。

已与用户确认：预览采用**批量面板内联预览区**；素材模板默认**跟随全局批量模板**。

## 方案

### 后端（[main.py](file:///c:/Users/CooKu/Desktop/Creation/Remove/video-watermark-remover/backend/main.py)）——向后兼容扩展

`BatchReq` 新增可选字段：
```python
per_item: Optional[List[Optional[dict]]] = None   # 与 items 对齐，null=跟随全局
```
每个 dict 可为 `{"boxes":[...]}` 或 `{"template_name":"..."}`。

新增解析函数（手动 > 该项模板 > 全局模板 > 全局 boxes）：
```python
def _resolve_batch_config(req, idx, tpls):
    cfg = (req.per_item or [])[idx] if req.per_item else None
    if cfg and cfg.get("boxes"):                     # 手动优先
        return cfg["boxes"], req.engine, req.quality, req.device
    tname = (cfg and cfg.get("template_name")) or req.template_name
    if tname:
        tpl = tpls.get(tname)
        if not tpl: raise HTTPException(404, "模板不存在")
        return (tpl["boxes"], tpl.get("engine", req.engine),
                tpl.get("quality", req.quality), tpl.get("device", req.device))
    return req.boxes, req.engine, req.quality, req.device
```

在 `image_batch` 与 `batch` 中：
- 开头 `tpls = _load_templates()`（仅一次）；
- **删除**原来把全局 `template_name` 提前解析进 `boxes` 的做法；
- worker 循环内改为每项调用 `_resolve_batch_config(req, i, tpls)`，用返回的 `(boxes, engine, quality, device)` 调 `processor.process_image` / `processor.run_job`。

> 完全向后兼容：`per_item` 缺省时行为不变。新增字段不破坏旧请求。

### 前端

#### index.html（批量面板内联预览）
- 文件列表 `#batchFileList` 每行可点击（选中高亮）。
- 列表下方新增预览卡 `#batchPreviewCard`（默认隐藏）：
  - 状态栏：素材名。
  - 复用现有类：`.preview-wrap` + `.boxes-layer`（`#batchPreviewLayer`）+ `<video>/<img>`（`#batchPreviewVideo/#batchPreviewImg`），承载逐项媒体与可拖拽水印框。
  - 控制：逐项模板下拉 `#batchItemTplSel`（首项「跟随全局」+ 模板列表）、「＋ 添加框」「清空」「✨ 自动检测」按钮。

#### app.js
- 状态扩展：`state.batchItems`（与 `batchFiles` 同序）：`{ url, boxes:[], templateName:null, manual:false }`；`state.activeBatchIndex`；`state.boxMode`(`'single'|'batch'`)。
- 素材加入时用 `URL.createObjectURL(file)` 生成预览地址（视频直接用 object URL，无需后端）。
- `renderBatchFiles()`：行可点击 → `selectBatchItem(i)`（显示预览 + 载入该素材 boxes + 模板下拉）。
- **复用框绘制**：把 `renderBoxes` 里“创建单个 wm-box”抽成 `spawnBoxEl(box, idx)`；`setupBoxInteractions`/`applyBoxToEl` 已对 box/el 上下文无关，直接复用。新增：
  - `renderBatchBoxes()`：渲染 `activeBatchIndex.boxes` 到 `#batchPreviewLayer`（进入批量时把 `state.layer` 切到该层，坐标计算才能正确）。
  - `addBatchBox()/clearBatchBoxes()`：操作该项 `boxes` 并置 `manual=true`。
  - `batchItemTplSel` change：写 `templateName`。
  - 逐项「自动检测」：复用 `/api/detect`，`staging_id` 需在上传后才有——见下「提交时序」。
  - 进入/离开批量时用 `state.boxMode` 路由 `renderBoxList`（批量下不写入单编辑器 `#boxTags`），避免互相覆盖。
- **提交时序（关键）**：批量预览用**本地 object URL**（即时可用）；真正处理仍需先 `/api/upload_multi` 或 `/api/upload_images` 取得 `staging_id`（顺序与 `batchItems` 一致）。自动检测在提交后才能带 `staging_id` 调用，故逐项「自动检测」在素材**尚未上传**时给出提示，或改为提交后允许再次预览。本期支持：素材刚加入即可手动框选/选模板；「自动检测」在有 `staging_id` 后可用。
- `batchBtn` 提交时构造：
```js
const per_item = state.batchItems.map((it) => {
  if (it.manual && it.boxes.length) return { boxes: it.boxes.map(stripId) }; // 手动优先
  return it.templateName ? { template_name: it.templateName } : null;        // 跟随全局
});
payload.per_item = per_item;
payload.template_name = $("batchTplSel").value; // 全局兜底
payload.boxes = state.boxes.map(stripId);
```
（仍是可选字段，后端兼容。）

#### styles.css
- 复用现有 `.preview-wrap`/`.boxes-layer`/`.wm-box`（几乎无需新样式）。
- 新增少量：批量文件行 `.active` 高亮、预览卡布局、素材名栏。

## 关键复用点
- [applyBoxToEl](file:///c:/Users/CooKu/Desktop/Creation/Remove/video-watermark-remover/frontend/app.js)（box→DOM 尺寸）
- [setupBoxInteractions](file:///c:/Users/CooKu/Desktop/Creation/Remove/video-watermark-remover/frontend/app.js)（拖拽/缩放）
- `stripId`/`uid`/`cloneBoxes`/`escapeHtml`/`fmtSize`
- `fetchTemplates()` 填充逐项模板下拉
- 后端 `_load_templates()`/`processor.process_image`/`processor.run_job` 无需改

## 验证
1. `python -c "ast.parse(...)"` 校验 main.py/app.js（app.js 用 `node --check`）。
2. 后端重启/reload 后，用 curl 上传 2 张图构造批量，`per_item=[{template_name:X},{boxes:Y}]`，确认第 1 张用模板遮罩、第 2 张用手动框。
3. 浏览器：批量添加 2+ 素材 → 逐个点击预览 → 给第 1 个选模板、第 2 个手动画框（再给第 1 个也手动画框验证「手动优先于模板」）→ 开始 → 校验各自结果遮罩位置正确。
4. 回归：不装新字段的旧批量请求仍按全局 boxes/template 工作。