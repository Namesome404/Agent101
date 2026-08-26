# -*- coding: utf-8 -*-
"""检索词必须保住用户说的那个型号。

真实事故：用户说「我想知道雷鸟 IO」，模型把检索词写成「雷鸟Air 3智能眼镜详细
参数评测」，还编出「分辨率 视场角」这类子查询；用户纠正「我说雷鸟 IO」之后，
它又搜了一模一样的 Air 3。模型没听过的新品会被替换成训练里熟悉的近似型号，
而且纠正也拉不回来。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_wording_difference_is_reported_not_silently_overridden():
    """检索词和用户原话不一致时只报告，不强行改回去。

    曾经在这里强行改回用户原话，是错的：ASR 把「DGX」听成「C G X」，模型改成
    DGX 是**对的纠正**，被硬拗回 CGX 反而查不到东西。而「雷鸟 IO」被换成
    「雷鸟 Air 3」是**错的替换**——两者文本上一模一样（都是空格分开的单字母），
    只有靠知识才分得清，而知识在模型那儿不在规则这儿。
    """
    import app

    note = app._query_differs_from_user_wording("英伟达 DGX 工作站 价格", "C G X 工作站的价格。")
    assert note and "CGX" in note and "DGX" in note
    # 忠实的检索词不触发
    assert app._query_differs_from_user_wording("RayNeo IO 参数", "我想知道雷鸟 IO") == ""
    assert app._query_differs_from_user_wording("开源机械臂 入门", "找几个入门开源机械臂") == ""
    # 单个字母不算型号（「B 站」的 B）
    assert app._query_differs_from_user_wording("哔哩哔哩 新番", "看看 B 站上有什么新番") == ""


def test_faithful_requests_pass_through():
    """忠实保留了用户标识的检索词不触发报告。"""
    import app

    for request, spoken in [
        ("RayNeo IO 参数 评测", "我想知道雷鸟 IO"),
        ("ESP32-C3 ESP8266 区别", "esp32c3 和 esp8266 有什么区别"),
        ("开源机械臂 入门项目", "找几个适合入门的开源机械臂项目"),
    ]:
        assert app._query_differs_from_user_wording(request, spoken) == ""


def test_single_letters_are_not_treated_as_model_numbers():
    """「B 站」的 B 不是型号，改写成「哔哩哔哩」是合理的，不该被报告。"""
    import app

    assert app._query_differs_from_user_wording("哔哩哔哩 新番 2026", "看看 B 站上有什么新番") == ""


def test_no_prompt_rule_tries_to_police_the_wording():
    """不再用提示词管「型号能不能换」。

    试过两版都不行：写「一律原样保留」会把「把听岔的 C G X 纠正成 DGX」这种
    正确纠正一起禁掉；写「听岔可以纠正」又让它对「雷鸟 IO」退化成反问。
    文本上分不清对错的事，交给模型判断，服务端只负责把差异摆出来（见
    _query_differs_from_user_wording），让它自己决定并对用户说明。
    """
    from tools import task_control

    description = task_control.tool_definition()["function"]["parameters"]["properties"]
    assert description["request"]["description"] == "用户完整原意。"
