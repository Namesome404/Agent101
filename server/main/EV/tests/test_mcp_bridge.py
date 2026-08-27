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
    assert "40%" in result["after"]


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
