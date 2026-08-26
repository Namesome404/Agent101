import os
import sys
from config.logger import setup_logging
import importlib

logger = setup_logging()


_SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def create_instance(class_name, *args, **kwargs):
    # 创建intent实例
    provider_path = os.path.join(
        _SERVER_ROOT, "core", "providers", "intent", class_name, f"{class_name}.py"
    )
    if os.path.exists(provider_path):
        lib_name = f'core.providers.intent.{class_name}.{class_name}'
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f'{lib_name}')
        return sys.modules[lib_name].IntentProvider(*args, **kwargs)

    raise ValueError(f"不支持的intent类型: {class_name}，请检查该配置的type是否设置正确")