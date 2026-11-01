"""极简 .env 加载器（无需 python-dotenv）

用法：在模块顶部 `import env_loader` 即可（自动读取同目录 .env）。
规则：
  - KEY=VALUE，支持 # 注释、行内空格、单双引号
  - 已存在的环境变量优先，不覆盖（方便临时 set 覆盖）
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_FILE = Path(__file__).resolve().parent / ".env"


def load(path: Path | None = None) -> int:
    p = path or _ENV_FILE
    if not p.is_file():
        return 0
    n = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key or key in os.environ:
            continue
        os.environ[key] = val
        n += 1
    return n


load()
