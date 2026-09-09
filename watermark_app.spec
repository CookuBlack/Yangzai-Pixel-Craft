# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置（onedir 模式，含 torch 的完整版）。

设计：
- Analysis 唯一入口为 desktop.py（FastAPI 后台线程 + pywebview 原生窗口）。
- dlss / model / frontend / lib / data 全部**外置**在 exe 旁（不打进 exe），
  后端通过环境变量 APP_ROOT（在 desktop.py 里指向 exe 所在目录）定位它们。
- 由 collect_all 把 imageio_ffmpeg（ffmpeg.exe）、iopaint（静态资源/torch 连带 hook）
  一起收集进 _internal，保证 AI 去水印与视频编码可用。
"""
import os
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# 注意：SPECPATH 本身就是 spec 所在目录（项目根），不要再用 dirname 取上级。
APP_ROOT = os.path.abspath(SPECPATH)

# 收集自带二进制的包：imageio_ffmpeg 的 ffmpeg.exe、onnxruntime 的 DLL、
# iopaint 的模型注册表与静态资源（import 时会连带触发 torch 的 PyInstaller hook）、
# resvg_py / pillow_heif（DLSS 解码器用到，PyInstaller 无 hook，必须显式收全包文件+二进制）、
# cupy（GPU 盲水印嵌入：_core 原生模块与 cuda 运行库需整体收集，供视频嵌入走 GPU）。
extra_datas, extra_binaries, extra_hidden = [], [], []
for _pkg in ("imageio_ffmpeg", "iopaint", "resvg_py", "pillow_heif", "cupy"):
    _d, _b, _h = collect_all(_pkg)
    extra_datas += _d
    extra_binaries += _b
    extra_hidden += _h

a = Analysis(
    ["desktop.py"],
    pathex=[os.path.join(APP_ROOT, "backend")],
    binaries=extra_binaries,
    datas=extra_datas,
    hiddenimports=[
        # 本项目 backend 模块（desktop.py 通过动态 sys.path 导入 main，
        # 静态分析可能漏收，显式列出确保打进 PYZ）
        "main",
        "task_store",
        "video_io",
        "mask",
        "processor",
        "watermark_api",
        "enhance_api",
        "det_onnx",
        "inpaint",
        "propainter_engine",
        "_wm_worker",
        # GPU 深度嵌入（视频嵌入帧率提速），惰性 import，需显式收集。
        # 其内部对 cupy 做带 guard 的懒加载，无法导入时自动维持 CPU 回退。
        "_wm_gpu",
        # uvicorn 子系统（启动器按需加载，显式列出避免漏收）
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.lifespan.on",
        "uvicorn.loops.autoloop",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.wsproto_impl",
        # 表单/多部分上传
        "multipart",
        "multipart.multipart",
        # 深度学习文本检测（惰性 import，需显式收集）
        "onnxruntime",
        # AI 去水印（LaMa / MAT / ProPainter，纯惰性 import）
        "torch",
        "torchvision",
        "iopaint",
        # DLSS 图像解码（外置 dlss/ 动态 import，PyInstaller 静态分析收不到）
        "resvg_py",
        "pillow_heif",
        # 桌面窗口后端
        "webview",
    ] + extra_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sheep_app",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # GUI 应用：不弹黑色控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(APP_ROOT, "asset", "app.ico") if os.path.isfile(os.path.join(APP_ROOT, "asset", "app.ico")) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="sheep_app",
)