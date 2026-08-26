# -*- coding: utf-8 -*-
"""Muse 对话客户端：流式 chat，供 VoiceCore 喂分句。"""

import json
import threading
import time

import requests


class MuseDialogClient:
    """调用 EV `/api/agents/{id}/chat/stream`，产出文本 delta。"""

    def __init__(self, muse_url, agent_id, http_session=None):
        self.muse_url = (muse_url or "http://127.0.0.1:8002").rstrip("/")
        self.agent_id = int(agent_id)
        self._http = http_session or requests.Session()

    def chat_stream(
        self,
        text,
        metrics=None,
        history=None,
        cancel_event=None,
        addressed_hint="conversation_window",
        voice_mode=True,
        speaker_name=None,
        speaker_score=None,
        speaker_status=None,
        known_speakers=None,
    ):
        original_text = text
        if cancel_event is not None and cancel_event.is_set():
            return
        request_started_at = time.perf_counter()
        body = {
            "message": text,
            "voice_mode": voice_mode,
            "history": history or [],
            "addressed_hint": addressed_hint,
        }
        if speaker_name:
            body["speaker_name"] = str(speaker_name)
            if speaker_score is not None:
                try:
                    body["speaker_score"] = float(speaker_score)
                except (TypeError, ValueError):
                    pass
        if speaker_status:
            body["speaker_status"] = str(speaker_status)
        if known_speakers:
            body["known_speakers"] = [
                str(x).strip() for x in list(known_speakers)[:12] if str(x).strip()
            ]
        response = self._http.post(
            "%s/api/agents/%d/chat/stream" % (self.muse_url, self.agent_id),
            json=body,
            timeout=(5, 120),
            stream=True,
        )
        headers_at = time.perf_counter()
        if metrics is not None:
            metrics["muse_headers_ms"] = round(
                (headers_at - request_started_at) * 1000,
                1,
            )
        if response.status_code == 404:
            response.close()
            yield self.chat(original_text)
            return
        response.raise_for_status()
        try:
            for line in response.iter_lines(decode_unicode=True):
                if cancel_event is not None and cancel_event.is_set():
                    break
                if not line:
                    continue
                event = json.loads(line)
                if event.get("error"):
                    raise RuntimeError(event["error"])
                if event.get("metrics") and metrics is not None:
                    metrics["upstream"] = event["metrics"]
                if event.get("ignored") and metrics is not None:
                    metrics["addressed"] = False
                kind = str(event.get("kind") or "").strip()
                if kind == "tool_wait":
                    yield {
                        "kind": "tool_wait",
                        "tool": str(event.get("tool") or ""),
                    }
                speak = event.get("speak")
                if speak:
                    # 工具垫场/进度：给 TTS 播，不当作成品回复正文
                    speak_kind = kind or "tool_ack"
                    if speak_kind not in ("tool_ack", "tool_progress"):
                        speak_kind = "tool_ack"
                    yield {"kind": speak_kind, "text": str(speak)}
                delta = event.get("delta")
                if delta:
                    yield delta
        finally:
            response.close()

    def chat(self, text, history=None, addressed_hint="conversation_window"):
        response = self._http.post(
            "%s/api/agents/%d/chat" % (self.muse_url, self.agent_id),
            json={
                "message": text,
                "voice_mode": True,
                "history": history or [],
                "addressed_hint": addressed_hint,
            },
            timeout=(5, 120),
        )
        response.raise_for_status()
        return (response.json().get("reply") or "").strip()
