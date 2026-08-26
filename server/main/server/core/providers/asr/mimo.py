import os
import time
import base64
from typing import Optional, Tuple, List

import requests

from config.logger import setup_logging
from core.providers.asr.dto.dto import InterfaceType
from core.providers.asr.base import ASRProviderBase

TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    """小米 MiMo-V2.5-ASR 云端语音识别（audio-LLM，chat/completions 接口）。

    与设备无关的整句离线识别：VAD 切好的一段语音 -> wav 文件 -> base64
    -> POST /v1/chat/completions (content type input_audio) -> 取回文本。
    打断由 VAD(receiveAudioHandle) 负责，与本 provider 无关。
    """

    def __init__(self, config: dict, delete_audio_file: bool):
        self.interface_type = InterfaceType.NON_STREAM
        self.api_key = config.get("api_key")
        self.api_url = config.get(
            "base_url", "https://api.xiaomimimo.com/v1/chat/completions"
        )
        self.model = config.get("model_name", "mimo-v2.5-asr")
        # 语言：auto / zh / en
        self.language = config.get("language", "auto")
        self.output_dir = config.get("output_dir", "tmp/")
        self.delete_audio_file = delete_audio_file
        self.http_session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2)
        self.http_session.mount("https://", adapter)
        os.makedirs(self.output_dir, exist_ok=True)

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, audio_format="opus", artifacts=None
    ) -> Tuple[Optional[str], Optional[str]]:
        file_path = None
        try:
            if artifacts is None:
                return "", None
            file_path = artifacts.file_path  # base 类已存好 wav

            import io
            import wave

            wav = io.BytesIO()
            with wave.open(wav, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(artifacts.pcm_bytes)
            audio_b64 = base64.b64encode(wav.getvalue()).decode("utf-8")

            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": "data:audio/wav;base64," + audio_b64
                                },
                            }
                        ],
                    }
                ],
                "asr_options": {"language": self.language},
            }
            headers = {
                "api-key": self.api_key,
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            start_time = time.time()
            response = self.http_session.post(
                self.api_url, json=payload, headers=headers, timeout=30
            )
            logger.bind(tag=TAG).debug(
                f"MiMo ASR 耗时: {time.time() - start_time:.3f}s | {response.status_code}"
            )

            if response.status_code == 200:
                text = (
                    response.json()
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    or ""
                )
                return text.strip(), file_path
            raise Exception(f"MiMo ASR 请求失败: {response.status_code} - {response.text}")
        except Exception as e:
            logger.bind(tag=TAG).error(f"MiMo ASR 识别失败: {e}")
            return "", None

    async def close(self):
        self.http_session.close()
