# -*- coding: utf-8 -*-
"""Search evidence quality must block plausible-looking but unsupported claims."""
from tools import deep_search


def test_exact_product_evidence_ranks_above_generic_esp32_pages():
    items, quality = deep_search._rank_items_for_query([
        {
            "title": "M5Stack 全系列主机选型",
            "snippet": "采用 ESP32-S3，支持 Wi-Fi，适合 AIoT 项目开发。",
            "url": "https://example.com/generic",
            "score": 0.9,
        },
        {
            "title": "ESP32-S3 红外遥控收发成品开发板",
            "snippet": "板载红外发射管和红外接收管，可直接购买。",
            "url": "https://example.com/exact",
            "score": 0.5,
        },
    ], "ESP32 红外遥控收发 成品开发板 板载红外发射接收")
    assert items[0]["url"].endswith("/exact")
    assert not any(item["url"].endswith("/generic") for item in items)
    assert quality in {"medium", "strong"}


def test_weak_candidates_are_not_described_as_a_found_or_nonexistent_product():
    items, quality = deep_search._rank_items_for_query([
        {
            "title": "ESP32 技术规格书",
            "snippet": "介绍处理器、Wi-Fi 和 GPIO。",
            "url": "https://example.com/spec",
            "score": 0.95,
        },
    ], "ESP32 红外遥控收发 成品开发板 板载红外发射接收")
    assert quality == "weak"
    context = deep_search._build_answer_context(
        "ESP32 红外遥控收发 成品开发板",
        items,
        [],
        [],
        "",
        [],
        evidence_quality=quality,
    )
    assert "只能说『本轮没找到』" in context
    assert "不能说目标不存在" in context
    assert "不能把相近产品/教程说成『找到了』" in context
    assert "https://example.com/spec" not in context


def test_weak_result_keeps_only_the_models_natural_uncertainty_sentence():
    import app

    answer = (
        "搜索结果里没有直接确认哪块板子同时板载红外发射和接收。"
        "不过有一个方向比较明确——某款相近开发板。"
    )
    constrained = app._constrain_search_answer(answer, {
        "query": "ESP32 红外收发成品板",
        "answerable": False,
        "evidence_quality": "weak",
    })
    assert constrained == "搜索结果里没有直接确认哪块板子同时板载红外发射和接收。"
    assert "相近开发板" not in constrained


def test_answerable_search_keeps_the_complete_answer():
    import app

    answer = "找到了明确型号。它同时板载红外发射和接收。"
    assert app._constrain_search_answer(answer, {
        "answerable": True,
        "evidence_quality": "strong",
    }) == answer


def test_advice_question_is_not_silenced_when_search_finds_nothing():
    """检索是补充而不是依据时，扑空不该把模型写好的回答换成一句「不能下结论」。

    真实事故：用户问「我在洞洞板下走飞线可不可以」——他自己就该会答的常识题，
    查询却被上文撑成「ESP32-C3 Super Mini 洞洞板 飞线接线 注意事项」，
    自然扑空，然后回答被整段丢掉，屏幕上只剩罐头话。
    """
    import app

    answer = "可以。洞洞板背面走飞线是常规做法，注意固定、别和电源线并排太长。"
    helpful = {
        "query": "洞洞板 飞线",
        "answerable": False,
        "evidence_quality": "weak",
        "grounding": "helpful",
    }
    assert app._constrain_search_answer(answer, helpful) == answer
    # 事实类问题（型号/价格/链接）仍旧失败即收口，不能靠这条口子放行
    required = dict(helpful, grounding="required")
    constrained = app._constrain_search_answer(answer, required)
    # 仍然失败即收口：证据不足又写了笃定说法时返回空串，调用方据此让模型
    # 自己重说（说清找到了什么、缺什么），而不是由服务端换一句罐头话。
    assert constrained == ""


def test_weak_context_tells_model_to_answer_from_its_own_knowledge():
    from tools import deep_search

    helpful = deep_search._build_answer_context(
        "洞洞板 飞线", [], [], [], "", [], evidence_quality="weak", grounding="helpful",
    )
    assert "用你自己的知识正常回答" in helpful
    assert "不能下结论" in helpful  # 作为禁止说的话出现
    required = deep_search._build_answer_context(
        "某型号板子", [], [], [], "", [], evidence_quality="weak", grounding="required",
    )
    assert "只能说『本轮没找到』" in required


def test_overspecific_query_is_broadened_before_giving_up():
    """上文里的型号被顺手粘进查询词，长查询必然扑空——先退回主题词再搜。"""
    from tools import deep_search

    assert deep_search._broaden_query(
        "ESP32-C3 Super Mini 洞洞板 飞线接线 注意事项"
    ) == "洞洞板 飞线接线"
    assert deep_search._broaden_query("arduino uno pinout guide") == "arduino uno pinout"
    # 已经足够短的查询没有可放宽的余地，不做无意义的重搜
    assert deep_search._broaden_query("洞洞板 飞线") == ""
    assert deep_search._broaden_query("洞洞板") == ""
