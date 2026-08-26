"""Curated voice choices for TTS providers used by the EV settings UI."""
from __future__ import annotations


DOUBAO_CLASSIC_VOICES = [
    ("BV001_streaming", "通用女声 · 女 · 免费可用"),
    ("BV002_streaming", "通用男声 · 男 · 免费可用"),
    ("BV056_streaming", "阳光男声 · 男"),
    ("BV064_streaming", "小萝莉 · 女"),
    ("BV405_streaming", "甜美小源 · 女"),
]


HUOSHAN_TTS1_VOICES = [
    ("zh_female_wanwanxiaohe_moon_bigtts", "湾湾小何 · 女 · 台普甜美"),
    ("zh_female_vv_mars_bigtts", "Vivi · 女 · 活泼灵动"),
    ("zh_female_tianmeitaozi_mars_bigtts", "甜美桃子 · 女 · 温柔"),
    ("zh_female_gujie_mars_bigtts", "顾姐 · 女 · 御姐烟嗓"),
    ("zh_female_qinqienvsheng_moon_bigtts", "亲切女声 · 女 · 亲和"),
    ("zh_female_wenrouxiaoya_moon_bigtts", "温柔小雅 · 女 · 温柔"),
    ("zh_female_tianmeixiaoyuan_moon_bigtts", "甜美小源 · 女 · 甜美"),
    ("zh_female_qingchezizi_moon_bigtts", "清澈梓梓 · 女 · 清澈"),
    ("zh_female_kailangjiejie_moon_bigtts", "开朗姐姐 · 女 · 开朗"),
    ("zh_female_linjianvhai_moon_bigtts", "邻家女孩 · 女 · 自然"),
    ("zh_female_gaolengyujie_moon_bigtts", "高冷御姐 · 女 · 成熟"),
    ("zh_female_shuangkuaisisi_moon_bigtts", "爽快思思 · 女 · 爽朗"),
    ("zh_female_sajiaonvyou_moon_bigtts", "柔美女友 · 女 · 柔美"),
    ("zh_female_yuanqinvyou_moon_bigtts", "撒娇学妹 · 女 · 元气"),
    ("zh_male_shaonianzixin_moon_bigtts", "少年梓辛 · 男 · 少年感"),
    ("zh_male_yuanboxiaoshu_moon_bigtts", "渊博小叔 · 男 · 沉稳"),
    ("zh_male_yangguangqingnian_moon_bigtts", "阳光青年 · 男 · 阳光"),
    ("zh_male_dongfanghaoran_moon_bigtts", "东方浩然 · 男 · 大气"),
    ("zh_male_jieshuoxiaoming_moon_bigtts", "解说小明 · 男 · 解说"),
    ("zh_male_linjiananhai_moon_bigtts", "邻家男孩 · 男 · 自然"),
    ("zh_male_wennuanahu_moon_bigtts", "温暖阿虎 · 男 · 温暖"),
    ("zh_male_aojiaobazong_moon_bigtts", "傲娇霸总 · 男 · 角色"),
    ("zh_male_shenyeboke_moon_bigtts", "深夜播客 · 男 · 低沉"),
    ("zh_male_beijingxiaoye_moon_bigtts", "北京小爷 · 男 · 京腔"),
    ("zh_female_daimengchuanmei_moon_bigtts", "呆萌川妹 · 女 · 川味"),
]


HUOSHAN_TTS2_VOICES = [
    ("zh_female_xiaohe_uranus_bigtts", "小何 2.0 · 女 · 台普甜美"),
    ("zh_female_vv_uranus_bigtts", "Vivi 2.0 · 女 · 活泼灵动"),
    ("zh_male_xiaotian_uranus_bigtts", "小天 2.0 · 男 · 清爽磁性"),
    ("zh_male_yunzhou_uranus_bigtts", "云舟 2.0 · 男 · 清爽沉稳"),
    ("zh_female_xueayi_saturn_bigtts", "儿童绘本 · 女 · 有声阅读"),
    ("zh_male_dayi_saturn_bigtts", "大壹 · 男 · 视频配音"),
    ("zh_female_mizai_saturn_bigtts", "咪仔 · 女 · 视频配音"),
    ("zh_female_jitangnv_saturn_bigtts", "鸡汤女 · 女 · 视频配音"),
    ("zh_female_meilinvyou_saturn_bigtts", "魅力女友 · 女 · 视频配音"),
    ("zh_female_santongyongns_saturn_bigtts", "流畅女声 · 女 · 视频配音"),
    ("zh_male_ruyayichen_saturn_bigtts", "儒雅逸辰 · 男 · 视频配音"),
    ("ICL_zh_female_keainvsheng_tob", "可爱女生 · 女 · 角色扮演"),
    ("ICL_zh_female_tiaopigongzhu_tob", "调皮公主 · 女 · 角色扮演"),
    ("ICL_zh_male_shuanglangshaonian_tob", "爽朗少年 · 男 · 角色扮演"),
    ("ICL_zh_male_tiancaitongzhuo_tob", "天才同桌 · 男 · 角色扮演"),
]


def catalog_for_tts(provider_type: str, block: dict) -> dict | None:
    """Return a list-mode descriptor for providers with known voice IDs."""
    provider_type = str(provider_type or "").lower()
    block = block or {}
    resource_id = str(block.get("resource_id") or "").lower()
    current_voice = str(
        block.get("private_voice")
        or block.get("speaker")
        or block.get("voice")
        or ""
    )

    if provider_type == "huoshan_double_stream":
        voices = (
            HUOSHAN_TTS2_VOICES
            if "seed-tts-2.0" in resource_id
            else HUOSHAN_TTS1_VOICES
        )
        return {
            "voiceKey": "speaker",
            "current": current_voice,
            "voices": voices,
        }

    if provider_type == "doubao":
        voices = (
            DOUBAO_CLASSIC_VOICES
            if current_voice.startswith("BV")
            else HUOSHAN_TTS1_VOICES
        )
        return {
            "voiceKey": "voice",
            "current": current_voice,
            "voices": voices,
        }

    api_url = str(block.get("api_url") or block.get("url") or "").lower()
    model = str(block.get("model") or "").lower()
    if provider_type == "openai" and (
        "volcengine" in api_url or model == "doubao-tts"
    ):
        return {
            "voiceKey": "voice",
            "current": current_voice,
            "voices": HUOSHAN_TTS1_VOICES,
        }

    return None
