"""Netlify Functions 入口：将 FastAPI 应用包装为 AWS Lambda handler。

- 依赖由构建命令安装到 netlify/functions（运行时位于 LAMBDA_TASK_ROOT 与 lib/）。
- app/ 源码通过 netlify.toml 的 included_files 打包进函数。
- DEMO_MODE=true：使用进程内存储与种子数据（注意：Serverless 实例冷启动后重置）。
"""

import os
import sys

# 注入 Lambda 运行时路径与依赖目录
_lambda_root = os.environ.get("LAMBDA_TASK_ROOT") or os.path.dirname(os.path.abspath(__file__))
for _p in (_lambda_root, os.path.join(_lambda_root, "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("DEMO_MODE", "true")

from mangum import Mangum  # noqa: E402

from app.main import app  # noqa: E402

handler = Mangum(app, lifespan="off")
