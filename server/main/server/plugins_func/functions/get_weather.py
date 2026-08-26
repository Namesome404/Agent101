import httpx
from bs4 import BeautifulSoup
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from plugins_func.muse_panel import skill_panel
from core.utils.util import get_ip_info
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

GET_WEATHER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "获取某个地点的天气，用户应提供一个位置，比如用户说杭州天气，参数为：杭州。"
            "如果用户说的是省份，默认用省会城市。如果用户说的不是省份或城市而是一个地名，默认用该地所在省份的省会城市。"
            "重要：本地未来7天天气已在上下文中提供，用户未指明其他城市时绝对不要调用此工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "地点名，例如杭州。可选参数，如果不提供则不传",
                },
                "lang": {
                    "type": "string",
                    "description": "返回用户使用的语言code，例如zh_CN/zh_HK/en_US/ja_JP等，默认zh_CN",
                },
            },
            "required": ["lang"],
        },
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36"
    )
}

# 天气代码 https://dev.qweather.com/docs/resource/icons/#weather-icons
WEATHER_CODE_MAP = {
    "100": "晴",
    "101": "多云",
    "102": "少云",
    "103": "晴间多云",
    "104": "阴",
    "150": "晴",
    "151": "多云",
    "152": "少云",
    "153": "晴间多云",
    "300": "阵雨",
    "301": "强阵雨",
    "302": "雷阵雨",
    "303": "强雷阵雨",
    "304": "雷阵雨伴有冰雹",
    "305": "小雨",
    "306": "中雨",
    "307": "大雨",
    "308": "极端降雨",
    "309": "毛毛雨/细雨",
    "310": "暴雨",
    "311": "大暴雨",
    "312": "特大暴雨",
    "313": "冻雨",
    "314": "小到中雨",
    "315": "中到大雨",
    "316": "大到暴雨",
    "317": "暴雨到大暴雨",
    "318": "大暴雨到特大暴雨",
    "350": "阵雨",
    "351": "强阵雨",
    "399": "雨",
    "400": "小雪",
    "401": "中雪",
    "402": "大雪",
    "403": "暴雪",
    "404": "雨夹雪",
    "405": "雨雪天气",
    "406": "阵雨夹雪",
    "407": "阵雪",
    "408": "小到中雪",
    "409": "中到大雪",
    "410": "大到暴雪",
    "456": "阵雨夹雪",
    "457": "阵雪",
    "499": "雪",
    "500": "薄雾",
    "501": "雾",
    "502": "霾",
    "503": "扬沙",
    "504": "浮尘",
    "507": "沙尘暴",
    "508": "强沙尘暴",
    "509": "浓雾",
    "510": "强浓雾",
    "511": "中度霾",
    "512": "重度霾",
    "513": "严重霾",
    "514": "大雾",
    "515": "特强浓雾",
    "900": "热",
    "901": "冷",
    "999": "未知",
}

# Open-Meteo WMO 天气代码 → 中文（免 API Key 备用源）
WMO_WEATHER_ZH = {
    0: "晴",
    1: "晴",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾",
    51: "毛毛雨",
    53: "小雨",
    55: "中雨",
    56: "冻毛毛雨",
    57: "冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "阵雨",
    82: "暴雨",
    85: "阵雪",
    86: "阵雪",
    95: "雷暴",
    96: "雷阵雨",
    99: "雷阵雨伴有冰雹",
}

DEFAULT_QWEATHER_KEY = "a861d0d5e7bf4ee1a83d9a9e4f96d4da"


def _normalize_location(name: str) -> str:
    s = (name or "").strip()
    for suffix in ("天气怎么样", "天气如何", "的天气", "天气", "市", "省"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
    return s.strip() or (name or "").strip()


def _wmo_text(code) -> str:
    try:
        return WMO_WEATHER_ZH.get(int(code), "未知")
    except (TypeError, ValueError):
        return "未知"


def _format_iso_date(iso: str) -> str:
    """2026-07-10 → 07/10"""
    if not iso or len(iso) < 10:
        return iso or ""
    return iso[5:10].replace("-", "/")


def _weather_report_text(city_name, current_abstract, current_basic, temps_list):
    weather_report = f"您查询的位置是：{city_name}\n\n当前天气: {current_abstract}\n"
    if current_basic:
        weather_report += "详细参数：\n"
        for key, value in current_basic.items():
            if value != "0":
                weather_report += f"  · {key}: {value}\n"
    weather_report += "\n未来7天预报：\n"
    for date, weather, high, low in temps_list:
        weather_report += f"{date}: {weather}，气温 {low}~{high}\n"
    weather_report += "\n（如需某一天的具体天气，请告诉我日期）"
    return weather_report


def _unpack_weather_cache(cached):
    if isinstance(cached, dict) and cached.get("report"):
        return cached["report"], cached.get("panel")
    if isinstance(cached, str):
        return cached, None
    return None, None


def _pack_weather_cache(report, panel):
    return {"report": report, "panel": panel}


async def fetch_city_info(location, api_key, api_host):
    url = f"https://{api_host}/geo/v2/city/lookup?key={api_key}&location={location}&lang=zh"
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
        response = await client.get(url, headers=HEADERS)
    data = response.json()
    if data.get("error") is not None:
        logger.bind(tag=TAG).error(
            f"获取天气失败，原因：{data.get('error', {}).get('detail')}"
        )
        return None
    return data.get("location", [])[0] if data.get("location") else None


async def fetch_weather_page(url):
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
        response = await client.get(url, headers=HEADERS)
    return BeautifulSoup(response.text, "html.parser") if response.status_code == 200 else None


def parse_weather_info(soup):
    city_name = soup.select_one("h1.c-submenu__location").get_text(strip=True)

    current_abstract = soup.select_one(".c-city-weather-current .current-abstract")
    current_abstract = (
        current_abstract.get_text(strip=True) if current_abstract else "未知"
    )

    current_basic = {}
    for item in soup.select(
        ".c-city-weather-current .current-basic .current-basic___item"
    ):
        parts = item.get_text(strip=True, separator=" ").split(" ")
        if len(parts) == 2:
            key, value = parts[1], parts[0]
            current_basic[key] = value

    temps_list = []
    for row in soup.select(".city-forecast-tabs__row")[:7]:  # 取前7天的数据
        date = row.select_one(".date-bg .date").get_text(strip=True)
        weather_code = (
            row.select_one(".date-bg .icon")["src"].split("/")[-1].split(".")[0]
        )
        weather = WEATHER_CODE_MAP.get(weather_code, "未知")
        temps = [span.get_text(strip=True) for span in row.select(".tmp-cont .temp")]
        high_temp, low_temp = (temps[0], temps[-1]) if len(temps) >= 2 else (None, None)
        temps_list.append((date, weather, high_temp, low_temp))

    return city_name, current_abstract, current_basic, temps_list


async def fetch_weather_openmeteo(location: str):
    """Open-Meteo 免费天气（无需 API Key），适合国内城市。"""
    query = _normalize_location(location)
    if not query:
        return None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0)) as client:
            geo_resp = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={
                    "name": query,
                    "count": 1,
                    "language": "zh",
                    "format": "json",
                },
                headers=HEADERS,
            )
            geo_data = geo_resp.json()
            results = geo_data.get("results") or []
            if not results:
                return None

            place = results[0]
            lat = place["latitude"]
            lon = place["longitude"]
            city_name = place.get("name") or query
            admin1 = place.get("admin1") or ""
            if admin1 and admin1 not in city_name:
                display_name = f"{city_name}（{admin1}）"
            else:
                display_name = city_name

            wx_resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": (
                        "temperature_2m,relative_humidity_2m,weather_code,"
                        "wind_speed_10m,apparent_temperature"
                    ),
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "timezone": "Asia/Shanghai",
                    "forecast_days": 7,
                },
                headers=HEADERS,
            )
            wx = wx_resp.json()
    except Exception as e:
        logger.bind(tag=TAG).warning(f"Open-Meteo 天气获取失败: {e}")
        return None

    current = wx.get("current") or {}
    daily = wx.get("daily") or {}
    cur_code = current.get("weather_code", 0)
    cur_desc = _wmo_text(cur_code)
    temp_c = current.get("temperature_2m")
    feels = current.get("apparent_temperature")
    humidity = current.get("relative_humidity_2m")
    wind = current.get("wind_speed_10m")

    temp_str = f"{int(round(temp_c))}°C" if temp_c is not None else ""
    current_abstract = f"{cur_desc}，{temp_str}" if temp_str else cur_desc

    current_basic = {}
    if temp_c is not None:
        current_basic["温度"] = temp_str
    if feels is not None:
        current_basic["体感"] = f"{int(round(feels))}°C"
    if humidity is not None:
        current_basic["湿度"] = f"{int(round(humidity))}%"
    if wind is not None:
        current_basic["风速"] = f"{int(round(wind))} km/h"

    temps_list = []
    dates = daily.get("time") or []
    codes = daily.get("weather_code") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    for i, iso in enumerate(dates[:7]):
        code = codes[i] if i < len(codes) else 0
        hi = highs[i] if i < len(highs) else None
        lo = lows[i] if i < len(lows) else None
        hi_s = str(int(round(hi))) if hi is not None else ""
        lo_s = str(int(round(lo))) if lo is not None else ""
        temps_list.append(
            (_format_iso_date(iso), _wmo_text(code), hi_s, lo_s)
        )

    return display_name, current_abstract, current_basic, temps_list


async def fetch_weather_qweather(location, api_key, api_host):
    """和风天气：需有效 api_key / api_host。"""
    city_info = await fetch_city_info(location, api_key, api_host)
    if not city_info:
        return None
    soup = await fetch_weather_page(city_info["fxLink"])
    if not soup:
        return None
    try:
        return parse_weather_info(soup)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"和风天气页面解析失败: {e}")
        return None


def _weather_panel_payload(city_name, current_abstract, current_basic, temps_list):
    temp = ""
    for key, value in (current_basic or {}).items():
        if "温度" in key or key.lower() == "temp":
            temp = value
            break
    condition = (current_abstract or "").split("，")[0].split(",")[0].strip()
    forecast = []
    for date, weather, high, low in temps_list or []:
        forecast.append(
            {
                "date": date,
                "weather": weather,
                "high": high,
                "low": low,
            }
        )
    details = {
        k: v for k, v in (current_basic or {}).items() if v and str(v) != "0"
    }
    return skill_panel(
        "weather",
        f"{city_name}天气",
        data={
            "city": city_name,
            "condition": condition,
            "current": current_abstract,
            "temp": temp,
            "subtitle": current_abstract,
            "details": details,
            "forecast": forecast,
        },
        width=420,
        height=480,
    )


@register_function("get_weather", GET_WEATHER_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def get_weather(conn: "ConnectionHandler", location: str = None, lang: str = "zh_CN"):
    from core.utils.cache.manager import cache_manager, CacheType

    weather_config = conn.config.get("plugins", {}).get("get_weather", {})
    default_location = weather_config.get("default_location", "广州")
    client_ip = conn.client_ip

    # 优先使用用户提供的location参数
    if not location:
        # 通过客户端IP解析城市
        if client_ip:
            # 先从缓存获取IP对应的城市信息
            cached_ip_info = cache_manager.get(CacheType.IP_INFO, client_ip)
            if cached_ip_info:
                location = cached_ip_info.get("city")
            else:
                # 缓存未命中，调用API获取
                ip_info = get_ip_info(client_ip, logger)
                if ip_info:
                    cache_manager.set(CacheType.IP_INFO, client_ip, ip_info)
                    location = ip_info.get("city")

            if not location:
                location = default_location
        else:
            # 若无IP，使用默认位置
            location = default_location
    location = _normalize_location(location)

    # 尝试从缓存获取完整天气报告
    weather_cache_key = f"full_weather_{location}_{lang}"
    cached = cache_manager.get(CacheType.WEATHER, weather_cache_key)
    if cached:
        report, panel = _unpack_weather_cache(cached)
        if report:
            if isinstance(panel, dict) and not panel.get("content"):
                panel = {**panel, "content": report}
            return ActionResponse(Action.REQLLM, report, None, panel=panel)

    provider = (weather_config.get("provider") or "auto").lower()
    use_qweather = provider in ("qweather", "auto", "hefeng")
    api_key = (weather_config.get("api_key") or "").strip()
    api_host = (weather_config.get("api_host") or "").strip()
    if not api_key or api_key == DEFAULT_QWEATHER_KEY:
        use_qweather = provider == "qweather"

    parsed = None
    if use_qweather and api_key and api_host:
        parsed = await fetch_weather_qweather(location, api_key, api_host)

    if not parsed:
        if use_qweather and api_key:
            logger.bind(tag=TAG).info(
                f"和风天气不可用，切换 Open-Meteo 备用源: {location}"
            )
        parsed = await fetch_weather_openmeteo(location)

    if not parsed:
        return ActionResponse(
            Action.REQLLM, f"未找到相关的城市: {location}，请确认地点是否正确", None
        )

    city_name, current_abstract, current_basic, temps_list = parsed
    weather_report = _weather_report_text(
        city_name, current_abstract, current_basic, temps_list
    )
    panel = _weather_panel_payload(
        city_name, current_abstract, current_basic, temps_list
    )
    panel["content"] = weather_report

    cache_manager.set(
        CacheType.WEATHER, weather_cache_key, _pack_weather_cache(weather_report, panel)
    )
    if hasattr(conn, "__dict__"):
        conn.last_weather_panel = panel

    return ActionResponse(Action.REQLLM, weather_report, None, panel=panel)
