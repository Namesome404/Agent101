import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__


async def handleAbortMessage(conn: "ConnectionHandler"):
    conn.logger.bind(tag=TAG).info("Abort message received")
    # 设置成打断状态，会自动打断llm、tts任务
    conn.close_after_chat = False
    conn.client_abort = True
    conn.clear_queues()
    # 打断客户端说话状态
    stop_msg = {"type": "tts", "state": "stop", "session_id": conn.session_id}
    await conn.websocket.send(json.dumps(stop_msg))
    try:
        from core.handle.sendAudioHandle import _broadcast_muse_tts

        await _broadcast_muse_tts(conn, stop_msg)
    except Exception:
        pass
    conn.clearSpeakStatus()
    conn.logger.bind(tag=TAG).info("Abort message received-end")
