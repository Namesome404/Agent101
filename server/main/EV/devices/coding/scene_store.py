# -*- coding: utf-8 -*-
"""EV desktop scene: the single source of truth for system surfaces.

The store is transport-agnostic.  FastAPI exposes it over WebSocket while the
coding orchestrator only declares surfaces here.  A stable surface id is
replaced in place; it never implies creating a second window.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from devices.coding import turn_trace

PROTOCOL_VERSION = 1
# Surface ids are deliberately open-ended. Stable ids identify reusable native
# windows; renderer kind/content decides what appears inside them.
SURFACE_IDS = ()
_ALLOWED_INTENTS = {"ambient", "inform", "confront"}
# 窗口数量硬上限：超过后自动淘汰不可见、非常驻的最旧窗口，防止历史残留
# 无上限堆积（曾出现 17 个窗口挤掉 YouTube，模型查不到当前窗口）。
# 上限值保证 voice 的窗口记忆注入不必截断，模型总能看全当前窗口。
SURFACE_LIMIT = int(os.environ.get("MUSE_SCENE_SURFACE_LIMIT", "20") or "20")

Subscriber = Callable[[Dict[str, Any]], None]


class SceneStore:
    def __init__(self, state_path: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._surfaces: Dict[str, Dict[str, Any]] = {}
        self._listeners: List[Subscriber] = []
        self._rev = 0
        self._show_seq = 0
        self._shells: set[str] = set()
        self._ready: Dict[str, Dict[str, Any]] = {}
        self._last_geometry_persist = 0.0
        self._state_path = Path(state_path) if state_path else None
        if self._state_path:
            self._load_persisted()

    def _load_persisted(self) -> None:
        if not self._state_path:
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            surfaces = raw.get("surfaces") if isinstance(raw, dict) else []
            for value in surfaces if isinstance(surfaces, list) else []:
                if not isinstance(value, dict):
                    continue
                surface_id = str(value.get("id") or "")
                if surface_id:
                    normalized = self._normalize(surface_id, value)
                    self._surfaces[surface_id] = normalized
                    try:
                        self._show_seq = max(
                            self._show_seq,
                            int((normalized.get("data") or {}).get("show_rev") or 0),
                        )
                    except (TypeError, ValueError):
                        pass
            self._rev = max(0, int(raw.get("rev") or 0)) if isinstance(raw, dict) else 0
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._surfaces = {}
            self._rev = 0

    def _persist_locked(self) -> None:
        if not self._state_path:
            return
        payload = {
            "version": 1,
            "rev": self._rev,
            "surfaces": list(self._surfaces.values()),
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            pending = self._state_path.with_suffix(".tmp")
            pending.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            pending.replace(self._state_path)
        except OSError:
            # Persistence improves restart continuity but must never break the
            # live Scene protocol when a disk is temporarily unavailable.
            pass

    @property
    def rev(self) -> int:
        with self._lock:
            return self._rev

    def _is_pinned(self, surface_id: str) -> bool:
        """常驻窗判定：延迟 import 避免与 surface_tools 循环依赖。"""
        try:
            from devices.coding.surface_tools import is_pinned_surface
            return bool(is_pinned_surface(surface_id))
        except Exception:
            return surface_id in {"info-board", "status-timeline"}

    def _evict_over_limit_locked(self) -> List[str]:
        """超过 SURFACE_LIMIT 时淘汰最旧、不可见、非常驻窗口，返回被删 id。"""
        limit = max(1, int(SURFACE_LIMIT or 20))
        if len(self._surfaces) <= limit:
            return []
        candidates = [
            sid for sid, surface in self._surfaces.items()
            if not surface.get("visible")
            and not surface.get("focus")
            and not self._is_pinned(sid)
        ]
        victims = []
        for sid in candidates:
            if len(self._surfaces) - len(victims) <= limit:
                break
            victims.append(sid)
        for sid in victims:
            self._surfaces.pop(sid, None)
            self._ready.pop(sid, None)
        return victims

    def _normalize(self, surface_id: str, value: Dict[str, Any]) -> Dict[str, Any]:
        if not surface_id or not isinstance(surface_id, str):
            raise ValueError("scene surface id must be a non-empty string")
        kind = value.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("scene surface must contain a string kind")
        data = value.get("data")
        if not isinstance(data, dict):
            data = {}
        out: Dict[str, Any] = {
            "id": surface_id,
            "kind": kind,
            "data": copy.deepcopy(data),
            "intent": value.get("intent") if value.get("intent") in _ALLOWED_INTENTS else "inform",
            "visible": value.get("visible") is True,
        }
        if value.get("focus") is True:
            out["focus"] = True
        if isinstance(value.get("order"), (int, float)):
            out["order"] = value["order"]
        # Fail early rather than allowing an unserialisable scene to poison WS.
        json.dumps(out, ensure_ascii=False)
        return out

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return unsubscribe

    def _emit(self, event: Dict[str, Any], listeners: List[Subscriber]) -> None:
        for callback in listeners:
            try:
                callback(copy.deepcopy(event))
            except Exception:
                pass

    def upsert(
        self,
        surface_id: str,
        *,
        kind: str,
        data: Optional[Dict[str, Any]] = None,
        intent: str = "inform",
        focus: bool = False,
        order: Optional[float] = None,
        visible: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            previous = self._surfaces.get(surface_id) or {}
            next_data = copy.deepcopy(data or {})
            previous_data = previous.get("data") if isinstance(previous.get("data"), dict) else {}
            if "show_rev" not in next_data and "show_rev" in previous_data:
                next_data["show_rev"] = previous_data["show_rev"]
            raw: Dict[str, Any] = {
                "kind": kind,
                "data": next_data,
                "intent": intent,
                "focus": focus,
                "visible": (
                    bool(visible)
                    if visible is not None
                    else bool(previous.get("visible", True))
                ),
            }
            if focus:
                raw["visible"] = True
            if order is not None:
                raw["order"] = order
            surface = self._normalize(surface_id, raw)
            ops: List[Dict[str, Any]] = []
            if focus:
                for other_id, other_surface in list(self._surfaces.items()):
                    if other_id == surface_id or other_surface.get("focus") is not True:
                        continue
                    unfocused = copy.deepcopy(other_surface)
                    unfocused.pop("focus", None)
                    self._surfaces[other_id] = unfocused
                    self._ready.pop(other_id, None)
                    ops.append({"op": "upsert", "surface": copy.deepcopy(unfocused)})
            if previous != surface:
                self._surfaces[surface_id] = surface
                previous_content = previous_data.get("content")
                next_content = next_data.get("content")
                if previous_content != next_content:
                    self._ready.pop(surface_id, None)
                ops.append({"op": "upsert", "surface": copy.deepcopy(surface)})
            if not ops:
                turn_trace.record_runtime("scene.upsert", {
                    "surface_id": surface_id, "changed": False,
                    "scene_rev": self._rev, "visible": bool(surface.get("visible")),
                }, category="scene")
                return {"changed": False, "rev": self._rev, "surface": copy.deepcopy(surface)}
            # 超过窗口上限时淘汰最旧不可见非常驻窗，remove 并入同一 patch。
            victims = self._evict_over_limit_locked()
            if victims:
                ops.extend({"op": "remove", "id": sid} for sid in victims)
            base = self._rev
            self._rev += 1
            self._persist_locked()
            event = {
                "v": PROTOCOL_VERSION,
                "type": "scene.patch",
                "rev": self._rev,
                "base": base,
                "ops": ops,
            }
            listeners = list(self._listeners)
            self._condition.notify_all()
        self._emit(event, listeners)
        turn_trace.record_runtime("scene.upsert", {
            "surface_id": surface_id, "kind": surface.get("kind"),
            "changed": True, "scene_rev": event["rev"],
            "visible": bool(surface.get("visible")),
            "focused": bool(surface.get("focus")),
            "content_type": str(((surface.get("data") or {}).get("content") or {}).get("type") or ""),
            "listener_count": len(listeners), "shell_count": self.shell_count(),
            "evicted": victims,
        }, category="scene")
        return {"changed": True, "rev": event["rev"], "surface": copy.deepcopy(surface)}

    def request_show(self, surface_id: str, *, focus: bool = True) -> Dict[str, Any]:
        """Reassert visibility even when declarative state already says visible."""
        with self._lock:
            current = copy.deepcopy(self._surfaces.get(surface_id))
            if not current:
                raise KeyError(surface_id)
            self._show_seq += 1
            current.setdefault("data", {})["show_rev"] = self._show_seq
        return self.upsert(
            surface_id,
            kind=current["kind"],
            data=current["data"],
            intent=current.get("intent") or "inform",
            focus=bool(focus),
            order=current.get("order"),
            visible=True,
        )

    def set_visible(self, surface_id: str, visible: bool, *, focus: bool = False) -> Dict[str, Any]:
        with self._lock:
            current = copy.deepcopy(self._surfaces.get(surface_id))
        if not current:
            raise KeyError(surface_id)
        return self.upsert(
            surface_id,
            kind=current["kind"],
            data=current.get("data") or {},
            intent=current.get("intent") or "inform",
            focus=bool(focus and visible),
            order=current.get("order"),
            visible=bool(visible),
        )

    def remove(self, surface_id: str) -> Dict[str, Any]:
        with self._lock:
            if surface_id not in self._surfaces:
                turn_trace.record_runtime("scene.remove", {
                    "surface_id": surface_id, "changed": False, "scene_rev": self._rev,
                }, category="scene")
                return {"changed": False, "rev": self._rev}
            base = self._rev
            self._rev += 1
            del self._surfaces[surface_id]
            self._ready.pop(surface_id, None)
            self._persist_locked()
            event = {
                "v": PROTOCOL_VERSION,
                "type": "scene.patch",
                "rev": self._rev,
                "base": base,
                "ops": [{"op": "remove", "id": surface_id}],
            }
            listeners = list(self._listeners)
            self._condition.notify_all()
        self._emit(event, listeners)
        turn_trace.record_runtime("scene.remove", {
            "surface_id": surface_id, "changed": True,
            "scene_rev": event["rev"], "listener_count": len(listeners),
        }, category="scene")
        return {"changed": True, "rev": event["rev"]}

    def get(self, surface_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            value = self._surfaces.get(surface_id)
            return copy.deepcopy(value) if value else None

    def focused(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            for value in self._surfaces.values():
                if value.get("focus") is True and value.get("visible") is True:
                    return copy.deepcopy(value)
            return None

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            indexed = list(enumerate(self._surfaces.values()))
            indexed.sort(key=lambda item: (item[1].get("order", 0), item[0]))
            return {
                "v": PROTOCOL_VERSION,
                "type": "scene",
                "rev": self._rev,
                "surfaces": copy.deepcopy([surface for _, surface in indexed]),
            }

    def shell_connected(self, shell_id: str) -> None:
        with self._condition:
            self._shells.add(shell_id)
            self._condition.notify_all()
        turn_trace.record_runtime("shell.connected", {
            "shell_id": shell_id, "shell_count": self.shell_count(), "scene_rev": self.rev,
        }, category="shell")

    def shell_disconnected(self, shell_id: str) -> None:
        with self._condition:
            self._shells.discard(shell_id)
            self._condition.notify_all()
        turn_trace.record_runtime("shell.disconnected", {
            "shell_id": shell_id, "shell_count": self.shell_count(), "scene_rev": self.rev,
        }, category="shell", severity="warning")

    def shell_count(self) -> int:
        with self._lock:
            return len(self._shells)

    def mark_surface_ready(
        self,
        surface_id: str,
        *,
        shell_id: str,
        rev: int = 0,
        visible: Optional[bool] = None,
        focused: Optional[bool] = None,
        bounds: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._condition:
            if surface_id in self._surfaces:
                receipt = copy.deepcopy(self._ready.get(surface_id) or {})
                receipt.update({
                    "shell_id": shell_id,
                    "rev": int(rev or self._rev),
                    "at": time.time(),
                    "visible": visible,
                    "focused": focused,
                    "bounds": copy.deepcopy(bounds) if isinstance(bounds, dict) else None,
                })
                self._ready[surface_id] = receipt
                self._condition.notify_all()
                self._sync_dragged_geometry_locked(surface_id, bounds)
                receipt = copy.deepcopy(self._ready[surface_id])
            else:
                receipt = {}
        turn_trace.record_runtime("surface.ready", {
            "surface_id": surface_id, "accepted": bool(receipt),
            "shell_id": shell_id, "rendered_rev": int(receipt.get("rev") or 0),
            "visible": receipt.get("visible"), "focused": receipt.get("focused"),
            "bounds": receipt.get("bounds"),
        }, category="renderer", severity="info" if receipt else "warning")

    def _sync_dragged_geometry_locked(self, surface_id: str, bounds: Any) -> None:
        """把用户鼠标拖出来的真实窗口位置回写为声明几何，避免被后续更新弹回原位。

        壳上报的 bounds 是逻辑坐标（同 data.window.x/y）。若用户拖动过窗口，
        data.window 里的旧 x/y 会与真实位置不一致：模型下一次 set/patch 更新
        内容时，normalize 会用旧的声明 x/y 重建窗口，窗口就会「跳回」拖动前
        的位置。这里同步 x/y，且节流写盘（拖动是高频事件，不每次落盘）。
        """
        if surface_id == "work-hud" or not isinstance(bounds, dict):
            return
        surface = self._surfaces.get(surface_id)
        data = surface.get("data") if isinstance(surface, dict) else None
        if not isinstance(data, dict):
            return
        window = data.get("window")
        if not isinstance(window, dict):
            window = {}
            data["window"] = window
        changed = False
        for key in ("x", "y"):
            value = bounds.get(key)
            if not isinstance(value, (int, float)):
                continue
            value = int(value)
            if window.get(key) != value:
                window[key] = value
                changed = True
        if not changed:
            return
        now = time.time()
        if now - self._last_geometry_persist >= 1.0:
            self._last_geometry_persist = now
            self._persist_locked()

    def mark_content_status(
        self,
        surface_id: str,
        *,
        shell_id: str,
        status: str,
        url: str = "",
        error: str = "",
    ) -> None:
        """Record what the renderer actually loaded, not merely the outer window."""
        if status not in {"loading", "ready", "error"}:
            return
        with self._condition:
            if surface_id not in self._surfaces:
                return
            receipt = self._ready.setdefault(surface_id, {
                "shell_id": shell_id,
                "rev": self._rev,
                "at": time.time(),
            })
            receipt.update({
                "shell_id": shell_id,
                "content_status": status,
                "content_url": str(url or ""),
                "content_error": str(error or "")[:500],
                "content_at": time.time(),
            })
            self._condition.notify_all()
        turn_trace.record_runtime("surface.content_status", {
            "surface_id": surface_id, "shell_id": shell_id, "status": status,
            "url": str(url or ""), "error": str(error or "")[:500],
        }, category="renderer", severity="error" if status == "error" else "info")

    def mark_content_size(
        self,
        surface_id: str,
        *,
        shell_id: str,
        width: int = 0,
        height: int = 0,
    ) -> None:
        """Record the content's measured size, as reported by the renderer."""
        width = max(0, int(width or 0))
        height = max(0, int(height or 0))
        with self._condition:
            if surface_id not in self._surfaces:
                return
            receipt = self._ready.setdefault(surface_id, {
                "shell_id": shell_id,
                "rev": self._rev,
                "at": time.time(),
            })
            receipt["content_size"] = {"width": width, "height": height}
            receipt["content_size_at"] = time.time()
            self._condition.notify_all()
        turn_trace.record_runtime("surface.content_size", {
            "surface_id": surface_id, "shell_id": shell_id,
            "width": width, "height": height,
        }, category="renderer", severity="info")

    def wait_surface_ready(self, surface_id: str, *, min_rev: int = 0, timeout: float = 0.8) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                receipt = self._ready.get(surface_id) or {}
                if receipt and int(receipt.get("rev") or 0) >= int(min_rev or 0):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)

    def wait_content_result(self, surface_id: str, *, timeout: float = 1.5) -> str:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                status = str((self._ready.get(surface_id) or {}).get("content_status") or "")
                if status in {"ready", "error"}:
                    return status
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return status or "unknown"
                self._condition.wait(remaining)

    def inspect(self, *, scope: str = "all", surface_id: str = "") -> Dict[str, Any]:
        with self._lock:
            surfaces = []
            for sid, surface in self._surfaces.items():
                receipt = copy.deepcopy(self._ready.get(sid) or {})
                item = copy.deepcopy(surface)
                item["created"] = True
                item["visible"] = bool(receipt.get("visible")) if receipt.get("visible") is not None else bool(surface.get("visible"))
                item["focused"] = bool(receipt.get("focused")) if receipt.get("focused") is not None else bool(surface.get("focus"))
                item["bounds"] = receipt.get("bounds")
                item["rendered_rev"] = int(receipt.get("rev") or 0)
                item["renderer_ready"] = bool(receipt)
                item["content_status"] = receipt.get("content_status") or "unknown"
                item["content_url"] = receipt.get("content_url") or ""
                item["content_error"] = receipt.get("content_error") or ""
                item["content_size"] = receipt.get("content_size")
                surfaces.append(item)
            if scope == "visible":
                surfaces = [item for item in surfaces if item.get("visible")]
            elif scope == "focused":
                surfaces = [item for item in surfaces if item.get("focused")]
            elif scope == "id":
                surfaces = [item for item in surfaces if item.get("id") == surface_id]
            return {
                "surfaces": surfaces,
                "focused_surface_id": next((item["id"] for item in surfaces if item.get("focused")), ""),
                "shell_connected": bool(self._shells),
                "scene_rev": self._rev,
            }


scene_store = SceneStore(
    state_path=Path(
        os.environ.get("MUSE_SCENE_STATE_PATH")
        or (Path(__file__).resolve().parents[2] / "tmp" / "scene_state.json")
    )
)
