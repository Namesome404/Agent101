"""通用 MCP 桥：把任意 MCP server 的工具映射成对象。

为什么要有它：EV 侧此前一个 MCP 客户端都没有。灯带能在语音里用不是因为它有
MCP，而是 led.py 直连 HTTP 又手写了一遍对象适配器——同一个灯两套接入。每加一个
MCP 都手写一遍，等于把 MCP 的通用性扔了。

这里不连真服务：把 _call_blocking 换掉，验的是映射规则和超时降级本身。
真服务的对照另有一条路——灯带同时有 MCP 通道和手写适配器，两边结果可以直接比。
"""

import time

import pytest

from tools import mcp_bridge


LED_TOOLS = [
    {
        "name": "led_power",
        "description": "打开或关闭 WS2812 灯带。on=true 开灯，on=false 关灯。",
        "schema": {"type": "object", "required": ["on"],
                   "properties": {"on": {"type": "boolean"}}},
    },
    {
        "name": "led_brightness",
        "description": "设置灯光亮度百分比，brightness 范围为 0 到 100。",
        "schema": {"type": "object", "required": ["brightness"],
                   "properties": {"brightness": {"type": "integer"}}},
    },
]


@pytest.fixture(autouse=True)
def clean():
    mcp_bridge.forget_all()
    yield
    mcp_bridge.forget_all()


def _stub(monkeypatch, value=None, error="", call_error="", delay=0.0, calls=None):
    """error 让所有往返都失败；call_error 只让 invoke 失败、列清单照常成功。"""
    def fake(url, timeout_s, action, **kwargs):
        if calls is not None:
            calls.append((action, kwargs))
        if delay:
            time.sleep(delay)
        if error:
            return None, error
        if action == "list":
            return list(LED_TOOLS), ""
        if call_error:
            return None, call_error
        return value, ""
    monkeypatch.setattr(mcp_bridge, "_call_blocking", fake)


def test_tools_become_commands_and_argument_shapes(monkeypatch):
    _stub(monkeypatch)
    mcp_bridge.register_server("muse-led", "http://127.0.0.1:8012/mcp")
    mcp_bridge._refresh_catalog("muse-led")   # 启动时预热做的就是这件事
    d = mcp_bridge._descriptor("muse-led")

    assert d["target_id"] == "mcp.muse-led"
    assert d["kind"] == "mcp"
    assert d["commands"] == ["led_power", "led_brightness"]
    assert d["state"]["reachable"] is True
    assert d["command_args"]["led_power"]["on"].endswith("必填")


def test_the_tool_description_is_carried_over(monkeypatch):
    """取值范围常常只写在工具描述里，schema 里只有光秃秃一个 type。

    拿灯带对照时发现的：led_brightness 的 schema 只说 integer，而它的描述写着
    「范围为 0 到 100」。丢了这句，模型只能靠报错试出参数范围——正是手写适配器
    当初要消灭的事（实测一次调灯因此要三个来回）。
    """
    _stub(monkeypatch)
    mcp_bridge.register_server("muse-led", "http://127.0.0.1:8012/mcp")
    mcp_bridge._refresh_catalog("muse-led")
    shape = mcp_bridge._descriptor("muse-led")["command_args"]["led_brightness"]
    assert "0 到 100" in shape["说明"]


def test_unreachable_server_says_so_instead_of_pretending(monkeypatch):
    """连不上要如实写进 state。世界快照照实投影，模型才不会许诺做不到的事。"""
    _stub(monkeypatch, error="ConnectionError: refused")
    mcp_bridge.register_server("muse-led", "http://127.0.0.1:8012/mcp")
    d = mcp_bridge._descriptor("muse-led")

    assert d["state"]["reachable"] is False
    assert "refused" in d["state"]["error"]
    assert d["commands"] == []


def test_a_dead_server_is_not_retried_every_turn(monkeypatch):
    """连不上之后要冷却。每轮都去撞一次墙，等于每轮白等一个超时。"""
    calls = []
    _stub(monkeypatch, error="ConnectionError: refused", calls=calls)
    mcp_bridge.register_server("muse-led", "http://127.0.0.1:8012/mcp")

    for _ in range(5):
        mcp_bridge._descriptor("muse-led")
    assert len(calls) == 1, "冷却期内不该反复重连"


def test_the_catalog_is_cached(monkeypatch):
    """工具清单几乎不变，每轮都去问一次就是白等一个往返。"""
    calls = []
    _stub(monkeypatch, calls=calls)
    mcp_bridge.register_server("muse-led", "http://127.0.0.1:8012/mcp")

    for _ in range(5):
        mcp_bridge._descriptor("muse-led")
    assert len(calls) == 1


def test_invoke_returns_a_receipt(monkeypatch):
    _stub(monkeypatch, value={"ok": True, "text": "亮度已设为 40%"})
    mcp_bridge.register_server("muse-led", "http://127.0.0.1:8012/mcp")
    result = mcp_bridge._execute(
        "invoke", "mcp.muse-led",
        {"command": "led_brightness", "args": {"brightness": 40}}, {},
    )
    assert result["ok"] is True and result["changed"] is True
    # 工具输出填 display，注册表据此算 after（见 test_the_tool_output_becomes_the_receipt）
    assert "40%" in result["display"]


def test_an_unknown_tool_is_refused_with_the_real_list(monkeypatch):
    """报错要带上真有哪些工具，否则模型只能继续猜。"""
    _stub(monkeypatch)
    mcp_bridge.register_server("muse-led", "http://127.0.0.1:8012/mcp")
    result = mcp_bridge._execute("invoke", "mcp.muse-led", {"command": "led_disco"}, {})
    assert result["ok"] is False
    assert "led_power" in result["available"]


def test_a_failed_call_never_reports_success(monkeypatch):
    """没做成就不能有 after。播报规则锚在 after 上，没有它就说不出「已完成」。"""
    _stub(monkeypatch, call_error="超时（3.0s）")
    mcp_bridge.register_server("muse-led", "http://127.0.0.1:8012/mcp")
    result = mcp_bridge._execute(
        "invoke", "mcp.muse-led", {"command": "led_power", "args": {"on": True}}, {},
    )
    assert result["ok"] is False and result["changed"] is False
    assert "after" not in result


def test_a_hung_server_cannot_eat_the_turn():
    """真超时：反射层一轮预算约 1.5 秒，卡住的服务不能把它吃光。

    这条不打桩 _call_blocking——要验的正是它自己的超时。
    """
    started = time.perf_counter()
    value, error = mcp_bridge._call_blocking(
        "http://127.0.0.1:9/mcp", 0.5, "list",
    )
    elapsed = time.perf_counter() - started
    assert value is None and error
    assert elapsed < 2.0, "超时没有生效，用了 %.1fs" % elapsed


def test_registering_nothing_produces_nothing():
    """纯加法：没登记任何 server 时，桥对世界一无所增。"""
    assert mcp_bridge._discover() == []


def test_frequently_used_objects_keep_their_argument_shapes(monkeypatch):
    """参数形状的预算有限，装不下时要牺牲冷门的，不是牺牲新来的。

    原先按 target_id 字母序取舍，新接进来的 mcp.* 排最后、第一个被挤掉——
    接了个能力，参数清单却进不了提示词，模型只能靠 inspect 多跑一个来回。
    """
    from control_plane import object_registry as oreg, world_snapshot

    _stub(monkeypatch)
    mcp_bridge.ensure_provider()
    mcp_bridge.register_server("muse-led", "http://127.0.0.1:8012/mcp")

    def listed():
        hint = world_snapshot.capability_hint(max_chars=260)
        return [line[2:].split("（")[0] for line in hint.splitlines() if line.startswith("- ")]

    oreg.reset_usage()
    cold = listed()

    oreg.note_object_used("mcp.muse-led")
    oreg.note_object_used("mcp.muse-led")
    warm = listed()

    assert warm[0] == "mcp.muse-led", "用过的对象没排到前面：%s" % warm
    if "mcp.muse-led" not in cold:
        assert "mcp.muse-led" in warm, "常用对象仍被挤掉"
    oreg.reset_usage()


def test_usage_only_counts_real_actions():
    """只读的 inspect 不算用量，否则模型每次翻目录都会把排序搅乱。"""
    from control_plane import object_registry as oreg

    oreg.reset_usage()
    oreg.object_registry.execute("inspect", "app.timer", {}, {})
    assert oreg.usage_rank("app.timer") == 0.0
    oreg.reset_usage()


def test_the_catalog_never_blocks_the_turn(monkeypatch):
    """清单每轮都要读，绝不能在读的时候等网络。

    实测一个连不上设备的 MCP，单次往返 2.5 秒；而一轮语音的总预算才 1.5 秒。
    过期就先给旧的、后台去取新的——慢的那一下不能砸在用户那一轮上。
    """
    import threading

    _stub(monkeypatch, delay=0.6)
    mcp_bridge.register_server("muse-led", "http://127.0.0.1:8012/mcp")
    mcp_bridge._refresh_catalog("muse-led")          # 先有一份旧的

    with mcp_bridge._LOCK:
        mcp_bridge._SERVERS["muse-led"]["tools_at"] = 0.0   # 让它过期

    started = time.perf_counter()
    tools, _ = mcp_bridge._catalog("muse-led")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.2, "读清单等了 %.2fs，说明还在关键路径上取" % elapsed
    assert [t["name"] for t in tools] == ["led_power", "led_brightness"], "过期时该先给旧的"
    for thread in threading.enumerate():          # 后台确实去取了
        if thread.name.startswith("mcp-catalog-"):
            thread.join(2.0)


def test_config_file_absent_means_no_mcp_at_all(monkeypatch, tmp_path):
    """纯加法：没有配置文件时，桥对世界一无所增。"""
    monkeypatch.setattr(mcp_bridge, "config_path", lambda: tmp_path / "nope.json")
    assert mcp_bridge.load_config() == {}
    assert mcp_bridge.load_from_config() == []


def test_a_broken_config_does_not_block_startup(monkeypatch, tmp_path):
    """配置写坏了当成「没有 MCP」，不能让它挡住整个服务起不来。"""
    path = tmp_path / "mcp_servers.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    monkeypatch.setattr(mcp_bridge, "config_path", lambda: path)
    assert mcp_bridge.load_config() == {}


def test_config_registers_url_servers_and_honours_enabled(monkeypatch, tmp_path):
    import json as _json

    path = tmp_path / "mcp_servers.json"
    path.write_text(_json.dumps({"mcpServers": {
        "muse-led": {"url": "http://127.0.0.1:8012/mcp"},
        "chrome": {"url": "http://127.0.0.1:9222/mcp", "enabled": False},
        "wechat": {"command": "npx", "args": ["wechat-mcp"]},
        "空的": {},
    }}), encoding="utf-8")
    monkeypatch.setattr(mcp_bridge, "config_path", lambda: path)

    loaded = mcp_bridge.load_from_config()
    # url 和 command 两种都装；enabled=false 的不装；两样都没写的跳过
    assert loaded == ["muse-led", "wechat"]


def test_a_cold_catalog_does_not_make_invoke_claim_the_tool_is_missing(monkeypatch):
    """清单还没取回来时 invoke 要等一下，不能说「没有这个工具」。

    那是把自己的时序问题说成对方的能力缺失——用户会以为装的 MCP 坏了。
    """
    _stub(monkeypatch, value={"ok": True, "text": "已开灯"})
    mcp_bridge.register_server("muse-led", "http://127.0.0.1:8012/mcp")
    # 故意不预热
    result = mcp_bridge._execute(
        "invoke", "mcp.muse-led", {"command": "led_power", "args": {"on": True}}, {},
    )
    assert result["ok"] is True, "冷缓存把真实存在的工具误判成不存在"


def test_only_whitelisted_tools_reach_the_voice_layer(monkeypatch):
    """语音一轮预算约 1.5 秒，只装得下「一次调用就有确定回执」的能力。

    Chrome 的「开标签页」属于这一类，「排查控制台报错」要看日志、改代码、再验证，
    多轮。这两类常常来自同一个 MCP 服务，所以判据必须落在单个工具上。
    """
    _stub(monkeypatch)
    mcp_bridge.register_server(
        "muse-led", "http://x/mcp", voice_tools=["led_power"],
    )
    mcp_bridge._refresh_catalog("muse-led")
    d = mcp_bridge._descriptor("muse-led")

    assert d["commands"] == ["led_power"]
    assert d["state"]["tools"] == 1
    assert d["state"]["work_only"] == 1, "没进语音层的要如实报数"
    assert mcp_bridge.all_tools("muse-led") == ["led_power", "led_brightness"], \
        "全量清单不该被过滤掉——写白名单要照着它写"


def test_exclude_drops_a_few_from_everything(monkeypatch):
    _stub(monkeypatch)
    mcp_bridge.register_server("muse-led", "http://x/mcp", exclude=["led_brightness"])
    mcp_bridge._refresh_catalog("muse-led")
    assert mcp_bridge._descriptor("muse-led")["commands"] == ["led_power"]


def test_no_tiering_means_everything_is_voice(monkeypatch):
    """不写分层就是全给——已经装好的 MCP 不因为加了这个功能而变哑。"""
    _stub(monkeypatch)
    mcp_bridge.register_server("muse-led", "http://x/mcp")
    mcp_bridge._refresh_catalog("muse-led")
    d = mcp_bridge._descriptor("muse-led")
    assert d["commands"] == ["led_power", "led_brightness"]
    assert d["state"]["work_only"] == 0


def test_a_work_layer_tool_is_refused_differently_from_a_missing_one(monkeypatch):
    """「存在但不归语音」和「压根没有」要分开说。

    说成「没有」，模型会换个名字接着猜；说清楚归工作 Agent，它才知道该交出去。
    """
    _stub(monkeypatch)
    mcp_bridge.register_server("muse-led", "http://x/mcp", voice_tools=["led_power"])
    mcp_bridge._refresh_catalog("muse-led")

    work = mcp_bridge._execute(
        "invoke", "mcp.muse-led", {"command": "led_brightness", "args": {}}, {},
    )
    missing = mcp_bridge._execute(
        "invoke", "mcp.muse-led", {"command": "led_disco", "args": {}}, {},
    )
    assert work["ok"] is False and "工作 Agent" in work["error"]
    assert missing["ok"] is False and "没有" in missing["error"]


def test_config_carries_the_tiering(monkeypatch, tmp_path):
    import json as _json

    path = tmp_path / "mcp_servers.json"
    path.write_text(_json.dumps({"mcpServers": {
        "chrome": {"url": "http://x/mcp",
                   "voice_tools": ["new_page", "close_page"]},
    }}), encoding="utf-8")
    monkeypatch.setattr(mcp_bridge, "config_path", lambda: path)
    mcp_bridge.load_from_config()

    with mcp_bridge._LOCK:
        assert mcp_bridge._SERVERS["chrome"]["voice_tools"] == ["new_page", "close_page"]


def test_stdio_servers_are_registered_from_config(monkeypatch, tmp_path):
    """stdio 形式：没有网址，由 EV 把它拉起来。

    chrome-devtools-mcp、多数 npx 起的 MCP 都是这种。实测它有 29 个工具，
    冷启动（含拉起 Chrome）7.8 秒，之后每次调用 0.01 秒——所以进程必须常驻，
    每次重开根本扛不住反射层 1.5 秒的预算。
    """
    import json as _json

    path = tmp_path / "mcp_servers.json"
    path.write_text(_json.dumps({"mcpServers": {
        "chrome-dev": {
            "command": "npx",
            "args": ["-y", "chrome-devtools-mcp@latest", "--isolated"],
            "voice_tools": ["new_page", "close_page"],
        },
    }}), encoding="utf-8")
    monkeypatch.setattr(mcp_bridge, "config_path", lambda: path)

    assert mcp_bridge.load_from_config() == ["chrome-dev"]
    with mcp_bridge._LOCK:
        meta = mcp_bridge._SERVERS["chrome-dev"]
    assert meta["command"] == "npx"
    assert meta["args"][:2] == ["-y", "chrome-devtools-mcp@latest"]
    assert meta["url"] == ""


def test_a_warm_up_in_flight_does_not_make_invoke_say_the_tool_is_missing(monkeypatch):
    """启动预热还没跑完时调工具，要等那一次，不能空手而归。

    实测踩过：ensure_provider 起了预热线程，紧接着调 new_page，
    _refresh_catalog 因为「已经有人在取」直接返回，清单是空的，
    于是回「chrome-dev 没有 new_page 这个工具」——用户会以为装的 MCP 坏了。
    """
    import threading as _t

    started = _t.Event()
    release = _t.Event()

    def slow(url, timeout_s, action, **kwargs):
        if action == "list":
            started.set()
            release.wait(3)
            return list(LED_TOOLS), ""
        return {"ok": True, "text": "done"}, ""

    monkeypatch.setattr(mcp_bridge, "_call_blocking", slow)
    mcp_bridge.register_server("muse-led", "http://x/mcp")

    warm = _t.Thread(target=mcp_bridge._refresh_catalog, args=("muse-led",), daemon=True)
    warm.start()
    started.wait(2)                      # 预热正卡在取清单里

    def do_invoke(box):
        release.set()                    # 让预热完成
        box.append(mcp_bridge._execute(
            "invoke", "mcp.muse-led", {"command": "led_power", "args": {"on": True}}, {},
        ))

    box = []
    caller = _t.Thread(target=do_invoke, args=(box,), daemon=True)
    caller.start()
    caller.join(6)
    warm.join(3)

    assert box and box[0]["ok"] is True, "预热在跑时把真实存在的工具误判成不存在"


def test_the_tool_output_becomes_the_receipt(monkeypatch):
    """工具自己说的那句要填 display。

    注册表统一算 after：provider 报了 display 才用它，否则会回头重查对象目录，
    把「reachable=是、tools=5」这种状态摘要当成结果播给用户。
    """
    _stub(monkeypatch, value={"ok": True, "text": "## Pages\n1: Example (https://example.com/)"})
    mcp_bridge.register_server("muse-led", "http://x/mcp")
    mcp_bridge._refresh_catalog("muse-led")
    r = mcp_bridge._execute(
        "invoke", "mcp.muse-led", {"command": "led_power", "args": {"on": True}}, {},
    )
    assert "example.com" in r["display"]
    assert "\n" not in r["display"], "播报那句要压成一行"
    assert "## Pages" in r["text"], "完整输出仍要给模型"


def test_a_server_can_be_pointed_at_in_plain_language(monkeypatch):
    """接进来的 MCP 得能用人话点到，否则它对用户是隐形的。

    实测漏掉的代价：说「用浏览器打开 YouTube」，模型 inspect 找了四次
    「浏览器」「browser」都没匹配上 mcp.chrome-dev（那时它的名字就叫
    chrome-dev、描述是「外部 MCP 服务 chrome-dev 提供的能力」），
    最后绕回老路，12.9 秒 5 次调用。
    """
    _stub(monkeypatch)
    mcp_bridge.register_server(
        "chrome-dev", "http://x/mcp",
        label="浏览器",
        aliases=["chrome", "谷歌浏览器", "网页"],
        description="真正的 Chrome 浏览器。开网页、切页面、截图都用它。",
    )
    mcp_bridge._refresh_catalog("chrome-dev")
    d = mcp_bridge._descriptor("chrome-dev")

    assert d["name"] == "浏览器"
    assert "chrome" in d["aliases"] and "谷歌浏览器" in d["aliases"]
    assert "chrome-dev" in d["aliases"], "原名也要留着，配置里是按它写的"
    assert "Chrome 浏览器" in d["description"]


def test_without_a_label_it_falls_back_to_the_server_name(monkeypatch):
    _stub(monkeypatch)
    mcp_bridge.register_server("muse-led", "http://x/mcp")
    mcp_bridge._refresh_catalog("muse-led")
    d = mcp_bridge._descriptor("muse-led")
    assert d["name"] == "muse-led"
    assert d["aliases"] == ["muse-led"]


def test_config_carries_the_names(monkeypatch, tmp_path):
    import json as _json

    path = tmp_path / "mcp_servers.json"
    path.write_text(_json.dumps({"mcpServers": {
        "chrome-dev": {"command": "npx", "args": ["x"],
                       "name": "浏览器", "aliases": ["chrome"],
                       "description": "真正的 Chrome。"},
    }}), encoding="utf-8")
    monkeypatch.setattr(mcp_bridge, "config_path", lambda: path)
    mcp_bridge.load_from_config()

    with mcp_bridge._LOCK:
        meta = mcp_bridge._SERVERS["chrome-dev"]
    assert meta["label"] == "浏览器"
    assert meta["aliases"] == ["chrome"]
    assert "真正的 Chrome" in meta["description"]


def test_retired_web_surfaces_stop_being_objects(monkeypatch):
    """历史遗留的网站窗口退役后不再作为对象暴露。

    留着它们，用户说「打开 YouTube」时模型必然命中 surface.web-youtube-com
    而不是浏览器——实测两次都是这样，加了别名也没用。
    """
    from devices.coding import surface_tools

    monkeypatch.setattr(surface_tools, "web_windows_enabled", lambda: False)
    assert surface_tools.is_retired_web_surface("web-youtube-com") is True
    assert surface_tools.is_retired_web_surface("form-abc") is False
    assert surface_tools.is_retired_web_surface("app-timer") is False

    monkeypatch.setattr(surface_tools, "web_windows_enabled", lambda: True)
    assert surface_tools.is_retired_web_surface("web-youtube-com") is False


def test_local_pages_are_not_websites():
    """表单靠这条豁免：EV 自己起的页面照常开窗。"""
    from devices.coding import surface_tools

    assert surface_tools.is_local_page("http://127.0.0.1:8002/forms/abc") is True
    assert surface_tools.is_local_page("http://localhost:8002/forms/abc") is True
    assert surface_tools.is_local_page("https://www.youtube.com") is False
    assert surface_tools.is_local_page("") is False


def test_a_dead_server_says_so_instead_of_denying_the_tool(monkeypatch):
    """服务没起来 ≠ 没有这个工具。

    实测把 MCP 弄坏之后，回执说「没有 new_page 这个工具」——工具明明存在，
    是服务器没起来。模型据此以为能力缺失，转而去别处瞎试，连试六次，
    其中还绕回了已经退役的 surface.new。
    """
    _stub(monkeypatch, error="启动失败 FileNotFoundError: 没有这个命令")
    mcp_bridge.register_server("chrome-dev", "http://x/mcp")
    r = mcp_bridge._execute(
        "invoke", "mcp.chrome-dev", {"command": "new_page", "args": {}}, {},
    )
    assert r["ok"] is False
    assert "连不上" in r["error"]
    assert "没有 new_page" not in r["error"]


def test_a_state_tool_turns_the_service_status_into_object_state(monkeypatch):
    """服务的现状要变成对象的 state，否则模型只知道它存在、不知道它现在什么样。"""
    def fake(url, timeout_s, action, **kwargs):
        if action == "list":
            return list(LED_TOOLS), ""
        return {"ok": True, "text": "## Pages\n1: 百度一下 (https://www.baidu.com/)"}, ""

    monkeypatch.setattr(mcp_bridge, "_call_blocking", fake)
    mcp_bridge.register_server("chrome-dev", "http://x/mcp", state_tool="list_pages")
    mcp_bridge._refresh_catalog("chrome-dev")
    mcp_bridge._refresh_state("chrome-dev")
    d = mcp_bridge._descriptor("chrome-dev")

    assert "百度一下" in d["state"]["detail"]
    assert "百度一下" in d["display"]


def test_open_sites_become_aliases(monkeypatch):
    """用户说的是「百度」，服务报的是网址。这层断了，「百度已经打开了」
    就没有任何东西认得出来——以前由 web-* 窗口的站点别名承担，它们退役了。
    """
    def fake(url, timeout_s, action, **kwargs):
        if action == "list":
            return list(LED_TOOLS), ""
        return {"ok": True, "text": "1: (https://www.baidu.com/) 2: (https://www.bilibili.com/)"}, ""

    monkeypatch.setattr(mcp_bridge, "_call_blocking", fake)
    mcp_bridge.register_server("chrome-dev", "http://x/mcp", state_tool="list_pages")
    mcp_bridge._refresh_catalog("chrome-dev")
    mcp_bridge._refresh_state("chrome-dev")
    aliases = mcp_bridge._descriptor("chrome-dev")["aliases"]

    assert "百度" in aliases and "哔哩哔哩" in aliases
    assert "baidu" in aliases and "baidu.com" in aliases


def test_no_state_tool_means_no_extra_calls(monkeypatch):
    """没声明状态工具的服务一切照旧，不会平白多出往返。"""
    calls = []
    _stub(monkeypatch, calls=calls)
    mcp_bridge.register_server("muse-led", "http://x/mcp")
    mcp_bridge._refresh_catalog("muse-led")
    before = len(calls)
    mcp_bridge._descriptor("muse-led")
    mcp_bridge._descriptor("muse-led")
    assert len(calls) == before


def test_a_resident_server_is_never_folded_away(monkeypatch):
    """常驻的能力，签名一直留在提示词里，不参与预算排队。

    被折叠成「用到时先 inspect」就等于每次用都白烧一整轮模型（中位 1.8 秒）。
    浏览器、微信这类天天要用的东西不该有这个风险——之前是靠把预算抬高让它
    碰巧装得下，对象一多照样掉出去。
    """
    from control_plane import world_snapshot

    _stub(monkeypatch)
    mcp_bridge.ensure_provider()
    mcp_bridge.register_server("chrome-dev", "http://x/mcp", resident=True)
    mcp_bridge._refresh_catalog("chrome-dev")

    assert mcp_bridge._descriptor("chrome-dev")["pinned"] is True
    listed = [
        line[2:].split("（")[0]
        for line in world_snapshot.capability_hint(max_chars=120).splitlines()
        if line.startswith("- ")
    ]
    assert "mcp.chrome-dev" in listed, "预算再紧也不该把常驻的挤掉：%s" % listed
    assert listed[0] == "mcp.chrome-dev", "常驻的要排最前"


def test_a_normal_server_still_queues_by_budget(monkeypatch):
    """没声明常驻的照旧参与排队——常驻是选出来的，不是默认。"""
    _stub(monkeypatch)
    mcp_bridge.register_server("muse-led", "http://x/mcp")
    mcp_bridge._refresh_catalog("muse-led")
    assert mcp_bridge._descriptor("muse-led")["pinned"] is False


def test_config_carries_resident(monkeypatch, tmp_path):
    import json as _json

    path = tmp_path / "mcp_servers.json"
    path.write_text(_json.dumps({"mcpServers": {
        "chrome-dev": {"command": "npx", "args": ["x"], "resident": True},
        "wechat": {"command": "npx", "args": ["y"]},
    }}), encoding="utf-8")
    monkeypatch.setattr(mcp_bridge, "config_path", lambda: path)
    mcp_bridge.load_from_config()

    with mcp_bridge._LOCK:
        assert mcp_bridge._SERVERS["chrome-dev"]["resident"] is True
        assert mcp_bridge._SERVERS["wechat"]["resident"] is False
