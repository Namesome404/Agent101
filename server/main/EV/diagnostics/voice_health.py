# -*- coding: utf-8 -*-
"""Read-only voice SLO report built from structured turn diagnostics."""
from __future__ import annotations

import datetime
import json
import math
import os
from collections import deque
from pathlib import Path

from common.paths import TMP_DIR


DEFAULT_PATH = TMP_DIR / "voice_terminal_diagnostics.jsonl"
METRICS = (
    "vad_endpoint_ms",
    "asr_after_vad_ms",
    "llm_first_token_ms",
    "text_buffering_ms",
    "tts_first_audio_ms",
)


def _percentile(values, percentile):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return round(ordered[0], 1)
    position = (len(ordered) - 1) * float(percentile)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 1)
    mixed = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(mixed, 1)


def _tail_lines(path, max_bytes=4 * 1024 * 1024):
    """Read only the file tail; diagnostic logs can grow to hundreds of MB."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - int(max_bytes))
        handle.seek(start)
        data = handle.read()
    if start:
        split = data.split(b"\n", 1)
        data = split[1] if len(split) > 1 else b""
    return data.decode("utf-8", errors="replace").splitlines()


def load_turn_summaries(path=None, *, limit=60):
    source = Path(path or DEFAULT_PATH)
    if not source.is_file():
        return []
    summaries = deque(maxlen=max(1, min(int(limit or 60), 500)))
    for line in _tail_lines(source):
        if "turn_summary" not in line:
            continue
        try:
            item = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if item.get("event") == "turn_summary":
            summaries.append(item)
    return list(summaries)


def build_report(path=None, *, limit=60, local_voice=None):
    turns = load_turn_summaries(path, limit=limit)
    configured_target = float(os.environ.get("VOICE_FIRST_AUDIBLE_TARGET_MS", "1500"))
    completed = [item for item in turns if item.get("outcome") == "completed"]
    failed = [
        item for item in turns
        if item.get("outcome") not in {"completed", "interrupted", "cancelled"}
    ]
    interrupted = [
        item for item in turns
        if item.get("outcome") in {"interrupted", "cancelled"}
    ]
    first_audible = [
        float(item["first_audible_ms"])
        for item in completed
        if isinstance(item.get("first_audible_ms"), (int, float))
    ]
    components = {}
    for metric in METRICS:
        values = [
            float((item.get("components") or {}).get(metric))
            for item in completed
            if isinstance((item.get("components") or {}).get(metric), (int, float))
        ]
        components[metric] = {
            "p50_ms": _percentile(values, 0.50),
            "p95_ms": _percentile(values, 0.95),
        }
    failure_count = len(failed)
    within_target = sum(value <= configured_target for value in first_audible)
    latest = turns[-1] if turns else {}
    voice = dict(local_voice or {})
    running = bool(voice.get("running"))
    p95 = _percentile(first_audible, 0.95)
    healthy_latency = p95 is not None and p95 <= configured_target
    if not turns:
        health = "no_data"
    elif not running:
        health = "offline"
    elif failure_count:
        health = "degraded"
    elif healthy_latency:
        health = "healthy"
    else:
        health = "slow"
    source = Path(path or DEFAULT_PATH)
    return {
        "ok": True,
        "health": health,
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "runtime": voice,
        "sample": {
            "turns": len(turns),
            "completed": len(completed),
            "failures": failure_count,
            "interrupted": len(interrupted),
            "limit": max(1, min(int(limit or 60), 500)),
        },
        "slo": {
            "first_audible_target_ms": configured_target,
            "first_audible_p50_ms": _percentile(first_audible, 0.50),
            "first_audible_p95_ms": p95,
            "within_target_ratio": (
                round(within_target / len(first_audible), 3)
                if first_audible else None
            ),
        },
        "components": components,
        "latest": {
            "time": latest.get("time"),
            "turn_id": latest.get("turn_id"),
            "outcome": latest.get("outcome"),
            "first_audible_ms": latest.get("first_audible_ms"),
            "slowest_stage": latest.get("slowest_stage"),
            "slowest_stage_ms": latest.get("slowest_stage_ms"),
            "upstream": latest.get("upstream") or {},
        } if latest else None,
        "source": str(source),
    }
