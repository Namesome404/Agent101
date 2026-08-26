import app


def test_voice_persona_is_natural_butler_behavior_not_voice_cosplay():
    prompt = app._VOICE_PERSONA_SYSTEM

    assert "智能管家感来自具体行为" in prompt
    assert "执行任务先做再按真实回执报告" in prompt
    assert "冷幽默" in prompt
    assert "已经很熟的朋友" not in prompt
    assert "像微信语音回熟人" not in prompt
    assert "不用邀约或待命句收尾" in prompt
    assert "回答问题先给结论" in prompt
    assert "不靠低沉声线" in prompt
    assert "不强制使用『您』『先生』" in prompt
    assert "风格示例" not in prompt
    assert "说一声" in prompt
    assert "有什么直接说" in prompt
    assert "交给我就行" in prompt
    assert "内容答完就停" in prompt
    assert "不邀请用户下指令" in prompt
    assert "禁止甜嗲" in prompt
