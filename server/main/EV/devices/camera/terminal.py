# -*- coding: utf-8 -*-
"""兼容入口：语音终端已迁至 devices.voice.terminal。

旧命令 ``python -m devices.camera.terminal`` 仍可用。
"""
from devices.voice.terminal import *  # noqa: F401,F403
from devices.voice.terminal import main

if __name__ == "__main__":
    main()
