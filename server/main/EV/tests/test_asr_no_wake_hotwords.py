import gzip
import json
from unittest import mock

from speech.asr.doubao_stream import DoubaoStreamingASR


def _initial_payload(asr):
    captured = {}
    asr.socket = mock.Mock()
    asr.socket.send_binary.side_effect = lambda packet: captured.setdefault(
        "packet", packet,
    )
    asr._send_initial_request()
    packet = bytes(captured["packet"])
    return json.loads(gzip.decompress(packet[8:]).decode("utf-8"))


def test_empty_hotwords_do_not_emit_context_or_corpus():
    asr = DoubaoStreamingASR("key", hotwords=[])
    payload = _initial_payload(asr)
    assert "corpus" not in payload["request"]
    assert "context" not in payload["request"]


def test_hotwords_use_request_context_instead_of_corpus():
    asr = DoubaoStreamingASR("key", hotwords=["DeepSeek"])
    payload = _initial_payload(asr)
    context = json.loads(payload["request"]["context"])
    assert context == {"hotwords": [{"word": "DeepSeek"}]}
    assert "corpus" not in payload["request"]


def test_recent_valid_text_becomes_dynamic_domain_context():
    asr = DoubaoStreamingASR("key")
    asr.remember_text("I mean GitHub Trending page")
    payload = _initial_payload(asr)
    words = {
        item["word"]
        for item in json.loads(payload["request"]["context"])["hotwords"]
    }
    assert "GitHub" in words
    assert "Trending" in words
    assert "GitHub Trending" in words
    assert "page" not in words


def test_env_has_no_builtin_wake_hotwords(monkeypatch):
    monkeypatch.delenv("VOLC_ASR_HOTWORDS", raising=False)
    asr = DoubaoStreamingASR.from_env()
    assert asr.hotwords == []


def test_multilingual_config_switches_endpoint_and_sends_language():
    asr = DoubaoStreamingASR(
        "key",
        enable_multilingual="true",
        language="en-US",
        end_window_size="480",
    )
    payload = _initial_payload(asr)

    assert asr.url.endswith("/bigmodel_nostream")
    assert asr.mode == "multilingual_nostream"
    assert payload["audio"]["language"] == "en-US"
    assert payload["request"]["end_window_size"] == 480
    assert "enable_nonstream" not in payload["request"]


def test_bidirectional_mode_does_not_claim_language_support():
    asr = DoubaoStreamingASR(
        "key",
        enable_multilingual="false",
        language="en-US",
    )
    payload = _initial_payload(asr)

    assert asr.url.endswith("/bigmodel_async")
    assert asr.mode == "bilingual_async"
    assert "language" not in payload["audio"]
    assert payload["request"]["enable_nonstream"] is True


def test_agent_overrides_take_effect_even_when_env_provides_credentials(monkeypatch):
    monkeypatch.setenv("VOLC_ASR_API_KEY", "env-key")
    monkeypatch.setenv("VOLC_ASR_RESOURCE_IDS", "env-resource")

    asr = DoubaoStreamingASR.from_env({
        "access_token": "core-access-token",
        "resource_id": "agent-resource",
        "enable_multilingual": "true",
        "language": "en-US",
        "end_window_size": "360",
    })

    assert asr.api_key == "env-key"
    assert asr.resource_ids == ["agent-resource"]
    assert asr.enable_multilingual is True
    assert asr.language == "en-US"
    assert asr.end_window_size == 360
