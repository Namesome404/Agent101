# -*- coding: utf-8 -*-
"""窗口记忆 + 记录模式 + 反幻觉提示。

从 surfaces.py 拆出的提示注入层。供 app.py 注入 voice 系统提示：
- memory_hint()：当前窗口记忆快照。
- truth_system()：通用反幻觉铁律（回执唯一真相）。
- record_mode_hint(aid) / pending_input_*：surface_expect_input 记录模式状态。
- is_pure_info() / unbacked_completion()：已停用的正则拦截（不再使用）。
"""
from __future__ import annotations

import copy

from devices.coding.scene_store import scene_store
from devices.coding.surface_tools import (
    _PENDING_INPUT,
    _PENDING_LOCK,
)
from devices.coding.turn_trace import read_recent_executions as _read_recent_executions


# ==================== 记录模式（surface_expect_input） ====================
# surface_expect_input start 开启「记录模式」：系统只记住目标窗口，不吞话。
# 之后每句发言仍正常走 LLM——由模型自己判定该不该 append、要不要停止记录。
# 用户说「别记了/关掉/删除」就放行给模型处理，不做硬编码正则拦截。
def record_mode_hint(aid) -> str:
    """记录模式进行中时，返回注入给 LLM 的系统提示；未开启返回空串。

    提示模型：用户之前要求把接下来要说的话记到窗口。由模型自己判定——
    本条是内容（待办/想法/一句话/信息）就通过 object_control invoke/append 写入目标窗口；
    用户是在操作窗口、想停止记录、提问或纯闲聊就正常处理、不 append。
    """
    with _PENDING_LOCK:
        pending = _PENDING_INPUT.get(aid)
    if not pending:
        return ""
    surface_id = pending.get("surface_id") or ""
    path = pending.get("path") or "/content/items"
    return (
        "【记录模式】用户之前让你把接下来要说的话记到窗口，当前仍在记录中。"
        "由你判断本条发言：如果是要记录的内容（待办/想法/一句话/信息），"
        "就调用 object_control（op=invoke,target=surface.%s,command=append）写入；"
        "如果用户是在操作窗口（关/删/改/挪）、想停止记录、提问或纯闲聊，"
        "就不要 append，按正常流程处理。用户明确说「别记了/停止记录」或"
        "关闭该窗口后，调用 object_control（同一 target，command=record_stop）结束记录。"
        % surface_id
    )


def pending_input_snapshot(aid):
    with _PENDING_LOCK:
        return copy.deepcopy(_PENDING_INPUT.get(aid))


def pending_input_ack(aid, ok: bool):
    """执行完一次捕获后递减剩余；归零则移除。返回是否仍在等待。"""
    with _PENDING_LOCK:
        active = _PENDING_INPUT.get(aid)
        if not active:
            return False
        active["remaining"] = int(active.get("remaining") or 1) - 1
        if active["remaining"] <= 0:
            _PENDING_INPUT.pop(aid, None)
            return False
        return True


# ==================== 窗口记忆 + 反幻觉提示 ====================
def memory_hint() -> str:
    """当前窗口记忆：每次窗口操作/查询后自动反映真实状态，注入 voice 系统提示。

    模型改窗前先对照这份记忆，不臆测窗口是否存在/几何；相对调整（更宽/更大）
    仍需 object_control inspect 查当前 bounds/rev 再用 apply。记忆不含完整 HTML/CSS，
    只给能支撑决策的摘要，避免 token 膨胀破坏前缀缓存。

    不再截断窗口列表：可见/聚焦窗口排最前并全部列出（scene_store 已有
    SURFACE_LIMIT=20 上限淘汰，历史残留不会无限堆积）。曾因 surfaces[:6]
    截断把当前正用的 YouTube 挤出记忆，模型「挪 YouTube」时只能瞎查别的窗。
    """
    try:
        snapshot = scene_store.inspect(scope="all")
    except Exception:
        return ""
    surfaces = snapshot.get("surfaces") or []
    if not surfaces:
        return (
            "【当前窗口记忆】现在没有任何窗口。用户要求打开/新建窗口时调用 "
            "object_control（op=invoke,target=surface.new,command=create）；"
            "用户要求关闭/删除窗口时也必须调用 object_control 并等 ok:true 回执，"
            "不能只嘴上说关。"
        )
    # 可见/聚焦优先，其余按 scene 顺序；全部列出不截断（上限由 store 保证）。
    surfaces = sorted(
        surfaces,
        key=lambda item: (
            0 if item.get("visible") and item.get("focused") else
            1 if item.get("visible") else 2,
        ),
    )
    lines = []
    for item in surfaces:
        surface_id = str(item.get("id") or "")
        title = str((item.get("data") or {}).get("title") or "") or surface_id
        content = (item.get("data") or {}).get("content") or {}
        content_type = str(content.get("type") or "") or ""
        visible = "是" if item.get("visible") else "否"
        focused = "是" if item.get("focused") else "否"
        bounds = item.get("bounds")
        bounds_txt = ""
        if isinstance(bounds, dict):
            bounds_txt = ",".join(
                "%s=%s" % (k, v) for k, v in bounds.items() if k in ("width", "height", "x", "y")
            )
        extra = ""
        try:
            from devices.coding.surface_tools import is_pinned_surface
            if is_pinned_surface(surface_id):
                last_query = str((item.get("data") or {}).get("meta", {}).get("last_query") or "")
                if last_query:
                    extra = " | 最近内容=搜索:%s" % last_query[:30]
        except Exception:
            pass
        object_target = (
            "agent.ui.status"
            if surface_id == "status-timeline"
            else "surface.%s" % surface_id
        )
        if surface_id == "status-timeline":
            extra += " | owner=assistant（助手自己/你自己指这个对象）"
        lines.append(
            "- target=%s | legacy_id=%s | title=%s | visible=%s | focused=%s%s%s%s%s"
            % (
                object_target,
                surface_id,
                title[:40],
                visible,
                focused,
                (" | bounds(%s)" % bounds_txt) if bounds_txt else "",
                (" | type=%s" % content_type) if content_type else "",
                (" | 渲染中/异常" if str(item.get("content_status") or "") in ("loading", "error") else ""),
                extra,
            )
        )
    return (
        "【当前窗口记忆】（每次窗口操作后自动更新，基于此记忆说话，不要臆测）\n"
        + "\n".join(lines)
        + "\n注意：visible=否 只代表系统记录里不显示，实际屏幕上的窗口可能还在，"
        "判断以用户当前看到的为准。打开新窗口用 object_control invoke target=surface.new,"
        "command=create；已有窗口的显示、隐藏、删除也统一用 object_control 并等 ok:true 回执。"
        "同一个窗口即使刚更新过，用户再要求关闭仍是一次新的 invoke hide 动作，必须重新执行。"
        "相对调整先 object_control inspect 读取 bounds/rev，再 apply 提交明确 window 补丁；"
        "最终位置与尺寸只认回执。"
        "以上罗列的窗口只是当前【现状背景】，不代表这轮就要操作它们；要不要动窗口，"
        "以用户最新这一句为准。"
        "用户只是在告诉你信息（名字/颜色/内容/要求）或闲聊时，不涉及窗口操作，"
        "直接回应即可，不要调用 object_control 修改窗口。"
        + _search_results_hint()
    )


def search_results_hint() -> str:
    """最近搜索结果的可打开清单（公开入口，世界快照直接取用）。"""
    return _search_results_hint()


def _search_results_hint() -> str:
    """把最近搜索结果列成可打开对象，杜绝模型凭记忆编链接。

    真实事故：用户说「把那个链接打开」，模型手里没有 URL（弱证据时系统有意
    扣下），于是编了一个 B 站 BV 号——放出来是 rickroll。结果本身一直在服务端，
    这里只告诉模型「它们叫 result.N、可以直接 open」，URL 依旧不给它。
    """
    try:
        from control_plane import search_results
    except Exception:
        return ""
    snapshot = search_results.snapshot()
    items = snapshot.get("items") or []
    if not items:
        return ""
    lines = [
        "\n【最近搜索结果】用户说「打开那个/第几条/那个视频」时，"
        "用 object_control invoke target=result.N command=open 打开，"
        "N 是下面的序号。绝不要自己写 URL——你没有这些链接，凭记忆写必然是错的。",
    ]
    for index, item in enumerate(items, 1):
        lines.append(
            "- result.%d | %s | 来源 %s"
            % (index, str(item.get("title") or "")[:60], item.get("site") or "未知")
        )
    return "\n".join(lines)


def truth_system() -> str:
    """语音通用反幻觉：锚在回执上，短、不抢话题。

    以前这里靠一串「禁止…禁止…绝不可以…」去防模型编造完成状态——防的是
    「它不知道动作的真实结果」。现在每个变更回执都带 after（和【世界现状】
    同一套人话），结果直接摆在它面前，禁令就可以收敛成一条：复述 after。
    """
    return (
        "【事实铁律】回执是唯一真相。动作必须当场调工具、拿到 ok:true 才算做成，"
        "没有回执=没做，只能说自己还没做。回执里的 after 就是此刻的真实状态，"
        "播报直接依据它说，别自己组织「已完成」的说法，也别凭记忆说做过。"
        "失败就直说失败/查不到，不编数字、链接、文件，不开空头支票。"
        "用户每次提出的要求都是独立动作，即使和之前做过的一模一样也要当场重新调用。"
        "闲聊/打招呼正常回，不要主动扯窗口或写码进度。"
    )


# ==================== 光说不做拦截 ====================
# 已删除：正则核对模型声称的完成（_VOICE_CLAIMED_DONE_RE 等）既违背
# 「不用正则控制」原则，也实测无效（拦截上百次模型照样幻觉）。
# 防幻觉完全交给提示词铁律（truth_system + voice 主提示）：
# 模型必须当场调工具拿 ok:true 回执，没有回执不许说做了。


def is_pure_info(user_text: str) -> bool:
    """已停用：纯信息判断交给模型自己，始终返回 False（不抑制任何工具）。"""
    return False


def unbacked_completion(user_text, assistant_text, trace_id="",
                        has_mutation_receipt=False, record_mode=False,
                        model_intent=None):
    """已停用：正则核对模型完成声称的方案已废弃（违背不用正则原则且无效）。

    保留签名仅为兼容旧引用，始终返回 False——不再注入任何纠正轮。
    防幻觉交给 truth_system() 与 voice 主提示里的回执铁律。
    """
    return False


def execution_check_message(record_mode=False) -> str:
    """已停用：随 unbacked_completion 一并废弃，保留为空串。"""
    return ""
