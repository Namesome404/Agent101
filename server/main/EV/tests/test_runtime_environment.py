import sys
from pathlib import Path

import pytest

from common import runtime


def test_current_test_process_uses_project_venv():
    assert runtime.require_project_venv() == runtime.project_python().parents[1]


def test_system_python_prefix_is_rejected(monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(Path("/tmp/not-ev-venv")))
    with pytest.raises(RuntimeError, match="统一虚拟环境"):
        runtime.require_project_venv()
