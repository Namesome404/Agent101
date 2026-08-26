# -*- coding: utf-8 -*-
"""窗口能力 skill 兼容入口（facade）。

窗口能力已按职责拆到三个模块，本文件只做 re-export，保持
`from devices.coding import surfaces` 的旧调用不变：

- surface_layout：几何/JSON patch/定义规范化（纯函数层）
- surface_tools：工具定义/执行器/注册/记录模式状态（工具层）
- surface_hints：窗口记忆/反幻觉/光说不做拦截（提示层）
"""
from __future__ import annotations

from devices.coding.surface_hints import (
    execution_check_message,
    is_pure_info,
    memory_hint,
    search_results_hint,
    pending_input_ack,
    pending_input_snapshot,
    record_mode_hint,
    truth_system,
    unbacked_completion,
)
from devices.coding.surface_layout import (
    _aabb_overlap,
    _append_apply_patches,
    _visible_bounds,
    auto_place_window,
    deep_merge_dict,
    find_free_position,
    json_patch_apply,
    json_pointer_parts,
    normalize_web_surface_definition,
    reject_relative_geometry,
    safe_surface_color,
    surface_resolve_id,
)
from devices.coding.surface_tools import (
    _PENDING_INPUT,
    _PENDING_LOCK,
    _clear_pending_for_surface,
    execute,
    register,
    surface_expect_input_execute,
    surface_expect_input_tool_definition,
    surface_inspect_execute,
    surface_inspect_tool_definition,
    surface_manage_execute,
    surface_manage_tool_definition,
    tool_definitions,
)

__all__ = [
    # 工具入口
    "tool_definitions",
    "execute",
    "register",
    "surface_manage_execute",
    "surface_inspect_execute",
    "surface_expect_input_execute",
    # 提示层
    "memory_hint",
    "search_results_hint",
    "truth_system",
    "record_mode_hint",
    "pending_input_snapshot",
    "pending_input_ack",
    "is_pure_info",
    "unbacked_completion",
    "execution_check_message",
    # 布局层
    "safe_surface_color",
    "surface_resolve_id",
    "deep_merge_dict",
    "json_pointer_parts",
    "json_patch_apply",
    "normalize_web_surface_definition",
    "find_free_position",
    "auto_place_window",
    "reject_relative_geometry",
]
