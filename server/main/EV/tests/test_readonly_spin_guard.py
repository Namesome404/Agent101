# -*- coding: utf-8 -*-
"""空转保护的只读判定：必须按「函数名+参数」，不能按旧函数名。

背景：web_search/surface_inspect 等降级成参数，canvas_control 则按 action 判定。
_READONLY_ACTION_NAMES 仍按旧名匹配，导致搜索永远不算只读、空转保护失效——
实测用户问一句，agent 连搜 11 次都没被拦，最后撞硬上限甩出一句收尾语。
"""
import app


def test_task_control_search_is_readonly():
    """搜索/读网页/查时间天气都是只读，连续做就该判空转。"""
    for kind in ("web_search", "web_extract", "current_time", "date", "weather"):
        assert app._is_readonly_call("task_control", {"kind": kind}) is True, kind


def test_task_control_real_actions_are_not_readonly():
    """写码类任务会改变外部状态，不算只读。"""
    for kind in ("coding_plan", "coding_cancel", "coding_revert"):
        assert app._is_readonly_call("task_control", {"kind": kind}) is False, kind


def test_surface_and_device_readonly_by_action():
    assert app._is_readonly_call("surface_control", {"action": "status"}) is True
    assert app._is_readonly_call("surface_control", {"action": "close"}) is False
    assert app._is_readonly_call("surface_control", {"action": "create"}) is False
    assert app._is_readonly_call("device_control", {"action": "status"}) is True
    assert app._is_readonly_call("device_control", {"action": "power"}) is False


def test_conversation_reply_is_readonly():
    assert app._is_readonly_call("conversation_reply", {"mode": "answer"}) is True


def test_canvas_inspect_is_readonly_but_apply_is_a_mutation():
    assert app._is_readonly_call("canvas_control", {"action": "inspect"}) is True
    assert app._is_readonly_call("canvas_control", {"action": "apply"}) is False


def test_object_inspect_is_readonly_but_apply_and_invoke_are_mutations():
    assert app._is_readonly_call("object_control", {"op": "inspect"}) is True
    assert app._is_readonly_call("object_control", {"op": "apply"}) is False
    assert app._is_readonly_call("object_control", {"op": "invoke"}) is False


def test_unknown_tool_defaults_to_not_readonly():
    """未知工具按非只读处理：宁可放过一轮，也不要把真实动作误判成空转。"""
    assert app._is_readonly_call("something_new", {}) is False
    assert app._is_readonly_call("", {}) is False


def test_repeated_search_round_is_detected_as_spin():
    """连续多次搜索 → 每次都只读 → all() 成立 → 触发空转累计（本次修复的核心）。"""
    flags = [app._is_readonly_call("task_control", {"kind": "web_search"}) for _ in range(3)]
    assert flags and all(flags)


def test_readonly_names_no_longer_match_actual_tool_names():
    """回归护栏：真实函数名不该出现在旧名单里，否则说明又漂回按名匹配了。"""
    actual = {"task_control", "object_control"}
    assert not (actual & set(app._READONLY_ACTION_NAMES))


def test_terminal_search_batch_closes_retrieval_phase():
    batch = [{
        "action": "task_control",
        "args": {"kind": "web_search", "continue_after": False},
    }]
    assert app._batch_closes_readonly_phase(batch) is True


def test_explicit_continuation_keeps_retrieval_available():
    batch = [{
        "action": "task_control",
        "args": {
            "kind": "web_search",
            "continue_after": True,
            "post_search_goal": "聚焦第一张图片并全屏",
        },
    }]
    assert app._batch_closes_readonly_phase(batch) is False


def test_search_display_alone_cannot_create_a_fake_second_step():
    batch = [{
        "action": "task_control",
        "args": {"kind": "web_search", "continue_after": True},
    }]
    assert app._batch_requests_continuation(batch) is False
    assert app._batch_closes_readonly_phase(batch) is True


def test_mutation_and_answer_do_not_trigger_readonly_terminal_guard():
    mutation = [{
        "action": "object_control",
        "args": {
            "op": "invoke", "target": "surface.new", "command": "create",
            "continue_after": False,
        },
    }]
    answer = [{
        "action": "conversation_reply",
        "args": {"mode": "answer", "reply": "可以"},
    }]
    assert app._batch_closes_readonly_phase(mutation) is False
    assert app._batch_closes_readonly_phase(answer) is False


def test_forced_answer_round_omits_tools_and_tool_choice():
    """收尾轮真的撤掉工具，而不是靠提示约束模型。

    试过改成「保留 tools + tool_choice=none」以保住前缀缓存，实测两种做法
    命中率一样（搜索轮都是 2688 token），故保留行为保证更强的这一版。
    """
    assert app._tool_request_kwargs(None) == {}
    assert app._answer_only_tools([
        {"function": {"name": "conversation_reply"}},
        {"function": {"name": "task_control"}},
    ]) is None
    kwargs = app._tool_request_kwargs([{"function": {"name": "conversation_reply"}}])
    assert kwargs["tool_choice"] == "auto"   # 不动手是默认路径
    assert len(kwargs["tools"]) == 1


def test_object_lookup_does_not_close_the_action_phase():
    """查目标 ≠ 查资料：inspect 的意义就是「先找到 target 再动手」。

    真实事故：「把之前那个 GitHub 窗口显示出来」——模型 inspect 找到了
    surface.web-github-com，但那一轮被判为只读收尾，下一轮工具被全部收走，
    它再也调不出 show，最后蹦出一句「资料拿到了，但我这次没整理出可靠结论」。
    """
    import app

    lookup = [{
        "action": "object_control",
        "args": {"op": "inspect", "selector": {"query": "github"}, "continue_after": False},
    }]
    assert app._batch_closes_readonly_phase(lookup) is False

    # 搜索仍然是阶段终点：搜完就该作答，不许再起一轮检索
    search = [{
        "action": "task_control",
        "args": {"kind": "web_search", "request": "开源机械臂", "continue_after": False},
    }]
    assert app._batch_closes_readonly_phase(search) is True


def test_unbacked_claim_about_a_real_object_is_detected():
    """说了话却一个工具没调、还提到真实对象——必须先核对再开口。

    真实事故：tool_choice 放开后，「把哔哩哔哩窗口关上」这一轮模型直接答
    「Bilibili已关闭」，一个工具都没调，窗口还开着；下一句还加码说
    「已经关上了，不用再关」。required 挡不住这个（它选 conversation_reply
    一样能编），唯一的保证是拿回执核对。

    判据来自运行时对象目录里的名字，不是手写的语言模式。
    """
    import app
    from devices.iot import iot_registry
    from tools import device_control

    device_control.ensure_builtin_devices()
    assert app._mentions_live_object("桌面灯带亮度调到30%了") is True
    assert app._mentions_live_object("行，到点我喊你。") is False
    assert app._mentions_live_object("") is False

    # 新注册的对象自动进入判据，不需要改这里
    iot_registry.register(
        "claim-probe", name="核对探针灯", kind="light",
        capabilities=("status",), executor=lambda a, b: ("", {"ok": True}),
    )
    try:
        assert app._mentions_live_object("核对探针灯已经关了") is True
    finally:
        iot_registry.unregister("claim-probe")


def test_history_notes_the_action_without_replaying_the_call(tmp_path, monkeypatch):
    """历史里补回「当时真调过工具」这个事实，但不贴回调用本身。

    客户端传的历史只有台词，模型据此学会「说一句完成即可」（「Bilibili已关闭」
    而窗口还开着）。试过把真实 tool_calls 原样贴回去，两次翻车：先把错误的
    「DJX 价格」检索抄到「把哔哩哔哩关上」「计时35分钟」上；只回放动作之后，
    又把「inspect 哔哩哔哩 + show」抄到「和我闲聊」「零件还没到齐呢」这种纯
    闲聊上，而且每抄一次落盘一次，越滚越大。

    调用是可复制的模板，一句中文陈述不是。纸条版同样治好了说谎（开窗关窗
    6/6 正确），代价小得多。
    """
    import app

    monkeypatch.setattr(app, "_turn_acts_path", lambda: tmp_path / "turn_acts.json")
    app._TURN_ACTS.clear()
    app._TURN_ACTS_LOADED = False

    app._remember_turn_messages(1, "把哔哩哔哩窗口关上。", [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "object_control", "arguments": '{"op":"invoke"}'},
        }]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"ok":true}'},
        {"role": "assistant", "content": "窗口已关闭"},
    ])

    history = [
        {"role": "user", "content": "把哔哩哔哩窗口关上。"},
        {"role": "assistant", "content": "窗口已关闭"},
    ]
    noted = app._replay_recorded_turns(1, history)
    assert [m["role"] for m in noted] == ["user", "assistant", "system"]
    assert "object_control" in noted[2]["content"] and "ok:true" in noted[2]["content"]
    # 助手台词一个字不改（塞进台词里模型会照着念出来）
    assert noted[1] == history[1]
    # 历史里不再出现可被复制的调用模板
    assert not any(m.get("tool_calls") for m in noted)

    # 没记录的轮次原样保留
    plain = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好"}]
    assert app._replay_recorded_turns(1, plain) == plain

def test_receipt_is_compacted_before_it_goes_back_into_context():
    """回执原样贴回去会把提示词撑爆（inspect 回执有 1KB 出头）。"""
    import app

    fat = {"ok": True, "op": "invoke", "target_id": "surface.x", "after": "visible=否",
           "detail": "x" * 3000, "objects": [{"junk": "y" * 500}]}
    compact = app._compact_tool_content(__import__("json").dumps(fat))
    assert len(compact) <= 220
    assert '"ok": true' in compact and "visible=否" in compact
    assert "junk" not in compact



def test_search_calls_are_not_replayed_into_history(tmp_path, monkeypatch):
    """只回放动作，不回放查询。

    真实事故：模型对着「把哔哩哔哩关上」「计时35分钟」去搜「DJX 价格」，
    这些错误调用被当成示范贴回历史，下一轮照抄——自我强化的污染环。
    动作调用带着明确 target 和回执，抄错了立刻看得出来；检索调用只是一串
    自由文本，最容易被跨话题复制。
    """
    import app

    monkeypatch.setattr(app, "_turn_acts_path", lambda: tmp_path / "turn_acts.json")
    app._TURN_ACTS.clear()
    app._TURN_ACTS_LOADED = False

    # 动作轮：照常记录
    app._remember_turn_messages(1, "把灯关掉", [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "object_control", "arguments": '{"op":"invoke"}'},
        }]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"ok":true}'},
        {"role": "assistant", "content": "灯关了"},
    ])
    noted = app._replay_recorded_turns(1, [
        {"role": "user", "content": "把灯关掉"},
        {"role": "assistant", "content": "灯关了"},
    ])
    # 补的是一句纸条，不是可被复制的调用模板
    assert any(m.get("role") == "system" and "ok:true" in m["content"] for m in noted)
    assert not any(m.get("tool_calls") for m in noted)

    # 检索轮：调用不进历史（由调用方过滤，这里验证过滤判据本身）
    search_call = {
        "id": "c2", "type": "function",
        "function": {"name": "task_control",
                     "arguments": '{"kind":"web_search","request":"DJX 价格"}'},
    }
    action_call = {
        "id": "c3", "type": "function",
        "function": {"name": "object_control", "arguments": '{"op":"invoke"}'},
    }
    import json as _json

    def replayable(call):
        fn = call["function"]
        if fn["name"] != "task_control":
            return True
        return _json.loads(fn["arguments"]).get("kind") not in ("web_search", "web_extract")

    assert replayable(action_call) is True
    assert replayable(search_call) is False


def test_repeated_readonly_lookup_is_not_executed_again():
    """同一次查询重复了就别再打一遍：结果已经在上面。

    真实事故：ASR 只听到半句「呼吸的」，模型连着调了 6 次一模一样的
    inspect(query=哔哩哔哩)，7 秒后放弃并蹦出兜底话。原先「inspect 关闭
    动作阶段」那道刹车为了让 inspect→show 两步走通被摘掉了，而已有的去重
    只管成功的变更动作，只读查询完全没人管。
    """
    import app

    lookup = {
        "action": "object_control",
        "args": {"op": "inspect", "selector": {"query": "哔哩哔哩"}},
    }
    same = {
        "action": "object_control",
        "args": {"op": "inspect", "selector": {"query": "哔哩哔哩"}},
    }
    other = {
        "action": "object_control",
        "args": {"op": "inspect", "selector": {"query": "计时器"}},
    }
    assert app._is_readonly_call(lookup["action"], lookup["args"]) is True
    # 结构化动作键相同 → 认得出是同一次查询；不同查询不误伤
    assert app._transaction_action_key(lookup) == app._transaction_action_key(same)
    assert app._transaction_action_key(lookup) != app._transaction_action_key(other)


def test_answer_retry_is_one_decision_with_a_reason_code():
    """回炉判定合成一处：五个各写各的 if 会互相打架。

    原先协议残留、光说不做、反问代替动作、空输出、证据不足各有一个
    xxx_retries 计数器和一段几乎一样的 continue，彼此不知道对方存在——
    一轮里连触两个就会多加一次 action_round。
    """
    import app
    from tools import device_control

    device_control.ensure_builtin_devices()

    # 空输出最优先
    reason, text = app._voice_answer_retry(
        text="", had_tool_call=False, had_mutation_receipt=False,
        search_result=None, constrained_empty=False,
    )
    assert reason == "empty_output" and "自然中文" in text

    # 证据不足排在「光说不做」之前：先纠事实，再纠动作
    reason, _ = app._voice_answer_retry(
        text="桌面灯带是红色的。", had_tool_call=True, had_mutation_receipt=False,
        search_result={"evidence_quality": "weak"}, constrained_empty=True,
    )
    assert reason == "weak_evidence"

    # 说了动作却没有变更回执，且话里提到真实对象
    reason, _ = app._voice_answer_retry(
        text="桌面灯带已经关了。", had_tool_call=False, had_mutation_receipt=False,
        search_result=None, constrained_empty=False,
    )
    assert reason == "unbacked_claim"

    # 以问号结尾不再回炉。曾有一条 clarify_instead_of_act 判「反问代替动作」，
    # 实测 80 轮触发 9 次只中 1 次，另外 8 次全是纯闲聊（「你好」「可以和我用
    # 英文沟通吗？」「Hi, Vivian.」），回炉后模型也没去调工具。问号不能当
    # 「该动手」的证据，8 次误伤各白烧一个 LLM 来回（中位 1.46 秒）。
    assert app._voice_answer_retry(
        text="你是想问接口还是产品？", had_tool_call=False, had_mutation_receipt=False,
        search_result=None, constrained_empty=False,
    ) == ("", "")

    # 正常回答不回炉
    assert app._voice_answer_retry(
        text="行，到点我喊你。", had_tool_call=False, had_mutation_receipt=False,
        search_result=None, constrained_empty=False,
    ) == ("", "")

    # 真做过动作的播报不回炉
    assert app._voice_answer_retry(
        text="桌面灯带关了。", had_tool_call=True, had_mutation_receipt=True,
        search_result=None, constrained_empty=False,
    ) == ("", "")


def test_unbacked_claim_also_catches_device_values_and_forces_a_tool():
    """判据要认对象的「值」，不只是对象名。

    真实事故：用户三次说「把音频切到 X」，两次模型一个工具都没调就说
    「输出切回 AirPods Pro 了」。其中一句被拦下了（话里有「扬声器」，是
    agent.audio 的别名），另一句没有——「AirPods Pro」是对象的当前值，
    不是对象名，判据只看名字就漏了。值同样来自 registry，是数据不是语义。

    而且被拦下的那次，软提醒不够：模型重说时又说了同一句。所以回炉那一轮
    强制它必须产出结构化出口（真去调工具，或明确选 conversation_reply）。
    """
    import app
    from control_plane.object_registry import object_registry
    from tools import device_control, object_control

    device_control.ensure_builtin_devices()
    object_control.ensure_builtin_provider()

    assert app._mentions_live_object("输出切回电脑扬声器了。") is True   # 别名

    # 「当前值」要从运行时读，不能把 AirPods Pro 写死：这台机器插没插耳机
    # 决定了 agent.audio 的 output 是什么，写死的话只有耳机连着时才通过，
    # 换台机器或 CI 上必挂——测的是判据认不认值，不是这里有没有 AirPods。
    output = ""
    for item in object_registry.world():
        if item.get("target_id") == "agent.audio":
            output = str((item.get("state") or {}).get("output") or "")
            break
    assert output, "agent.audio 没有报告当前输出设备"
    assert app._mentions_live_object("输出切回 %s 了。" % output) is True

    assert app._mentions_live_object("行，到点我喊你。") is False

    reason, _ = app._voice_answer_retry(
        text="输出切回 %s 了。" % output, had_tool_call=False,
        had_mutation_receipt=False, search_result=None, constrained_empty=False,
    )
    assert reason == "unbacked_claim"


def test_claiming_an_action_without_calling_anything_is_caught():
    """零调用却说「做完了」要回炉——哪怕说的东西不在对象目录里。

    真实事故（trace 15:14:11 与 15:14:24）：「打开维基百科」，模型一个工具都没调，
    直接说「维基百科打开了」；下一句「重新打开维基百科」照样零调用。
    _mentions_live_object 拦不住：「维基百科」既不是对象名，也不在当前标签页里。

    对照实验证明这不是 MCP 带来的——同一段历史下，退回 url 窗口那条老路一样
    2/3 零调用。变量是历史：空历史 0/6，带上「连续几次成功动作」的真实历史就塌。
    加上这条判据之后同一组条件 3/3 → 0/3。
    """
    import app

    for text in ["维基百科打开了。", "维基百科重新打开了。", "斯坦福官网已经打开了。"]:
        assert app._claims_completed_action(text) is True, text
        reason, _ = app._voice_answer_retry(
            text=text, had_tool_call=False, had_mutation_receipt=False,
            search_result=None, constrained_empty=False,
        )
        assert reason == "unbacked_claim", text


def test_ordinary_chat_is_not_mistaken_for_a_claim():
    """这是模式匹配，今晚已经栽过一次（按问号判，9 次误伤 8 次），所以先量后上：
    136 条真实的零调用回复里只命中 3 条，3 条全是真幻觉。这里钉住负例。"""
    import app

    for text in [
        "好，我在这儿。", "明白，不打扰你了。", "行，到点我喊你。",
        "你好。有什么需要我做的？", "我是 EV，你的私人智能管家。",
        "好，讲一个。程序员最讨厌的两件事……", "在的。您说。",
        "23点半，对大多数人来说确实该准备睡了。",
    ]:
        assert app._claims_completed_action(text) is False, text


def test_a_real_receipt_still_lets_it_speak():
    """真调了工具就不该被这条拦下——否则每次成功动作都要白烧一轮。"""
    import app

    reason, _ = app._voice_answer_retry(
        text="维基百科打开了。", had_tool_call=True, had_mutation_receipt=True,
        search_result=None, constrained_empty=False,
    )
    assert reason == ""
