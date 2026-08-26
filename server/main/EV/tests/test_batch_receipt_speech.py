# -*- coding: utf-8 -*-
"""批量回执播报：良性无操作不当失败；真失败不生成固定话术，交给模型解释。

复现并锁定「把所有页面都删除」→ 失败项不再拼出「有一项没成功：xxx」的
自相矛盾播报，而是返回空串让模型基于回执自己解释原因。
"""
import app


def test_delete_all_skips_pinned_and_missing_no_contradiction():
    """删除批量里混进常驻窗（不可删）和已不存在的窗口 → 只播成功那条。"""
    speech = app._batch_direct_reply([
        {"ok": False, "meta": {"action": "delete", "reason": "pinned_surface",
                                "detail": "信息推送是常驻窗口，不能被删除"}},
        {"ok": True, "meta": {"action": "delete", "speech": "窗口已删除"}},
        {"ok": False, "meta": {"action": "delete", "reason": "surface_not_found",
                                "detail": "目标页面不存在"}},
    ])
    assert speech == "窗口已删除"
    assert "没成功" not in speech
    assert "没有收到成功回执" not in speech


def test_genuine_failure_defers_to_model_not_template():
    """真失败不生成「有一项没成功」话术，返回空串交给模型解释。"""
    speech = app._batch_direct_reply([
        {"ok": False, "meta": {"action": "close", "reason": "boom",
                                "detail": "真实失败原因"}},
    ])
    assert speech == ""


def test_mixed_batch_with_failure_defers_whole_batch_to_model():
    """批量里混入真失败 → 整批交给模型，不拼固定失败话术。"""
    speech = app._batch_direct_reply([
        {"ok": True, "meta": {"action": "delete", "speech": "窗口已删除"}},
        {"ok": False, "meta": {"action": "close", "reason": "boom",
                                "detail": "真实失败原因"}},
    ])
    assert speech == ""


def test_is_benign_receipt_is_context_aware():
    assert app._is_benign_receipt({"reason": "pinned_surface"}) is True
    assert app._is_benign_receipt({"reason": "surface_not_found", "action": "delete"}) is True
    assert app._is_benign_receipt({"reason": "surface_not_found", "action": "close"}) is True
    # update 指向不存在的窗口是真错误，模型需要知道，不算良性
    assert app._is_benign_receipt({"reason": "surface_not_found", "action": "update"}) is False
    assert app._is_benign_receipt({"reason": "boom", "action": "close"}) is False


def test_all_success_speech_joined_and_deduped():
    speech = app._batch_direct_reply([
        {"ok": True, "meta": {"speech": "灯已打开"}},
        {"ok": True, "meta": {"speech": "灯已打开"}},
        {"ok": True, "meta": {"speech": "窗口已关闭"}},
    ])
    assert speech == "灯已打开，窗口已关闭"
