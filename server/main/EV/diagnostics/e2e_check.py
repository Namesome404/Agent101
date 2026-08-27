# -*- coding: utf-8 -*-
"""对着真跑的 EV 走一遍整条链路，看还通不通。

单元测试测不到这些：桥有没有真连上外部进程、模型会不会真的选对象、退役的老路
是不是真被挡住、表单填完窗口是不是真收走。这些只有对着活的服务发请求才知道。

    python -m diagnostics.e2e_check              # 全部
    python -m diagnostics.e2e_check --quick      # 跳过要花钱的模型轮次

每一项都打出真实证据（耗时、回执、对象名），而不是只说「通过」——
说通过而拿不出证据，和没测一样。
"""

import argparse
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8002"
_RESULTS = []


def _get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _post(path, payload, timeout=120):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=body, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def check(name, ok, evidence=""):
    _RESULTS.append((name, bool(ok)))
    print("  %s %-42s %s" % ("✓" if ok else "✗", name, evidence))
    return bool(ok)


def _last_turn():
    """最近一轮语音的调用与回执。看模型实际选了什么，不是猜。"""
    import collections

    from devices.coding.turn_trace import TRACE_PATH

    rows = collections.OrderedDict()
    for line in TRACE_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except Exception:
            continue
        turn = item.get("turn_id")
        if turn:
            rows.setdefault(turn, []).append(item)
    # 取最后一个「有用户发言」的轮次。直接取最后一个 turn_id 会抓到后台写进来的
    # 运行时事件组（surface.ready 之类），于是报出「0 次调用、0ms」这种假失败——
    # 今晚第三次栽在自检自己身上了。
    ordered = [
        evs for evs in rows.values()
        if any(e.get("event") == "user" for e in evs)
    ]
    if not ordered:
        return {}
    events = ordered[-1]
    # conversation_reply 是「只说话」的结构化出口，不是动作。把它算成调用，
    # 「闲聊不该动工具」这条就永远不可能过——实测第一版就栽在这儿。
    calls = [
        (e.get("data") or {}).get("arguments") or {}
        for e in events
        if e.get("event") == "tool_call"
        and str((e.get("data") or {}).get("name") or "") != "conversation_reply"
    ]
    said = next(
        (str((e.get("data") or {}).get("text") or "")
         for e in events if e.get("event") == "assistant"), "",
    )
    return {
        "calls": calls,
        "said": said,
        "ms": max((e.get("elapsed_ms") or 0) for e in events),
    }


def _say(text):
    _post("/api/agents/1/chat/stream",
          {"message": text, "voice_mode": True, "history": []})
    time.sleep(2.5)
    return _last_turn()


# ---- 各项检查 -------------------------------------------------------------

def check_service():
    print("\n【服务】")
    try:
        status, _ = _get("/", timeout=5)
        check("控制面在跑", status == 200, "HTTP %s" % status)
    except Exception as exc:
        check("控制面在跑", False, str(exc)[:60])
        return False
    return True


def check_mcp_bridge():
    print("\n【MCP 桥】")
    import sys

    sys.path.insert(0, ".")
    from control_plane.object_registry import object_registry
    from tools import mcp_bridge, object_control

    object_control.ensure_builtin_provider()
    names = mcp_bridge.registered_servers()
    check("按配置登记了 server", bool(names), "、".join(names) or "一个都没有")
    if not names:
        return

    for server in names:
        deadline = time.time() + 25
        while time.time() < deadline and not mcp_bridge.all_tools(server):
            time.sleep(1)
        full = mcp_bridge.all_tools(server)
        obj = next(
            (o for o in object_registry.world()
             if o.get("target_id") == "mcp.%s" % server), {},
        )
        state = obj.get("state") or {}
        check("%s 连得上" % server, bool(state.get("reachable")),
              "工具 %d 个，其中语音层 %d 个" % (len(full), state.get("tools") or 0))
        check("%s 分层生效" % server,
              (state.get("work_only") or 0) > 0 or not full,
              "留给工作 Agent %d 个" % (state.get("work_only") or 0))
        check("%s 有中文名/别名" % server,
              obj.get("name") != server or len(obj.get("aliases") or []) > 1,
              "名字=%s 别名=%s" % (obj.get("name"), (obj.get("aliases") or [])[:3]))

        # 热调用要快：常驻会话的全部意义就在这里
        if full:
            started = time.perf_counter()
            object_registry.execute(
                "invoke", "mcp.%s" % server,
                {"command": full[0], "args": {}}, {},
            )
            spent = (time.perf_counter() - started) * 1000
            check("%s 热调用够快" % server, spent < 1500, "%.0f ms" % spent)


def check_web_retired():
    print("\n【网站开窗已退役】")
    import sys

    sys.path.insert(0, ".")
    from tools import surface_control

    _, meta = surface_control.execute({
        "action": "create", "url": "https://www.bilibili.com", "continue_after": False,
    })
    check("外部网站被拒绝", meta.get("reason") == "web_window_retired",
          str(meta.get("error") or "")[:52])
    check("报错说清了该走哪条路",
          "new_page" in str(meta.get("error") or ""),
          "只说不支持的话，模型会换个参数接着试")

    from devices.coding import surface_tools

    check("EV 自己的页面豁免",
          surface_tools.is_local_page("http://127.0.0.1:8002/forms/x"),
          "表单靠这条")


def check_forms():
    print("\n【表单】")
    import sys

    sys.path.insert(0, ".")
    from devices.coding.scene_store import scene_store

    # owner_id 每次不同：表单在内存里留 40 份，用固定 id 的话上一次跑剩下的
    # 答案会混进来，「答案回到发问方」就会数出 2 份。自检自己得是幂等的。
    owner = "e2e_%d" % int(time.time())
    status, body = _post("/api/forms", {
        "title": "端到端自检",
        "owner_kind": "run", "owner_id": owner,
        "fields": [
            {"key": "ok", "type": "choice", "label": "选一个",
             "options": ["甲", "乙"], "required": True},
        ],
    })
    created = json.loads(body) if status == 200 else {}
    form_id = created.get("form_id") or ""
    check("能声明一张表", bool(form_id), "form_id=%s" % (form_id or "无"))
    if not form_id:
        return

    check("窗口自己弹出来了",
          bool((created.get("window") or {}).get("ok")),
          "surface=%s" % (created.get("window") or {}).get("surface_id"))

    status, page = _get("/forms/%s" % form_id)
    check("页面能打开", status == 200 and "端到端自检" in page,
          "HTTP %s，%d 字节" % (status, len(page)))

    status, body = _post("/api/forms/%s/submit" % form_id, {"answers": {}})
    check("缺必填被整张退回", status == 400, json.loads(body).get("error", "")[:36])

    status, body = _post("/api/forms/%s/submit" % form_id, {"answers": {"ok": "乙"}})
    result = json.loads(body)
    check("正常提交收下了", status == 200 and result.get("ok"), "answers=%s"
          % json.dumps(result.get("answers") or {}, ensure_ascii=False))
    check("填完窗口自己收走", result.get("window_closed") is True,
          "visible=%s" % (scene_store.get("form-%s" % form_id) or {}).get("visible"))

    status, body = _get("/api/forms?owner_kind=run&owner_id=%s" % owner)
    items = json.loads(body).get("items") or []
    check("答案回到发问方", len(items) == 1 and items[0]["answers"]["ok"] == "乙",
          "%d 份" % len(items))

    status, _ = _post("/api/forms/%s/submit" % form_id, {"answers": {"ok": "甲"}})
    check("重复提交被拒", status == 400, "HTTP %s" % status)


def check_voice_turns():
    print("\n【语音回合】（要花模型调用）")
    turn = _say("打开百度。")
    targets = [str(c.get("target") or "") for c in turn.get("calls") or []]
    named = [t for t in targets if t] or ["(只有 inspect)"]
    check("开网页走浏览器，不是壳窗口",
          any(t.startswith("mcp.") for t in targets),
          "选了 %s，%d 次调用，%.0fms"
          % ("、".join(named), len(targets), turn.get("ms") or 0))

    turn = _say("嗯，知道了。")
    check("纯闲聊不动工具",
          not (turn.get("calls") or []),
          "%d 次调用，%.0fms，说了 %r"
          % (len(turn.get("calls") or []), turn.get("ms") or 0,
             (turn.get("said") or "")[:24]))

    turn = _say("定个三分钟计时器。")
    check("动作一轮到位",
          len(turn.get("calls") or []) == 1,
          "%d 次调用，%.0fms" % (len(turn.get("calls") or []), turn.get("ms") or 0))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="跳过要花模型调用的那几项")
    args = parser.parse_args()

    print("EV 端到端自检")
    if not check_service():
        print("\n控制面没起来，后面的都跑不了。")
        return
    check_mcp_bridge()
    check_web_retired()
    check_forms()
    if not args.quick:
        check_voice_turns()

    passed = sum(1 for _, ok in _RESULTS if ok)
    print("\n%d/%d 项通过" % (passed, len(_RESULTS)))
    failed = [name for name, ok in _RESULTS if not ok]
    if failed:
        print("没过的：%s" % "、".join(failed))


if __name__ == "__main__":
    main()
