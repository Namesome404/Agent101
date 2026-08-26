# -*- coding: utf-8 -*-
"""Typed IoT capability registry with explicit execution receipts.

The dialogue layer addresses devices by stable ``device_id`` and capability.
Adapters own transport details and must return an explicit ``meta.ok`` receipt.
This keeps physical-device truth outside prompts and makes new protocols
(Home Assistant, MQTT, Matter, LAN HTTP) additive rather than new agent tools.
"""
from __future__ import annotations

import copy
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterable, Optional


DeviceExecutor = Callable[[str, Dict[str, Any]], Any]


class DeviceCapabilityRegistry:
    """Thread-safe catalog and command bus for physical devices."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._devices: Dict[str, Dict[str, Any]] = {}
        self._device_locks: Dict[str, threading.RLock] = {}
        # 设备最近一次真实回执里的 state 快照（内存缓存，不主动轮询设备）。
        # 供 voice 状态注入：只信这里记录过的实际状态，不给模型「最近调过
        # 哪些工具」的日志（工具名会诱导模型跟调）。
        self._last_state: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _clean_id(value: Any) -> str:
        device_id = str(value or "").strip()
        allowed = set(
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789-_.:"
        )
        if not device_id or any(char not in allowed for char in device_id):
            raise ValueError("device_id 只能包含字母、数字、-、_、.、:")
        return device_id

    def register(
        self,
        device_id: str,
        *,
        name: str,
        kind: str,
        capabilities: Iterable[str],
        executor: DeviceExecutor,
        transport: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        command_args: Optional[Dict[str, Dict[str, str]]] = None,
        adjustable: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        stable_id = self._clean_id(device_id)
        actions = tuple(dict.fromkeys(
            str(item).strip() for item in capabilities if str(item).strip()
        ))
        if not actions:
            raise ValueError("设备至少要声明一个 capability")
        if not callable(executor):
            raise TypeError("executor 必须可调用")
        # 光有能力名不够：模型知道有 color 这个命令，仍然不知道要传 red/green/blue
        # 还是 color_name，只能靠报错试出来——一次调灯因此要三个 LLM 来回。
        # 参数形状跟着设备一起注册，任何适配器（含 MCP 接进来的）都同样受益。
        shapes: Dict[str, Dict[str, str]] = {}
        for capability, spec in (command_args or {}).items():
            action = str(capability).strip()
            if action not in actions or not isinstance(spec, dict):
                continue
            shapes[action] = {
                str(arg).strip(): str(hint)[:120]
                for arg, hint in spec.items() if str(arg).strip()
            }
        with self._lock:
            self._devices[stable_id] = {
                "device_id": stable_id,
                "name": str(name or stable_id),
                "kind": str(kind or "generic"),
                "capabilities": actions,
                "transport": str(transport or ""),
                "metadata": copy.deepcopy(metadata or {}),
                "command_args": shapes,
                "adjustable": copy.deepcopy(adjustable or {}),
                "executor": executor,
            }
            self._device_locks.setdefault(stable_id, threading.RLock())

    def unregister(self, device_id: str) -> bool:
        stable_id = self._clean_id(device_id)
        with self._lock:
            removed = self._devices.pop(stable_id, None) is not None
            self._device_locks.pop(stable_id, None)
        return removed

    def descriptor(self, device_id: str) -> Optional[Dict[str, Any]]:
        try:
            stable_id = self._clean_id(device_id)
        except ValueError:
            return None
        with self._lock:
            spec = self._devices.get(stable_id)
            if not spec:
                return None
            return {
                "device_id": spec["device_id"],
                "name": spec["name"],
                "kind": spec["kind"],
                "capabilities": list(spec["capabilities"]),
                "transport": spec["transport"],
                "metadata": copy.deepcopy(spec["metadata"]),
                "command_args": copy.deepcopy(spec.get("command_args") or {}),
                "adjustable": copy.deepcopy(spec.get("adjustable") or {}),
            }

    def list_devices(self) -> list:
        with self._lock:
            ids = sorted(self._devices)
        return [self.descriptor(device_id) for device_id in ids]

    def world_state(self) -> list:
        """最近一次真实回执里记录到的设备状态快照（纯内存，不发网络请求）。

        只返回有快照的设备；每次写操作/status 成功回执后自动更新。
        供 voice 状态注入渲染「世界现状」，不暴露设备协议与工具名。
        """
        with self._lock:
            return [
                {
                    "device_id": stable_id,
                    "name": spec["name"],
                    "kind": spec["kind"],
                    "state": copy.deepcopy(self._last_state.get(stable_id) or {}),
                }
                for stable_id, spec in sorted(self._devices.items())
                if self._last_state.get(stable_id)
            ]

    def execute(
        self,
        device_id: str,
        action: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        request_id: str = "",
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            stable_id = self._clean_id(device_id)
        except ValueError as error:
            return self._failure("", action, str(error), started, request_id)
        capability = str(action or "").strip()
        with self._lock:
            spec = self._devices.get(stable_id)
            device_lock = self._device_locks.get(stable_id)
        if not spec:
            return self._failure(
                stable_id,
                capability,
                "未注册的设备：%s" % stable_id,
                started,
                request_id,
            )
        if capability not in spec["capabilities"]:
            return self._failure(
                stable_id,
                capability,
                "设备 %s 不支持 %s；可用能力：%s" % (
                    stable_id,
                    capability or "（空）",
                    "、".join(spec["capabilities"]),
                ),
                started,
                request_id,
            )
        correlation_id = str(request_id or uuid.uuid4().hex)
        try:
            # One physical device is a conflict domain: writes and readback must
            # not interleave with another command to the same device.
            with device_lock:
                text, meta = spec["executor"](
                    capability,
                    copy.deepcopy(arguments or {}),
                )
        except Exception as error:  # transport exceptions become receipts
            return self._failure(
                stable_id,
                capability,
                str(error),
                started,
                correlation_id,
            )
        if not isinstance(meta, dict) or "ok" not in meta:
            return self._failure(
                stable_id,
                capability,
                "设备适配器没有返回显式 meta.ok 回执",
                started,
                correlation_id,
            )
        receipt = dict(meta)
        receipt.update({
            "device_id": stable_id,
            "device_name": spec["name"],
            "capability": capability,
            "correlation_id": correlation_id,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        })
        # 回执带实际状态就记住：这是模型判断「现在外面是什么样」的唯一依据。
        if isinstance(receipt.get("state"), dict) and receipt["state"]:
            with self._lock:
                self._last_state[stable_id] = copy.deepcopy(receipt["state"])
        return {
            "ok": bool(receipt.get("ok")),
            "result": str(text or ""),
            "meta": receipt,
        }

    @staticmethod
    def _failure(device_id, action, error, started, request_id=""):
        correlation_id = str(request_id or uuid.uuid4().hex)
        meta = {
            "ok": False,
            "device_id": device_id,
            "capability": str(action or ""),
            "correlation_id": correlation_id,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": str(error)[:1000],
        }
        return {"ok": False, "result": str(error), "meta": meta}


iot_registry = DeviceCapabilityRegistry()
