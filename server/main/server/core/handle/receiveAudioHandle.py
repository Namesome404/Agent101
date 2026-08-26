import time
import json
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.util import audio_to_data
from core.handle.abortHandle import handleAbortMessage
from core.handle.intentHandler import handle_user_intent
from core.utils.output_counter import check_device_output_limit
from core.handle.sendAudioHandle import send_stt_message, SentenceType

TAG = __name__

# 远程麦打断：需要连续高能量人声帧（约 60ms/帧）才确认，避免外放回声误打断
REMOTE_BARGE_IN_FRAMES = 8  # ~480ms
REMOTE_BARGE_IN_RMS = 1200  # int16 PCM，贴脸说话通常远高于房间回声


def _opus_rms(conn: "ConnectionHandler", opus_packet: bytes) -> float:
    """解码一帧 Opus 并算 RMS，失败返回 0。"""
    try:
        import numpy as np
        import opuslib_next

        decoder = getattr(conn, "_barge_opus_decoder", None)
        if decoder is None:
            decoder = opuslib_next.Decoder(16000, 1)
            conn._barge_opus_decoder = decoder
        pcm = decoder.decode(opus_packet, 960)
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples * samples)))
    except Exception:
        return 0.0


def _remote_barge_in_hit(conn: "ConnectionHandler", audio: bytes, have_voice: bool) -> bool:
    """远程麦：够响且持续够久才算真打断。"""
    if not have_voice:
        conn.remote_barge_frames = 0
        return False
    rms = _opus_rms(conn, audio)
    if rms < REMOTE_BARGE_IN_RMS:
        conn.remote_barge_frames = 0
        return False
    conn.remote_barge_frames = getattr(conn, "remote_barge_frames", 0) + 1
    return conn.remote_barge_frames >= REMOTE_BARGE_IN_FRAMES


async def handleAudioMessage(conn: "ConnectionHandler", audio):
    # TTS 刚结束后的短冷却：丢掉房间里残留的扬声器尾音
    if (
        not getattr(conn, "client_is_speaking", False)
        and time.time() < getattr(conn, "asr_cooldown_until", 0)
    ):
        return

    remote_mic = False
    try:
        from core.muse_session_hub import has_active_remotes, parse_muse_agent_id

        agent_id = parse_muse_agent_id(conn.device_id)
        remote_mic = bool(agent_id and has_active_remotes(agent_id))
    except Exception:
        remote_mic = False

    # 当前片段是否有人说话
    have_voice = conn.vad.is_vad(conn, audio)

    # TTS 播放中：绝不把回声送进 ASR；打断规则分远程/本机
    if getattr(conn, "client_is_speaking", False):
        if conn.client_listen_mode == "manual" or getattr(conn, "client_abort", False):
            return
        allow_abort = False
        if remote_mic:
            # 远程麦：高能量 + 持续约 0.5s 才打断（回声通常不够响/不够稳）
            allow_abort = _remote_barge_in_hit(conn, audio, have_voice)
        else:
            allow_abort = have_voice
        if allow_abort:
            conn.remote_barge_frames = 0
            await handleAbortMessage(conn)
            conn.reset_audio_states()
            await conn.asr.receive_audio(conn, audio, True)
        return

    conn.remote_barge_frames = 0

    # 如果设备刚刚被唤醒，短暂忽略VAD检测
    if hasattr(conn, "just_woken_up") and conn.just_woken_up:
        have_voice = False
        # 设置一个短暂延迟后恢复VAD检测
        if not hasattr(conn, "vad_resume_task") or conn.vad_resume_task.done():
            conn.vad_resume_task = asyncio.create_task(resume_vad_detection(conn))
        return
    # 设备长时间空闲检测，用于say goodbye
    await no_voice_close_connect(conn, have_voice)
    # 接收音频
    await conn.asr.receive_audio(conn, audio, have_voice)


async def resume_vad_detection(conn: "ConnectionHandler"):
    # 等待2秒后恢复VAD检测
    await asyncio.sleep(2)
    conn.just_woken_up = False


async def startToChat(conn: "ConnectionHandler", text):
    # 检查输入是否是JSON格式（包含说话人信息）
    speaker_name = None
    language_tag = None
    actual_text = text

    try:
        # 尝试解析JSON格式的输入
        if text.strip().startswith("{") and text.strip().endswith("}"):
            data = json.loads(text)
            if "speaker" in data and "content" in data:
                speaker_name = data["speaker"]
                language_tag = data["language"]
                actual_text = data["content"]
                conn.logger.bind(tag=TAG).info(f"解析到说话人信息: {speaker_name}")

                # 直接使用JSON格式的文本，不解析
                actual_text = text
    except (json.JSONDecodeError, KeyError):
        # 如果解析失败，继续使用原始文本
        pass

    # 保存说话人信息到连接对象
    if speaker_name:
        conn.current_speaker = speaker_name
    else:
        conn.current_speaker = None

    if conn.need_bind:
        await check_bind_device(conn)
        return

    # 如果当日的输出字数大于限定的字数
    if conn.max_output_size > 0:
        if check_device_output_limit(
            conn.headers.get("device-id"), conn.max_output_size
        ):
            await max_out_size(conn)
            return

    # manual 模式下不打断正在播放的内容
    if conn.client_is_speaking and conn.client_listen_mode != "manual":
        await handleAbortMessage(conn)

    # 首先进行意图分析，使用实际文本内容
    intent_handled = await handle_user_intent(conn, actual_text)

    if intent_handled:
        # 如果意图已被处理，不再进行聊天
        return

    # 意图未被处理，继续常规聊天流程，使用实际文本内容
    await send_stt_message(conn, actual_text)

    # 准备开始新会话
    conn.client_abort = False

    conn.executor.submit(conn.chat, actual_text)


async def no_voice_close_connect(conn: "ConnectionHandler", have_voice):
    if have_voice:
        conn.last_activity_time = time.time() * 1000
        return
    if str(getattr(conn, "device_id", "") or "").startswith("muse:"):
        return
    # 只有在已经初始化过时间戳的情况下才进行超时检查
    if conn.last_activity_time > 0.0:
        no_voice_time = time.time() * 1000 - conn.last_activity_time
        close_connection_no_voice_time = int(
            conn.config.get("close_connection_no_voice_time", 120)
        )
        if (
            not conn.close_after_chat
            and no_voice_time > 1000 * close_connection_no_voice_time
        ):
            conn.close_after_chat = True
            conn.client_abort = False
            end_prompt = conn.config.get("end_prompt", {})
            if end_prompt and end_prompt.get("enable", True) is False:
                conn.logger.bind(tag=TAG).info("结束对话，无需发送结束提示语")
                await conn.close()
                return
            prompt = end_prompt.get("prompt")
            if not prompt:
                prompt = "请你以```时间过得真快```未来头，用富有感情、依依不舍的话来结束这场对话吧。！"
            await startToChat(conn, prompt)


async def max_out_size(conn: "ConnectionHandler"):
    # 播放超出最大输出字数的提示
    conn.client_abort = False
    text = "不好意思，我现在有点事情要忙，明天这个时候我们再聊，约好了哦！明天不见不散，拜拜！"
    await send_stt_message(conn, text)
    file_path = "config/assets/max_output_size.wav"
    opus_packets = await audio_to_data(file_path)
    conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
    conn.close_after_chat = True


async def check_bind_device(conn: "ConnectionHandler"):
    if conn.bind_code:
        # 确保bind_code是6位数字
        if len(conn.bind_code) != 6:
            conn.logger.bind(tag=TAG).error(f"无效的绑定码格式: {conn.bind_code}")
            text = "绑定码格式错误，请检查配置。"
            await send_stt_message(conn, text)
            return

        text = f"请登录控制面板，输入{conn.bind_code}，绑定设备。"
        await send_stt_message(conn, text)

        # 播放提示音
        music_path = "config/assets/bind_code.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.FIRST, opus_packets, text))

        # 逐个播放数字
        for i in range(6):  # 确保只播放6位数字
            try:
                digit = conn.bind_code[i]
                num_path = f"config/assets/bind_code/{digit}.wav"
                num_packets = await audio_to_data(num_path)
                conn.tts.tts_audio_queue.put((SentenceType.MIDDLE, num_packets, None))
            except Exception as e:
                conn.logger.bind(tag=TAG).error(f"播放数字音频失败: {e}")
                continue
        conn.tts.tts_audio_queue.put((SentenceType.LAST, [], None))
    else:
        # 播放未绑定提示
        conn.client_abort = False
        text = f"没有找到该设备的版本信息，请正确配置 OTA地址，然后重新编译固件。"
        await send_stt_message(conn, text)
        music_path = "config/assets/bind_not_found.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
