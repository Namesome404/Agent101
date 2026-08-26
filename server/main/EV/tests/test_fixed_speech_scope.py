"""固定话术只剩计时器暂停/恢复，其余交回模型；且必须中英各一份。

背景：用户听到的「已经加上时间了」「窗口已关闭」「计时器已暂停」一字不差就是
服务端字典里的字符串，不是模型说的。object_control 里写着「模型没写就不替它说」，
但那只是句注释——适配器早把话塞进回执了，不覆盖也照样播出去。

现在只保留按得最频繁、要的就是一声确认的那两个动作（暂停/恢复），其余一律
不往回执里塞话。保留的这两句要跟着对话语言走，用户切英文时不能还蹦中文。
"""

from tools import object_control, surface_apps


def test_only_pause_and_resume_keep_a_fixed_line():
    assert surface_apps._speech("timer", "pause") == "暂停了。"
    assert surface_apps._speech("timer", "resume") == "继续了。"
    # 其余全部交回模型：不返回任何话
    for command in ("open", "start", "add", "reset", "status", "close"):
        assert surface_apps._speech("timer", command) == ""
    for command in ("open", "append", "replace", "clear", "status"):
        assert surface_apps._speech("notes", command) == ""


def test_fixed_lines_follow_the_conversation_language():
    assert surface_apps._speech("timer", "pause", "en") == "Paused."
    assert surface_apps._speech("timer", "resume", "en") == "Resumed."
    assert surface_apps._speech("timer", "pause", "zh") == "暂停了。"
    # 认不出的语言回落中文，不要返回空把人晾着
    assert surface_apps._speech("timer", "pause", "ja") == "暂停了。"


def test_language_comes_from_what_the_user_just_said():
    assert object_control._lang_of("把计时器暂停") == "zh"
    assert object_control._lang_of("Pause the timer.") == "en"
    assert object_control._lang_of("") == "en"
    # 中英混说只要有汉字就按中文
    assert object_control._lang_of("把 timer 暂停一下") == "zh"


def _timer(command, **args):
    """跑一条计时器命令并把状态收拾干净。

    无头环境里没有桌面壳，回执的 ok 恒为 false（rendered=false），但 meta 是
    完整构造出来的——这里看的就是 meta 里有没有话，跟渲染无关。
    """
    _, meta = surface_apps.execute(dict(args, app_id="timer", command=command))
    return meta


def test_kept_lines_are_marked_so_they_survive_the_strip():
    """保留的那两句要带 speech_fixed，否则会被 object_control 当模子话抹掉。"""
    try:
        _timer("start", duration_seconds=60, lang="zh")
        meta = _timer("pause", lang="zh")
        assert meta.get("accepted") is True
        assert meta.get("speech") == "暂停了。"
        assert meta.get("speech_fixed") is True

        meta = _timer("resume", lang="en")
        assert meta.get("speech") == "Resumed."
        assert meta.get("speech_fixed") is True
    finally:
        _timer("reset")


def test_other_commands_put_no_words_in_the_receipt():
    """不保留的动作：回执里根本没有话，模型才会看着回执自己组织语言。"""
    try:
        meta = _timer("start", duration_seconds=60, lang="zh")
        assert meta.get("accepted") is True
        assert "speech" not in meta
        assert not meta.get("speech_fixed")

        meta = _timer("add", duration_seconds=60, lang="zh")
        # 「已经加上时间了」这句模子话就是从这里出去的，现在不该再有
        assert "speech" not in meta
    finally:
        _timer("reset")


def test_model_written_say_still_wins_over_everything():
    """模型自己写的那句永远优先——它才知道用户这句话的上下文。"""
    try:
        _timer("start", duration_seconds=60, lang="zh")
        meta = _timer("pause", lang="zh", reply="行，先停在这儿。")
        assert meta.get("direct_reply") == "行，先停在这儿。"
    finally:
        _timer("reset")
