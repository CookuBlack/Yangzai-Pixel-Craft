"""
轻量任务状态存储（进程内存，单进程足够本地工具使用）。
"""
import threading
import time
import uuid

_lock = threading.Lock()
_tasks = {}


def new_task(kind: str) -> str:
    tid = uuid.uuid4().hex[:12]
    with _lock:
        _tasks[tid] = {
            "task_id": tid,
            "kind": kind,
            "status": "staged",
            "progress": 0,
            "message": "已就绪",
            "output": None,
            "created": time.time(),
        }
    return tid


def update(tid: str, **kwargs):
    with _lock:
        if tid in _tasks:
            _tasks[tid].update(kwargs)


def get(tid: str) -> dict:
    with _lock:
        return dict(_tasks.get(tid, {}))
