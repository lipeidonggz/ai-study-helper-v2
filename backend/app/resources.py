from __future__ import annotations

import sys
from pathlib import Path


def bundled_resource_path(relative_path: str) -> Path:
    """返回随后端发布的只读资源路径。

    PyInstaller one-folder 构建会把 datas 放到 ``_MEIPASS``；从源码运行时，
    资源位于 ``backend`` 目录。调用方必须继续校验资源内容 hash。
    """

    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("resource path must stay within the bundled resource root")
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return (root / relative).resolve()
