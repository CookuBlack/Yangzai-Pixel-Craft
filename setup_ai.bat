@echo off
chcp 65001 >nul
echo ============================================
echo   视频去水印工具 - AI 引擎一键安装
echo   将自动下载：PyTorch(CUDA) + LaMa + 模型权重
echo ============================================
echo.

echo [1/3] 安装 PyTorch (CUDA 12.1) 与 LaMa (iopaint)...
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
pip install iopaint
if errorlevel 1 (
  echo.
  echo [错误] 安装失败。请确认：
  echo   1. 已安装 NVIDIA 驱动，且本机 CUDA 版本匹配；
  echo   2. 若 CUDA 不是 12.1，请修改上面的 cu121 为 cu118 / cu124 等。
  echo 详见 https://pytorch.org/get-started/locally/
  pause
  exit /b 1
)

echo.
echo [2/3] 模型权重说明：
echo    - 把 big-lama.pt（约 196MB）放到本目录的 model\ 下，工具会直接加载、不联网；
echo    - 若不放，首次点「高质量 AI」时 iopaint 会自动下载并缓存。

echo.
echo [3/3] 完成！现在运行 python start.py，
echo       在界面「引擎」中选择「高质量 AI（LaMa）」即可。
echo.
pause
