# -*- coding: utf-8 -*-
"""
Muse TTS preview process: reads JSON and invokes the xiaozhi TTS factory.
在 server 目录下、用其 venv 运行，PATH 含 .venv/Scripts（opus.dll）。
"""
import sys
import json
import wave
import asyncio
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[3] / "server"
sys.path.insert(0, str(SERVER_DIR))


def _minimax_preview(block, text, out):
    import requests
    gid, key = block.get("group_id"), block.get("api_key")
    model = block.get("model", "speech-02-turbo")
    voice = block.get("private_voice") or block.get("voice_id", "female-shaonv")
    try:
        speed = float(block.get("speed", 1) or 1)
    except Exception:
        speed = 1.0
    sr = 32000
    d = requests.post("https://api.minimaxi.com/v1/t2a_v2?GroupId=%s" % gid,
                      headers={"Authorization": "Bearer %s" % key, "Content-Type": "application/json"},
                      json={"model": model, "text": text, "stream": False,
                            "voice_setting": {"voice_id": voice, "speed": speed, "vol": 1, "pitch": 0},
                            "audio_setting": {"sample_rate": sr, "bitrate": 128000, "format": "pcm", "channel": 1}},
                      timeout=60).json()
    if d.get("base_resp", {}).get("status_code") != 0:
        raise RuntimeError("MiniMax: %s" % d.get("base_resp"))
    pcm = bytes.fromhex(d["data"]["audio"])
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm)


def main():
    req = json.load(open(sys.argv[1], encoding="utf-8"))
    block, text, out = req["block"], req["text"], req["out"]
    ttype = block.get("type")

    if ttype == "minimax_httpstream":
        try:
            _minimax_preview(block, text, out)
        except Exception as e:
            print("ERR:MiniMax试听失败 " + repr(e)); sys.exit(6)
        print("OK"); return

    from core.utils import tts as tts_factory
    try:
        provider = tts_factory.create_instance(ttype, block, False)
    except Exception as e:
        print("ERR:实例化失败 " + repr(e)); sys.exit(3)
    if ttype == "huoshan_double_stream":
        try:
            import opuslib
            frames = provider.to_tts(text)
            if not frames:
                print("ERR:火山TTS未生成音频"); sys.exit(5)
            decoder = opuslib.Decoder(24000, 1)
            pcm = b"".join(decoder.decode(frame, 1440) for frame in frames)
            with wave.open(out, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(pcm)
        except Exception as e:
            print("ERR:火山TTS试听失败 " + repr(e)); sys.exit(4)
        print("OK"); return
    try:
        asyncio.run(provider.text_to_speak(text, out))
    except Exception as e:
        print("ERR:合成失败 " + repr(e)); sys.exit(4)
    import os
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        print("ERR:未生成音频"); sys.exit(5)
    print("OK")


if __name__ == "__main__":
    main()
