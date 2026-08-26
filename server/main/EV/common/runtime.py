"""Project runtime invariants shared by startup and supervised children."""

import os
import sys
from pathlib import Path

from common.paths import SERVER_DIR


def project_python() -> Path:
    scripts = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return SERVER_DIR / ".venv" / scripts / executable


def require_project_venv() -> Path:
    expected_prefix = (SERVER_DIR / ".venv").resolve()
    actual_prefix = Path(sys.prefix).resolve()
    if actual_prefix != expected_prefix:
        raise RuntimeError(
            "EV 必须使用统一虚拟环境启动：%s；当前环境：%s"
            % (expected_prefix, actual_prefix)
        )
    return expected_prefix
