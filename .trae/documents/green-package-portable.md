# 绿色包打包：把「视频去水印」做成双击即用的便携桌面程序

## Context
用户希望把项目打包为**双击即用的绿色包**（便携文件夹），可在任意 Windows 桌面上直接运行，
无需目标机器预装 conda 环境。项目已是桌面形态（`desktop.py` 用 pywebview 打开原生窗口承载
FastAPI 后端），核心诉求是**将"运行所需的全部解释器 + 依赖 + 模型 + DLSS 运行时"连同程序一起
分发**，双击即启动。

现状关键事实（已核实）：
- 依赖全部装在 conda 环境 `exp311`（`D:\App\Miniconda\envs\exp311`），体积约 **5.9GB**，
  其中 torch+cu121 占约 4.3GB、imageio_ffmpeg 自带 ffmpeg 84MB。
- DLSS 子系统在 `dlss/`：纯 Python `src/` 由后端 `enhance_api.py` 以 `sys.path` 方式懒加载并
  运行于**主 Python 进程**；其二进制在 `dlss/bin/`（ffmpeg 424MB + 嵌入式 Python3.13 432MB
  + runtime 229MB ≈ 1.06GB）。所有路径经 `dlss/src/core/paths.py` 以 `dlss/` 为根相对定位，
  且 DLSS 各 subprocess 均显式传 `cwd=`。嵌入式 Python 有**自己的** site-packages，必须原样保留。
- `model/` ≈ 630MB。
- `desktop.py` / `backend/*.py` 均已通过 `__file__` 相对定位，**无任何硬编码绝对路径**、不依赖 CWD。
- `blind_watermark` 仅存在于项目 `lib/`（运行时加入 sys.path），不在 site-packages。

## 方案总览
打包成一个 `App\` 便携文件夹，内含：
1. 可独立运行的 Python 运行时（从 `exp311` 复制的 `python.exe`/`pythonw.exe` + `python311.dll`
   + `Lib/` + `DLLs/` + `Library/` + 全部 site-packages，裁剪无用部分）。
2. 全部项目代码：`backend` `frontend` `lib` `dlss`（整树） `model` `asset` `third_party`
   `desktop.py`，以及运行时自建的 `data/ jobs/ output/`。
3. 双击启动器：`启动.vbs`（主）+ `启动.bat`（备用）。

不用 PyInstaller 整包（torch+cu121+onnx 整包捆绑极易失败且体积相近）；`pythonw.exe` 本身是
自包含解释器，直接拷贝环境目录即可独立运行（与 conda-pack 同类但不需要 activation 层）。

## 最终包目录结构
```
App\
├─ desktop.py                  # 入口（自带 uvicorn 内嵌 + pywebview 窗口，已含 asset/app.ico 图标）
├─ backend\  frontend\  lib\  asset\  model\  third_party\
├─ dlss\                       # 整树保留，含 bin\（ffmpeg/嵌入python/runtime）
├─ data\  jobs\  output\       # 运行时 self-created（可空，保留目录）
├─ runtime\                    # 剪裁后的 exp311 运行时
│    ├─ python.exe  pythonw.exe  python311.dll
│    ├─ Lib\  DLLs\  Library\
│    └─ site-packages\...      # torch/onnxruntime/iopaint/opencv/fastapi/uvicorn/pywebview/imageio_ffmpeg...
├─ 启动.vbs
└─ 启动.bat
```

## 实现步骤

### 步骤 1：搭建 `App\` 与拷贝项目文件
在项目同级创建 `App\`，拷贝以下（排除 `.venv` `.trae` `.workbuddy` `__pycache__` `.git`
`download_model.py` `setup_ai.bat` 等非运行期文件）：
`backend` `frontend` `lib` `dlss`(整树) `model` `asset` `third_party` `desktop.py`，
并预建空的 `data` `jobs` `output`。

### 步骤 2：剪裁拷贝 Python 运行时
`robocopy D:\App\Miniconda\envs\exp311 App\runtime /E`，排除目录：
`__pycache__` `conda-meta` `include` `libs` `Tools` `Lib\test` `Lib\idlelib` `Lib\lib2to3`
`Lib\tkinter` `Lib\turtledemo` `Lib\curses` `Lib\ensurepip` `Lib\pydoc_data`，
排除文件：`*.pdb`。
随后：
- 递归删除 `Lib\` 与 `Lib\site-packages\` 下的 `.pyc` / `.pyo`。
- **保留** `python.exe` `pythonw.exe` `python311.dll` `python3.dll` `DLLs\` `Lib\`
  `Library\bin\` 与全部 `Lib\site-packages`。
- `torchvision`（18MB）需保留（iopaint/torchhub/propainter_engine 引用），不要删。
- `imageio_ffmpeg` 的 ffmpeg 路径取自其包内，随 site-packages 移动自动有效。
- 不改动任何 `.pth` 文件（复制即相对 embedded prefix 有效）。

### 步骤 3：运行时冒烟测试（在打包机上，仅用 App\runtime）
```
App\runtime\python.exe -c "import torch, cv2, numpy, onnxruntime, iopaint, fastapi, uvicorn, imageio_ffmpeg, webview; print(imageio_ffmpeg.get_ffmpeg_exe()); import sys; sys.path.insert(0, r'App\lib'); from blind_watermark import WaterMark; print('ok')"
```
验证 torch/CUDA/onnx 等均能从此解释器正常导入。

### 步骤 4：编写启动器
- `启动.vbs`（存为 **ANSI/GBK 或 UTF-8 带 BOM**，避免 WSH 乱码）：
  ```vbs
  Set fso = CreateObject("Scripting.FileSystemObject")
  appRoot = fso.GetParentFolderName(WScript.ScriptFullName)
  pyw  = appRoot & "\runtime\pythonw.exe"
  main = appRoot & "\desktop.py"
  Set sh = CreateObject("WScript.Shell")
  sh.CurrentDirectory = appRoot
  sh.Run """" & pyw & """ """ & main & """", 0, False
  ```
- `启动.bat`（UTF-8，`chcp 65001`；需附带 `cd /d "%~dp0"` 以固定 CWD）：
  ```bat
  @echo off
  chcp 65001 >nul
  cd /d "%~dp0"
  start "" "%~dp0runtime\pythonw.exe" "%~dp0desktop.py"
  exit /b 0
  ```

### 步骤 5：desktop.py 便于可靠的绿色启动（小型加固，非必需但推荐）
在 `desktop.py` 顶部加一句 `os.chdir(os.path.dirname(os.path.abspath(__file__)))`，
使无论从 VBS/BAT/拖拽等任意方式启动，进程 CWD 都确定指向包根目录（belt-and-suspenders）。

### 步骤 6：打包为 zip
将 `App\`（默认 → 可提示先解压运行，因无法从 zip 内直接双击启动）用 7-Zip 或
`Compress-Archive` 压为 `视频去水印-绿色版.zip`。体积约 6.5~7GB，需 zip64（7-Zip 默认支持）。

## 需要修改的文件
- [desktop.py](file:///c:/Users/CooKu/Desktop/Creation/Remove/video-watermark-remover/desktop.py)：
  顶部加 `os.chdir(...)` 一行（步骤 5）。
- 新建：`App/启动.vbs`、`App/启动.bat`（打包产物，非仓库内）。

## 验证（端到端）
在本机（不动原 exp311 环境）验证 `App` 自包含：
1. `App\runtime\python.exe desktop.py` 直接运行 → 窗口打开；浏览器开 `http://127.0.0.1:<port>/api/engines`，
   确认 `ai_available:true`、`propainter_available:true`、nvenc 标志，`/api/dlss/status` 的 `ready`。
2. 视频去水印：上传测试 mp4 → 自动检测 → 处理 → 结果写 `App\data\output`，`/api/download/{id}` 含 Range seek。
3. AI（LaMa）去水印 device=cuda 离线加载 `model/big-lama.pt`；随后测试 device=cpu 回退。
4. 盲水印嵌入→提取（用识别码）。
5. DLSS 图/视频增强与插帧，输出到 `App\dlss\outputs`，`/enhanceout` 下载正常。
6. 将整个 `App` 移动到**带空格+非 ASCII 路径**（如 `C:\新 目录\视频去水印`）重新双击 `启动.vbs`，
   重复 2–6 验证可迁移性。
7. 在无 conda/env 的干净机（或新 VM）重复 1–6。

## 风险与裁剪提示
- **体积大头是 torch cu121（~4.3GB，主要是 torch_cuda.dll），无法安全裁剪而不失 GPU AI**；
  最终包约 6.5~7GB。这是 CUDA 绿色包的诚实成本。
- 已安全可裁：`Lib\test` `idlelib` `lib2to3` `tkinter` `turtledemo` `curses` `ensurepip`
  `pydoc_data` `include` `libs` `Tools` `*.pdb` 及全部 `__pycache__`。移除后须重测。
- 避免冒险裁剪 `Library\bin`（56MB，DLL 兜底）及 stdlib 中惰性导入的模块。
- VBS/BAT 启动器可规避 PyInstaller exe 可能触发的杀毒误报。
- `desktop.py` 已有浏览器回退：目标机缺 WebView2/Edge 时自动退回浏览器模式，属可接受的降级。