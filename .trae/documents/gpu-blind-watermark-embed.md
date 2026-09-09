# 视频盲水印嵌入：深度 GPU 加速方案

## Context（为什么做）

「羊仔像素工艺」的视频盲水印嵌入目前是**纯 CPU**：`lib\blind_watermark\bwm_core.py` 对每帧做 haar DWT + 每个 4×4 块的 DCT/SVD，全部走 numpy/pywt/cv2，再靠 CPU 多进程并行。瓶颈就在这一段逐帧、逐块的 DWT+SVD 计算。

目标：把嵌入计算从 CPU 改写为 **GPU（cupy）批量执行**——把一批帧的所有 4×4 块堆叠成 `(N*blocks, 4, 4)` 数组，一次性批量 SVD / 系数注入 / 批量 IDWT，实现大颗粒并行提速。**嵌入结果必须能被现有 CPU 提取端解出**（3 档强度回归逐位一致），且不动后端 API 契约、不动前端。

已确认环境：RTX 3050（4GB 显存，CC 8.6），驱动 610.47（支持 CUDA 13.3 UMD，向后兼容 12.x 运行时）。`D:\App\Miniconda\envs\exp311` **未装 cupy**，需先安装。

## 关键设计结论

- 嵌入/提取的唯一契约是「像素 + 分块坐标」。不要求 GPU 与 CPU 逐位相等，只需重建的 uint8 像素经提取端重新 CPU 走一遍后，奇异值 `s[0]` 落在目标半区即解出。半区余量最低（quality 档 d1=14）也达 3.5，远大于 float32 累计误差，安全。
- **唯一硬性精确项是 haar DWT 的分块坐标相位必须与 pywt 一致**（否则位序错、必错）。用 init 强制对拍自检兜底。
- u/v 的符号/相位差异不影响结果：SVD 重建不变量 `u·diag(s)·v` 与位判定 `(s[0] % d1 > d1/2)` 只看奇异值本身。
- **色域 BGR↔YUV 保留走 CPU cv2**（整批一次向量化，开销小、风险最低），GPU 专注 DWT/DCT/SVD 这一主要耗时点。

## 实施步骤

### 1. 安装 cupy 并冒烟
```powershell
& D:\App\Miniconda\envs\exp311\python.exe -m pip install "cupy-cuda12x"
```
验证脚本：导入 cupy；`cp.linalg.svd` 对 `(batch,4,4)` 能批量返回奇异值；读 `cp.cuda.Device(0)` 名含 3050、compute capability ≥ (8,6)。cupy 无法导入/无设备时，`available()` 返回 False → 无缝回退 CPU。

### 2. 新建 `backend\_wm_gpu.py`（GPU 批量嵌入模块）
对外接口（其余内部封装）：
- `available() -> bool`：cupy 可导入 + 有设备 + 自检（haar 相位对拍、DCT 基矩阵对拍、一次性 SVD 对拍）全部通过才返回 True。
- `embed_batch_size(h, w, mem_cap_mb=1800) -> int`：按像素与 4GB 显存自适应批大小，clamp `[4, 64]`。
- `batch_embed(wm_cfg: dict, frames: np.ndarray) -> np.ndarray`：输入 BGR uint8 `(N,H,W,3)`，输出同型嵌入帧。

模块内**一次性预计算**（与帧无关，跨批复用，上传到 GPU）：
- `idx_shuffle` / `idx_shuffle_inv`（按 `password_img` 与块数，`random_strategy1`，`(nb,16)` 与逆排列）。
- `wm_vector`（`(nb,)`，`wm_1[i] = wm_bits[i % wm_size]`）。
- 4×4 正交 DCT 基矩阵 `T`：`T[k,n]=α_k·cos(πk(2n+1)/8)`，`α0=1/2`，`α1..3=1/√2`。

批处理主体（对每批 N 帧）：
1. CPU：`cv2.cvtColor` 把 BGR 帧转 YUV float32，逐帧补白到偶数（与现有 `read_img_arr` 的加边方式一致）。YUV 一次性 `np.stack` 上传 GPU。
2. GPU 每通道：**批量 2D haar DWT**（`cupy` 用 reshape 拆分奇偶列/行实现，复刻 pywt `'haar'` 系数的 cA 左上 / cH 右上 / cV 左下 / cD 右下相位），得到 ca。
3. GPU 把 ca 分块成 `(N, nb, 16)`（`reshape + transpose`，C 序 4×4 展平，与 CPU `block.flatten()` 对齐）。
4. **批量置乱**：`cp.take_along_axis(B, idx_shuffle[None,:,:], axis=2)` → reshape `(N*nb,4,4)` → **批量 DCT**（`cp.einsum('ik,bkm,jm->bij', T, X, T)`）→ **批量 SVD**（`cp.linalg.svd`）。
5. 注入系数：`s0=(s0//d1 + 0.25 + 0.5·wm)*d1`，`d2` 存在同理改 `s1`；`X_new = einsum(u, diag(s_hav), v)`。
6. **批量 IDCT** → reshape `(N,nb,16)` → **批量逆置乱**（`take_along_axis(B, inv)`）→ reshape/transpose 回 `(N, hb, wc, 4, 4)` → 回填 ca 主体 → **批量 2D IDWT** → 得每通道重建 YUV。
7. 3 通道 stack、下载回 CPU、裁补边、`cv2.cvtColor(COLOR_YUV2BGR)`、`clip(0,255).astype(uint8)`。
8. `cp.cuda.Device(0).synchronize()` 后释放内存池临时块（`free_all_blocks`），避免跨批峰值叠加。

异常（缺 cupy / OOM / 自检断言失败 / 奇异 svd）一律抛出，由调用方回退 CPU。

### 3. 接入 `backend\watermark_api.py` 的 `_embed_video`
L470-515 的循环改为：优先 `_wm_gpu.available()` → 走 GPU 批量路径（读 N 帧 → `batch_embed` → 复用现有 `ThreadPoolExecutor` 并行写 BMP）；否则抛异常落回**现有 ProcessPool 多进程路径**（原样保留），再失败落 `_serial_embed`。`wm_cfg`、`_build_img_wm_stream`、强度档位、提取元数据契约全部不动。进度回调复用现有逻辑。

### 4. 打包 exe 启用 GPU（用户要求）
`watermark_app.spec`：把 `"cupy"` 加入第 24 行 `collect_all` 循环（CuPy 官方支持 PyInstaller 的 `--collect-all cupy` 方式，会收全 python 模块 + CUDA 库 DLL）。`pack.bat` 无需改（自动走 spec）。
风险与对策：cupy-cuda12x 与现有 torch cu121 都带 cudart/cublas 同名 DLL，onedir 内可能按名去重导致版本冲突。实施时需实测打包产物；若冲突，把 cupy 的 CUDA 运行库 DLL 单独放入 `cupy_backends` 下由 cupy 自行加载的方式规避。`available()` 在打包/加载异常时返回 False 自动回退 CPU，保证 exe 始终可用。

### 5. 同步 dist
与以往一致，把改动后的 `backend\watermark_api.py`、`backend\_wm_gpu.py`、`lib\blind_watermark\bwm_core.py` 同步到 `dist\yangzai\backend\`、`dist\yangzai\lib\`（以及 spec/pack 改动）。源码/conda 运行时可直接启用 GPU。

## 验证

1. **单元对拍**（模块 init 自检或脚本一次性校验）：
   - 批量 haar DWT vs `pywt.dwt2`，逐通道差值 < 1e-5（锁相位）；
   - 批量 DCT/IDCT vs `cv2.dct/idct`，差值 < 1e-5；
   - `cp.linalg.svd` vs `np.linalg.svd` 于 ≥1e4 个真实分块，`max|Δs| < 1e-3·d1` 且 `(s//d1)` bin 号 100% 一致。
2. **位流对拍**：GPU 嵌入合成帧 → 现有 `bwm_core.extract_raw` / `_img_raw_bits` 提取，三档 strength（quality/balanced/robust）与原始 wm_bits 逐位一致。
3. **端到端回归**：~30 帧 720p 短片，GP通过 GPU 路径嵌入 → 调现有 `/api/wm/extract` 用保存的 wm_key 自动带参数，断言还原文字/图片水印与原输入一致。
4. **回退审计**：临时禁用 cupy（或模拟异常）时走 CPU 多进程路径，功能与现状一致。
5. **打包冒烟**：`pack.bat` 产物能启动，GPU 路径可用；若 cupy 收集失败则确认自动回退 CPU 且 exe 正常。

## 涉及文件
- 新建：`backend\_wm_gpu.py`
- 修改：`backend\watermark_api.py`（`_embed_video` 接入 + 回退）
- 修改：`watermark_app.spec`（`collect_all` 加 `cupy`）
- 同步：`dist\yangzai\backend\`、`dist\yangzai\lib\`
- 仅作数值基准、不改动：`lib\blind_watermark\bwm_core.py`、`backend\_wm_worker.py`