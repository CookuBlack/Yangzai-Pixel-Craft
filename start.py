"""
启动器：自动打开浏览器并运行 FastAPI 服务。
用法（在你自己的 Python 环境中）：
    python start.py
前置：你自己的环境需已安装 requirements.txt 的基础依赖；
     若要使用「高质量 AI（LaMa）」去水印，还需安装 torch(CUDA)+lama-cleaner。
     LaMa 模型权重会在本工具首次需要时自动下载并缓存到
     ~/.cache/lama_cleaner，也可由助手提前为你下载好。
"""
import os
import sys
import webbrowser

# 将 backend 加入路径，保证模块导入正确
BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
sys.path.insert(0, BACKEND)

import uvicorn  # noqa: E402

if __name__ == "__main__":
    url = "http://127.0.0.1:8000"
    print("正在启动视频去水印工具…")
    print("打开浏览器访问：", url)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    uvicorn.run("main:app", host="127.0.0.1", port=8000, log_level="info", reload=True)
