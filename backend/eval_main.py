from __future__ import annotations

import argparse

import uvicorn

from app.evaluation.api import create_eval_app


def parse_args() -> argparse.Namespace:
    """解析评测 sidecar 的回环端口参数。"""

    parser = argparse.ArgumentParser(description="Run the evaluation sidecar")
    parser.add_argument("--port", type=int, required=True)
    return parser.parse_args()


def main() -> None:
    """启动只绑定本机回环地址的评测 sidecar。"""

    args = parse_args()
    app = create_eval_app()
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
