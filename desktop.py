"""
桌面版启动器：把本工具包成桌面应用（pywebview 原生窗口），替代浏览器访问。

用法（在已装好依赖的 Python 环境中）：
    python desktop.py

与 start.py 的唯一区别：不再打开系统浏览器，而是在进程内启动 FastAPI 服务并
弹出一个原生桌面窗口承载前端。功能（去水印 / 盲水印 / DLSS 增强等）完全一致。

依赖：extra 依赖 pywebview（Windows 默认 EdgeChromium 后端）。若未安装或启动
窗口失败，会自动回退为「打开浏览器」的网页模式，保证可用。
"""
import os
import sys
import socket
import threading

def _app_root() -> str:
    """定位「外置资源根」：PyInstaller 打包后取 exe 所在目录，开发态取本文件
    所在目录（项目根）。model/frontend/lib/data 等外置资源都放这个目录下，
    保证后端对它们的路径解析始终成立（可迁移 / 绿色包）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_ROOT = _app_root()
# 兜底：无论从 exe/脚本/拖拽等任意方式启动，都让进程 CWD 确定指向 APP_ROOT，
# 并把 APP_ROOT 注入环境变量，供 backend 各模块优先读取（开发态自动回退 __file__）。
os.chdir(APP_ROOT)
os.environ["APP_ROOT"] = APP_ROOT

# 把 backend 加入路径，保证 main 可被导入
ROOT = APP_ROOT
sys.path.insert(0, os.path.join(ROOT, "backend"))

import uvicorn  # noqa: E402
import main  # noqa: E402  (构建好 main.app)

TITLE = "羊仔像素工艺 · 本地去水印"


def _free_port() -> int:
    """取一个空闲端口，避免固定 8000 被占用。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _serve(server: uvicorn.Server) -> threading.Thread:
    """在后台 daemon 线程启动 uvicorn。"""
    t = threading.Thread(target=server.run, daemon=True, name="uvicorn")
    t.start()
    return t


def _wait_ready(server: uvicorn.Server, timeout: float = 30.0) -> bool:
    """轮询等服务就绪。"""
    import time
    end = time.time() + timeout
    while time.time() < end:
        if getattr(server, "started", False):
            return True
        time.sleep(0.05)
    return False


def run():
    # PyInstaller windowed 模式下 sys.stdout/stderr 为 None，uvicorn 的
    # DefaultFormatter 会调用 isatty() 而崩溃（AttributeError），这里兜底为 devnull。
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(main.app, host="127.0.0.1", port=port,
                            log_level="warning", reload=False)
    server = uvicorn.Server(config)
    _serve(server)
    if not _wait_ready(server):
        print("[desktop] 服务启动失败")
        return
    print("本地服务已就绪：", url)

    # 优先用 pywebview 弹原生窗口；失败则回退浏览器，保证始终可用。
    try:
        import webview
        # 窗口/任务栏图标：用 asset/app.ico（由 asset/Gusssheep.png 转换，含 16~256 多档尺寸）。
        # 注意：icon 是 webview.start() 的参数（Windows 的 WinForms 后端同样生效）。
        icon_path = os.path.join(ROOT, "asset", "app.ico")
        webview.create_window(
            TITLE, url, width=1320, height=860, resizable=True,
            min_size=(1024, 680), text_select=True,
        )
        webview.start(icon=icon_path if os.path.isfile(icon_path) else None)
        # 窗口已关闭：结束后台服务，并强制结束进程。
        # 即便 pywebview / uvicorn 遗留了非 daemon 线程，也不让其拖住进程。
        # graceful=False：daemon 线程无需优雅退出，直接终止最可靠。
        server.should_exit = True
        try:
            server.thread.join(timeout=2) if getattr(server, "thread", None) else None
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)
    except Exception as e:  # noqa: BLE001
        print("[desktop] 无法创建桌面窗口（%s），回退到浏览器模式。" % e)
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        try:
            threading.Event().wait()  # 保活
        except KeyboardInterrupt:
            pass
        server.should_exit = True


if __name__ == "__main__":
    run()