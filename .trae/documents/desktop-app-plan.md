# 桌面应用（pywebview 窗口版）实施计划

## Context
项目是「FastAPI 后端 + 静态前端」，现通过 `python start.py` 跑起后由浏览器访问。
本次用 **pywebview** 把它包成桌面窗口：进程内后台线程跑 FastAPI，原生窗口承载前端，彻底替代浏览器。功能零改动。

运行环境：conda `D:\App\Miniconda\envs\exp311\python.exe`（已具备 uvicorn/torch/onnx/cv2 等）。仅缺 `pywebview`。

## 改动清单（极小）
1. **新增 `desktop.py`**（项目根目录）：
   - `sys.path.insert(0, backend)` + `import main`，复用 `main.app`。
   - socket 绑定 `127.0.0.1:0` 取空闲端口（避开 8000 占用）。
   - `uvicorn.Config(main.app, host="127.0.0.1", port=<动态>, reload=False, log_level="warning")`，`uvicorn.Server` 放后台 daemon 线程，轮询等 `server.started`。
   - `webview.create_window("羊仔像素工艺 · 本地", url, width=1320, height=860, resizable=True, min_size=(1000,680))` → `webview.start()`。窗口关闭即进程退出，daemon 线程一并结束。
   - **回退**：`import webview` 失败时打印提示并 `webbrowser.open(url)` 后阻塞保活，行为等价现有 `start.py`，保证不崩。
   - `reload=False`，不额外开浏览器。
2. **新增 `requirements-desktop.txt`**：一行 `pywebview>=5.0`（Windows 默认 EdgeChromium 后端，自动带 pythonnet）。

前端 fetch 均为相对路径，通过窗口地址 `http://127.0.0.1:<port>` 即可正常运行，无需改动前端或后端。

## 依赖安装
```
D:\App\Miniconda\envs\exp311\python.exe -m pip install pywebview
```
注：历史 pip 曾因沙箱写权限失败（WinError 5）。若遇权限错误请在沙箱外/管理员下执行该命令。

## 验证
1. `python desktop.py` 弹出原生窗口（无浏览器、无黑框）。
2. 窗口内验证：图片去水印（OpenCV 路径）、盲水印嵌入→提取、DLSS 增强页状态正常。
3. 关窗后进程退出，无残留 uvicorn。
4. 手动禁 webview 时回退为开浏览器保活。

不做独立 exe（PyInstaller 打包 torch+AI+DLSS 属另一重物任务），待窗口版验证通过后再另行规划。