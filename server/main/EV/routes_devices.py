# -*- coding: utf-8 -*-
"""设备与宿主路由：本机音频、摄像头/语音开关、场景快照、网络扬声器、设备 CRUD、
ESP-Claw 工坊、LLM/TTS 预热。

从 app.py 拆出的 APIRouter。扬声器扇出（_SPEAKERS/_speaker_flags/_speaker_bust_cache）
与预热（_openai_client/_resolved_tts/_MINIMAX_TTS_*）在 app_shared。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, FileResponse

from devices.coding import surface_layout

from app_shared import (
    ESP_CLAW_FLASH_DIR,
    _MINIMAX_TTS_STREAM_WS,
    _MINIMAX_TTS_WS,
    _SPEAKERS,
    _clean_http_url,
    _esp_claw_runtime_config,
    _openai_client,
    _resolved_tts,
    _speaker_bust_cache,
    _speaker_flags,
)
from control_plane import database as db
from devices.coding import turn_trace as coding_turn_trace
from devices.coding.scene_store import scene_store
from devices.iot import iot_registry
from diagnostics.voice_health import build_report as build_voice_health_report
from tools import device_control

router = APIRouter()


@router.get("/api/host-audio/devices")
def api_host_audio_devices():
    """枚举本机 PortAudio 输入/输出（供设备页，无需浏览器麦克风权限）。"""
    try:
        import sounddevice as sd
    except Exception as error:
        return {
            "ok": False,
            "error": "sounddevice 不可用: %s" % error,
            "inputs": [],
            "outputs": [],
            "source": "none",
        }
    inputs = []
    outputs = []
    try:
        devices = list(sd.query_devices())
    except Exception as error:
        return {
            "ok": False,
            "error": "枚举失败: %s" % error,
            "inputs": [],
            "outputs": [],
            "source": "portaudio",
        }
    for index, info in enumerate(devices):
        name = str(info.get("name") or ("device-%d" % index)).strip() or ("device-%d" % index)
        low = name.lower()
        if "iphone" in low or "continuity" in low:
            continue
        if int(info.get("max_input_channels") or 0) > 0:
            inputs.append({
                "id": "pa-in-%d" % index,
                "label": name,
                "index": index,
                "ok": True,
            })
        if int(info.get("max_output_channels") or 0) > 0:
            outputs.append({
                "id": "pa-out-%d" % index,
                "label": name,
                "index": index,
                "ok": True,
            })
    return {
        "ok": True,
        "inputs": inputs,
        "outputs": outputs,
        "source": "portaudio",
    }


# 本机麦/喇叭：设备页选用与启停（与本机语音终端共用；不再依赖浏览器麦权限）
@router.get("/api/host-audio")
def api_host_audio_get():
    import json as _json
    def _labels(key, limit=None):
        raw = db.get_setting(key, "[]") or "[]"
        try:
            data = _json.loads(raw)
            items = [str(x) for x in data if str(x).strip()]
            return items[:limit] if limit else items
        except Exception:
            return []
    return {
        "mic_id": db.get_setting("host.audio.mic_id", "") or "",
        "mic_label": db.get_setting("host.audio.mic_label", "") or "",
        "disabled_mic_ids": _labels("host.audio.disabled_mic_ids"),
        "disabled_mic_labels": _labels("host.audio.disabled_mic_labels"),
        "active_mic_ids": _labels("host.audio.active_mic_ids", 1),
        "active_mic_labels": _labels("host.audio.active_mic_labels", 1),
        "spk_id": db.get_setting("host.audio.spk_id", "") or "",
        "spk_label": db.get_setting("host.audio.spk_label", "") or "",
        "disabled_spk_ids": _labels("host.audio.disabled_spk_ids"),
        "disabled_spk_labels": _labels("host.audio.disabled_spk_labels"),
    }


@router.put("/api/host-audio")
def api_host_audio_put(payload: dict = Body(...)):
    import json as _json
    def _save_list(key, value, limit=None):
        items = []
        if isinstance(value, list):
            items = [str(x).strip() for x in value if str(x).strip()]
        if limit:
            items = items[:limit]
        db.set_setting(key, _json.dumps(items, ensure_ascii=False))
    if "mic_id" in payload:
        db.set_setting("host.audio.mic_id", str(payload.get("mic_id") or ""))
    if "mic_label" in payload:
        db.set_setting("host.audio.mic_label", str(payload.get("mic_label") or ""))
    if "spk_id" in payload:
        db.set_setting("host.audio.spk_id", str(payload.get("spk_id") or ""))
    if "spk_label" in payload:
        db.set_setting("host.audio.spk_label", str(payload.get("spk_label") or ""))
    if "disabled_mic_ids" in payload:
        _save_list("host.audio.disabled_mic_ids", payload.get("disabled_mic_ids"))
    if "disabled_mic_labels" in payload:
        _save_list("host.audio.disabled_mic_labels", payload.get("disabled_mic_labels"))
    if "active_mic_ids" in payload:
        _save_list("host.audio.active_mic_ids", payload.get("active_mic_ids"), 1)
    if "active_mic_labels" in payload:
        _save_list("host.audio.active_mic_labels", payload.get("active_mic_labels"), 1)
    if "disabled_spk_ids" in payload:
        _save_list("host.audio.disabled_spk_ids", payload.get("disabled_spk_ids"))
    if "disabled_spk_labels" in payload:
        _save_list("host.audio.disabled_spk_labels", payload.get("disabled_spk_labels"))
    return {"ok": True}


def _feat_voice_enabled() -> bool:
    """语音应答开关：新键 feat.voice，兼容 feat.camera_voice。"""
    raw = db.get_setting("feat.voice", None)
    if raw is None:
        raw = db.get_setting("feat.camera_voice", "1")
    return str(raw or "1") == "1"


def _set_feat_voice(enabled: bool) -> None:
    flag = "1" if enabled else "0"
    db.set_setting("feat.voice", flag)
    # 旧键同步，避免外部脚本仍读 feat.camera_voice
    db.set_setting("feat.camera_voice", flag)


# 摄像头：视频问候（视觉相关）
@router.get("/api/camera/features")
def api_camera_features_get():
    return {
        "greet": db.get_setting("feat.camera_greet", "1") == "1",
        "voice": _feat_voice_enabled(),  # 兼容旧前端；请改用 /api/voice/features
    }


@router.post("/api/camera/features")
def api_camera_features_set(payload: dict = Body(...)):
    if "greet" in payload:
        db.set_setting("feat.camera_greet", "1" if payload["greet"] else "0")
    if "voice" in payload:
        _set_feat_voice(bool(payload["voice"]))
    return {"ok": True}


# 语音终端开关（与摄像头视觉分离）
@router.get("/api/voice/features")
def api_voice_features_get():
    return {"voice": _feat_voice_enabled()}


@router.post("/api/voice/features")
def api_voice_features_set(payload: dict = Body(...)):
    if "voice" in payload:
        _set_feat_voice(bool(payload["voice"]))
    return {"ok": True}


# ============ 网络扬声器 REST ============
@router.post("/api/scene/surfaces/{surface_id}/content_size")
async def api_surface_content_size(surface_id: str, request: Request):
    """窗口内容自报所需高度（沙箱 iframe 里的信标 POST 过来）。

    用 text/plain 发的简单请求，不触发预检；发出去就行，响应它读不到。
    """
    try:
        payload = json.loads((await request.body()).decode("utf-8", "ignore") or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    changed = surface_layout.apply_measured_window_size(
        surface_id,
        height=int(payload.get("height") or 0),
        width=int(payload.get("width") or 0),
        declared_fit="content",
        add_chrome=True,
    )
    return {"ok": True, "resized": bool(changed)}


@router.get("/api/scene")
def api_scene_snapshot():
    """Read-only diagnostics for the desktop scene truth source."""
    return {**scene_store.snapshot(), "shells": scene_store.shell_count()}


@router.get("/api/diagnostics/actions")
def api_action_trace(limit: int = 200, anomalies_only: bool = False, turn_id: str = ""):
    """Read-only causal audit: decisions, tools, receipts, Scene and renderer actions."""
    return {
        "items": coding_turn_trace.read_recent(
            limit=limit, anomalies_only=anomalies_only, turn_id=turn_id,
        ),
        "path": str(coding_turn_trace.ACTION_TRACE_PATH),
    }


@router.get("/api/agents/{aid}/voice/health")
def api_voice_health(aid: int, limit: int = 60):
    """Voice runtime liveness and p50/p95 latency SLOs from real turns."""
    if not db.get_agent(aid):
        return JSONResponse({"ok": False, "error": "智能体不存在"}, status_code=404)
    from control_plane import live_hub
    return build_voice_health_report(
        limit=limit,
        local_voice=live_hub.local_voice_status(aid),
    )


@router.get("/api/iot/devices")
def api_iot_devices(include_status: bool = False):
    """Agent-facing capability catalog; transport details stay in adapters."""
    return {
        "ok": True,
        "devices": device_control.list_devices(include_status=include_status),
    }


@router.post("/api/iot/devices/{device_id}/commands")
def api_iot_command(device_id: str, payload: dict = Body(...)):
    """Execute one typed command and return the adapter's explicit receipt."""
    device_control.ensure_builtin_devices()
    action = str((payload or {}).get("action") or "")
    arguments = (payload or {}).get("arguments")
    if not isinstance(arguments, dict):
        arguments = {
            key: value for key, value in (payload or {}).items()
            if key not in {"action", "request_id"}
        }
    receipt = iot_registry.execute(
        device_id,
        action,
        arguments,
        request_id=str((payload or {}).get("request_id") or ""),
    )
    status = 200 if receipt["ok"] else 409
    return JSONResponse(receipt, status_code=status)


@router.get("/api/speakers")
def api_speakers():
    out = []
    for s in _SPEAKERS.snapshot():
        enabled, gain = _speaker_flags(s["mac"])
        out.append({**s, "enabled": enabled, "gain": gain})
    return {"speakers": out}


@router.post("/api/speakers/toggle")
def api_speaker_toggle(payload: dict = Body(...)):
    mac = (payload.get("mac") or "").strip()
    if not mac:
        return JSONResponse({"ok": False, "error": "缺少 mac"}, status_code=400)
    if "enabled" in payload:
        enabled = bool(payload["enabled"])
        db.set_setting("speaker.enabled:%s" % mac, "1" if enabled else "0")
        # 与设备页能力开关双向同步
        try:
            for d in db.list_devices():
                if d.get("mac") == mac and d.get("device_type") == "speaker":
                    db.set_device_capability(d["id"], "speaker", enabled)
                    break
        except Exception:
            pass
    if "gain" in payload:
        try:
            gain = max(0, min(400, int(payload["gain"])))
            db.set_setting("speaker.gain:%s" % mac, str(gain))
        except (TypeError, ValueError):
            pass
    _speaker_bust_cache()
    return {"ok": True}


@router.get("/api/devices")
def api_devices():
    return {"devices": db.list_devices(), "capabilities": db.capability_catalog()}


@router.post("/api/devices/{did}/attach")
def api_device_attach(did: int, data: dict = Body(...)):
    """按设备 id 绑到智能体（摄像头 / 扬声器 / 已登记未绑设备）。"""
    agent_id = data.get("agent_id")
    if agent_id is None:
        return JSONResponse({"ok": False, "error": "缺少 agent_id"}, status_code=400)
    mac = db.bind_device_by_id(did, agent_id, data.get("name", ""))
    if not mac:
        return JSONResponse({"ok": False, "error": "设备不存在、已绑定或不可绑"}, status_code=400)
    return {"ok": True, "mac": mac}


@router.post("/api/devices/edge")
def api_edge_device_register(data: dict = Body(...)):
    """登记自带 Agent Loop 的设备，不把它绑定成 xiaozhi 瘦客户端。"""
    uid = re.sub(r"[^A-Za-z0-9:._-]+", "-", str(data.get("device_uid") or "").strip())[:120]
    metadata = {
        "platform": "esp-claw",
        "board": str(data.get("board") or "").strip()[:160],
        "board_id": str(data.get("board_id") or "").strip()[:160],
        "chip": str(data.get("chip") or "").strip()[:80],
        "firmware_version": str(data.get("firmware_version") or "").strip()[:80],
        "console_output": str(data.get("console_output") or "").strip()[:80],
        "ip_address": str(data.get("ip_address") or "").strip()[:255],
        "notes": str(data.get("notes") or "").strip()[:500],
    }
    device = db.register_edge_device({
        "device_uid": uid,
        "name": data.get("name"),
        "metadata": metadata,
    })
    if not device:
        return JSONResponse({"ok": False, "error": "设备标识与现有瘦客户端冲突"}, status_code=409)
    return {"ok": True, "device": device}


@router.delete("/api/devices/edge/{did}")
def api_edge_device_delete(did: int):
    if not db.delete_edge_device(did):
        return JSONResponse({"ok": False, "error": "边缘设备不存在"}, status_code=404)
    return {"ok": True}


@router.post("/api/devices/camera")
def api_camera_device_register(data: dict = Body(...)):
    """登记一台摄像头为可绑定设备。src=go2rtc 流名(必填)；
    可选 producer_url：直接把源加进 go2rtc；可选 agent_id：绑定到某智能体。"""
    src = str(data.get("src") or "").strip()
    if not src:
        return JSONResponse({"ok": False, "error": "缺少 go2rtc 流名 src"}, status_code=400)
    go2rtc_url = str(data.get("go2rtc_url") or "").strip() or "http://localhost:1984"
    producer = str(data.get("producer_url") or "").strip()
    if producer:
        try:
            u = go2rtc_url.rstrip("/") + "/api/streams?" + urllib.parse.urlencode({"name": src, "src": producer})
            urllib.request.urlopen(urllib.request.Request(u, method="PUT"), timeout=6)
        except Exception as e:
            return JSONResponse({"ok": False, "error": "加入 go2rtc 失败: %s" % e}, status_code=502)
    dev = db.register_camera_device({
        "src": src, "name": data.get("name"), "go2rtc_url": data.get("go2rtc_url"),
        "agent_id": data.get("agent_id"), "note": data.get("note"),
    })
    if not dev:
        return JSONResponse({"ok": False, "error": "该 src 与非摄像头设备冲突"}, status_code=409)
    return {"ok": True, "device": dev}


@router.delete("/api/devices/camera/{did}")
def api_camera_device_delete(did: int):
    if not db.delete_camera_device(did):
        return JSONResponse({"ok": False, "error": "摄像头设备不存在"}, status_code=404)
    return {"ok": True}


@router.post("/api/devices/bind")
def api_device_bind(data: dict = Body(...)):
    mac = db.bind_device(str(data.get("bind_code", "")).strip(),
                         data.get("agent_id"), data.get("name", ""))
    if not mac:
        return JSONResponse({"ok": False, "error": "绑定码无效或设备已绑定"}, status_code=400)
    return {"ok": True, "mac": mac}


@router.post("/api/devices/{did}/unbind")
def api_device_unbind(did: int):
    db.unbind_device(did)
    return {"ok": True}


@router.post("/api/devices/{did}/rename")
def api_device_rename(did: int, data: dict = Body(...)):
    db.rename_device(did, data.get("name", ""))
    return {"ok": True}


@router.post("/api/devices/{did}/capability")
def api_device_capability(did: int, data: dict = Body(...)):
    """开关设备某一能力。例：{capability:'mic', enabled:false} 只关摄像头麦。"""
    cap = str((data or {}).get("capability") or "").strip()
    if not cap:
        return JSONResponse({"ok": False, "error": "缺少 capability"}, status_code=400)
    if "enabled" not in (data or {}):
        return JSONResponse({"ok": False, "error": "缺少 enabled"}, status_code=400)
    enabled = bool(data.get("enabled"))
    out = db.set_device_capability(did, cap, enabled)
    if not out:
        return JSONResponse({"ok": False, "error": "设备不存在或不具备该能力"}, status_code=400)
    # 网络扬声器：能力开关同步到播放开关
    if cap == "speaker":
        try:
            for d in db.list_devices():
                if d["id"] == did and d.get("device_type") == "speaker":
                    db.set_setting("speaker.enabled:%s" % d["mac"], "1" if enabled else "0")
                    _speaker_bust_cache()
                    break
        except Exception:
            pass
    return {"ok": True, **out}


# ============ ESP-Claw 设备工坊 ============
@router.get("/api/esp-claw/config")
def api_esp_claw_config_get():
    snapshot = {}
    p = ESP_CLAW_FLASH_DIR / "snapshot.json"
    if p.exists():
        try:
            snapshot = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            snapshot = {}
    return {"ok": True, "config": _esp_claw_runtime_config(), "snapshot": snapshot}


@router.put("/api/esp-claw/config")
def api_esp_claw_config_set(data: dict = Body(...)):
    current = _esp_claw_runtime_config()
    for key, fallback in (
        ("versions_url", current["versions_url"]),
        ("firmware_origin", current["firmware_origin"]),
    ):
        if key in data:
            value = _clean_http_url(data.get(key), "")
            if not value:
                return JSONResponse({"ok": False, "error": "%s 必须是 http(s) URL" % key}, status_code=400)
            db.set_setting("esp_claw.%s" % key, value)
    return {"ok": True, "config": _esp_claw_runtime_config()}


@router.get("/api/esp-claw/firmware")
def api_esp_claw_firmware_manifest():
    path = ESP_CLAW_FLASH_DIR / "firmware.json"
    if not path.exists():
        return JSONResponse({"ok": False, "error": "ESP-Claw 固件清单尚未构建"}, status_code=503)
    return FileResponse(str(path), media_type="application/json", headers={"Cache-Control": "no-store"})


# ============ 预热 ============
def _prewarm_agent(aid):
    result = {"llm": "skipped", "tts": "skipped"}
    agent = db.get_agent(aid)
    if not agent:
        return result
    modules = agent.get("modules") or {}
    llm_node = modules.get("LLM") or {}
    llm_name = llm_node.get("selected")
    if llm_name:
        llm_block = dict(db.provider_catalog().get("LLM", {}).get(llm_name, {}) or {})
        llm_block.update(llm_node.get("overrides") or {})
        if llm_block.get("type") == "openai":
            try:
                client = _openai_client(llm_block.get("url"), llm_block.get("api_key"))
                client.models.list()
                result["llm"] = "ready"
            except Exception as error:
                result["llm"] = "failed: %s" % error
    tts_node = modules.get("TTS") or {}
    tts_name = tts_node.get("selected")
    if tts_name:
        tts_block = _resolved_tts(tts_name, tts_node.get("overrides") or {})
        if tts_block.get("type") == "minimax_httpstream":
            try:
                _MINIMAX_TTS_WS.prewarm(tts_block)
                _MINIMAX_TTS_STREAM_WS.prewarm(tts_block)
                result["tts"] = "ready"
            except Exception as error:
                result["tts"] = "failed: %s" % error
        else:
            try:
                from speech.tts import duplex as generic_tts
                if generic_tts.is_streaming_type(tts_block.get("type")):
                    generic_tts.prewarm_generic(tts_name, tts_block)
                    result["tts"] = "ready"
            except Exception as error:
                result["tts"] = "failed: %s" % error
    return result


def _prewarm_llm_agent(aid):
    agent = db.get_agent(aid)
    if not agent:
        return {"status": "skipped", "reason": "agent_not_found"}
    llm_node = ((agent.get("modules") or {}).get("LLM") or {})
    llm_name = llm_node.get("selected")
    if not llm_name:
        return {"status": "skipped", "reason": "llm_not_selected"}
    llm_block = dict(
        db.provider_catalog().get("LLM", {}).get(llm_name, {}) or {}
    )
    llm_block.update(llm_node.get("overrides") or {})
    if llm_block.get("type") != "openai":
        return {"status": "skipped", "reason": "unsupported_provider"}
    started_at = time.perf_counter()
    # models.list() 只建连接不热推理：改发真实最小 chat 请求，把服务端
    # 排队 + prefill 这段最耗时路径提前走一遍，后续真实请求的 stream_ready
    # 从 ~2s 降到 ~300ms。max_tokens=1 控制预热成本。
    try:
        client = _openai_client(llm_block.get("url"), llm_block.get("api_key"))
        model = llm_block.get("model") or ""
        # DeepSeek v4 默认开 thinking，预热请求显式关掉，避免触发推理链路
        overrides = {}
        if (
            "api.deepseek.com" in str(llm_block.get("url") or "").lower()
            and str(model or "").lower().startswith("deepseek-v4")
        ):
            overrides = {"extra_body": {"thinking": {"type": "disabled"}}}
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "好"}],
            max_tokens=1,
            stream=False,
            **overrides,
        )
        return {
            "status": "ready",
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
        }
    except Exception as error:
        # 预热失败不影响主链路（真实请求有重试/failover），但更新就绪时间
        return {
            "status": "ready",
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
            "warmup_error": str(error)[:200],
        }


@router.post("/api/latency/prewarm")
def latency_prewarm(payload: dict = Body(None)):
    aid = int((payload or {}).get("agent_id") or 1)
    return {"ok": True, "result": _prewarm_agent(aid)}


@router.post("/api/llm/prewarm")
def llm_prewarm(payload: dict = Body(None)):
    aid = int((payload or {}).get("agent_id") or 1)
    try:
        return {"ok": True, "result": _prewarm_llm_agent(aid)}
    except Exception as error:
        return JSONResponse(
            {"ok": False, "error": "LLM 连接预热失败: %s" % error},
            status_code=502,
        )
