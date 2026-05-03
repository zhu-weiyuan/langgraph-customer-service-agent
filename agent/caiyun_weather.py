# -*- coding: utf-8 -*-
"""
彩云天气 API 集成模块

API Token: 从环境变量 CAIYUN_TOKEN 读取，或在下方硬编码。
文档: https://open.caiyunapp.com/%E5%BD%A9%E4%BA%91%E5%A4%A9%E6%B0%94_API_%E4%B8%80%E8%A7%88%E8%A1%A8

接口格式:
  https://api.caiyunapp.com/v2.5/{token}/{coord}/{endpoint}.json

主要端点:
  - realtime.json   实况天气（温度/湿度/风速/气压/AQI，1分钟更新）
  - minutely.json   分钟级降雨（未来2小时逐分钟）
  - hourly.json     小时级预报（15天）
  - daily.json      天级预报（15天 + 生活指数）

参数:
  - dailysteps=15&hourlysteps=360 → 返回完整15天
  - unit=metric:v2 → 降水量用 mm/hr
  - alert=true → 包含天气预警
"""

import sys
import io
import json
import urllib.request
import os
import time

# Windows console UTF-8 fix
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ============================================================
# 配置
# ============================================================

CAIYUN_TOKEN = os.environ.get('CAIYUN_TOKEN', 'lXUrQvNFxHmlWgFa')
API_BASE = "https://api.caiyunapp.com/v2.5"

# 中文城市名 → 经纬度坐标（彩云 API 用坐标查询）
CITY_COORDS = {
    # 国内主要城市
    '北京': '116.4074,39.9042',
    '上海': '121.4737,31.2304',
    '广州': '113.2644,23.1291',
    '深圳': '114.0579,22.5431',
    '成都': '104.0668,30.5728',
    '杭州': '120.1551,30.2741',
    '西安': '108.9398,34.3416',
    '重庆': '106.5516,29.5630',
    '南京': '118.7969,32.0603',
    '武汉': '114.3054,30.5931',
    '长沙': '112.9388,28.2282',
    '昆明': '102.8329,24.8801',
    '厦门': '118.0894,24.4798',
    '青岛': '120.3826,36.0671',
    '大连': '121.6147,38.9140',
    '三亚': '109.5100,18.2528',
    '丽江': '100.2300,26.8661',
    '桂林': '110.2837,25.2721',
    '天津': '117.2009,39.0842',
    '郑州': '113.6253,34.7466',
    '济南': '117.0208,36.6683',
    '哈尔滨': '126.6424,45.7500',
    '沈阳': '123.4328,41.8086',
    '长春': '125.3235,43.8171',
    '石家庄': '114.5149,38.0428',
    '合肥': '117.2272,31.8206',
    '福州': '119.2965,26.0745',
    '南昌': '115.8579,28.6829',
    '贵阳': '106.6302,26.6470',
    '兰州': '103.8343,36.0611',
    '乌鲁木齐': '87.6168,43.8256',
    '拉萨': '91.1409,29.6456',
    '银川': '106.2309,38.4872',
    '西宁': '101.7782,36.6171',
    '呼和浩特': '111.7519,40.8414',
    '太原': '112.5492,37.8573',
    '海口': '110.3497,20.0174',
    '珠海': '113.5768,22.2769',
    '三亚': '109.5100,18.2528',
    '扬州': '119.4327,32.3972',
    '苏州': '120.5972,31.2989',
    '无锡': '120.3064,31.4913',
    '常州': '119.9660,31.8122',
    '南通': '120.8943,31.9709',
    '宁波': '121.5499,29.8683',
    '温州': '120.6993,28.0126',
    '嘉兴': '120.7553,30.7467',
    '绍兴': '120.5823,29.7168',
    '台州': '121.4207,28.6568',
    '金华': '119.6441,29.0784',
    '衢州': '118.8738,28.9556',
    '丽水': '119.9208,28.4510',
    '舟山': '122.2015,29.9853',
    '湖州': '120.0937,30.8806',
    '镇江': '119.4536,32.2044',
    '盐城': '120.1432,33.3782',
    '泰州': '119.9060,32.4742',
    '淮安': '119.0200,33.5847',
    '连云港': '119.1714,34.5962',
    '徐州': '117.1846,34.2540',
    '宿迁': '118.2751,33.9639',
    '承德': '117.9336,40.9591',
    '张家口': '114.8688,40.7587',
    '秦皇岛': '119.6035,39.9208',
    '唐山': '118.0672,39.6243',
    '廊坊': '116.6956,39.5187',
    '保定': '115.4654,38.8739',
    '沧州': '116.8600,38.3037',
    '衡水': '115.6733,37.7334',
    '邢台': '114.4877,37.0620',
    '邯郸': '114.5393,36.6116',
    '大同': '113.3009,40.0975',
    '阳泉': '113.5745,37.8627',
    '长治': '113.1306,36.1920',
    '晋城': '112.8524,35.4896',
    '朔州': '112.4348,39.3248',
    '晋中': '112.7125,37.6876',
    '运城': '111.0127,35.0305',
    '临汾': '111.5138,36.0973',
    '曲靖': '104.2549,25.5001',
    '玉溪': '102.5250,23.1283',
    '保山': '99.1671,25.0378',
    '昭通': '103.7243,27.3350',
    '丽江': '100.2300,26.8661',
    '普洱': '100.9717,22.7761',
    '临沧': '99.9024,23.8923',
    '楚雄': '101.5374,25.0475',
    '红河': '103.3929,23.3717',
    '文山': '104.2476,23.3687',
    '西双版纳': '100.7990,21.9544',
    '大理': '100.2256,25.5833',
    '德宏': '98.5787,24.4362',
    '怒江': '98.8549,25.7863',
    '迪庆': '99.7068,27.8272',
    # 国外热门城市
    '东京': '139.6917,35.6895',
    '大阪': '135.5023,34.6937',
    '京都': '135.7681,35.0116',
    '福冈': '130.4017,33.5903',
    '札幌': '141.3469,43.0621',
    '首尔': '126.9780,37.5665',
    '釜山': '129.0756,35.1796',
    '曼谷': '100.5018,13.7563',
    '清迈': '98.9853,18.7883',
    '普吉岛': '98.3923,7.8804',
    '新加坡': '103.8198,1.3521',
    '吉隆坡': '101.6869,3.1390',
    '雅加达': '106.8451,-6.2088',
    '巴厘岛': '115.2126,-8.4095',
    '巴黎': '2.3522,48.8566',
    '伦敦': '-0.1276,51.5074',
    '纽约': '-74.0060,40.7128',
    '洛杉矶': '-118.2437,34.0522',
    '旧金山': '-122.4194,37.7749',
    '悉尼': '151.2093,-33.8688',
    '墨尔本': '144.9631,-37.8136',
    '迪拜': '55.2708,25.2048',
    '罗马': '12.4964,41.9028',
    '巴塞罗那': '2.1734,41.3851',
    '阿姆斯特丹': '4.9041,52.3676',
    '柏林': '13.4050,52.5200',
    '莫斯科': '37.6173,55.7558',
    '伊斯坦布尔': '28.9784,41.0082',
    '开罗': '31.2357,30.0444',
    ' Cape Town': '18.4241,-33.9249',
    '马尔代夫': '73.5290,4.1755',
    '毛里求斯': '57.5522,-20.1609',
}

# 天气现象代码 → 中文描述 + emoji
SKYCON_MAP = {
    'CLEAR_DAY': ('晴', '☀️'),
    'CLEAR_NIGHT': ('晴', '🌙'),
    'PARTLY_CLOUDY_DAY': ('多云', '⛅'),
    'PARTLY_CLOUDY_NIGHT': ('多云', '☁️'),
    'CLOUDY': ('阴', '☁️'),
    'LIGHT_HAZE': ('轻度雾霾', '🌫️'),
    'MODERATE_HAZE': ('中度雾霾', '🌫️'),
    'HEAVY_HAZE': ('重度雾霾', '😷'),
    'LIGHT_RAIN': ('小雨', '🌧️'),
    'MODERATE_RAIN': ('中雨', '🌧️'),
    'HEAVY_RAIN': ('大雨', '⛈️'),
    'STORM_RAIN': ('暴雨', '⛈️'),
    'FOG': ('雾', '🌫️'),
    'LIGHT_SNOW': ('小雪', '🌨️'),
    'MODERATE_SNOW': ('中雪', '❄️'),
    'HEAVY_SNOW': ('大雪', '❄️'),
    'STORM_SNOW': ('暴雪', '❄️'),
    'DUST': ('浮尘', '🌬️'),
    'SAND': ('沙尘', '🌪️'),
    'WIND': ('大风', '💨'),
}

# 风力等级描述
WIND_LEVEL_MAP = {
    0: '无风', 1: '软风', 2: '轻风', 3: '微风',
    4: '和风', 5: '清风', 6: '强风', 7: '疾风',
    8: '大风', 9: '烈风', 10: '狂风', 11: '暴风',
    12: '飓风',
}

# 生活指数描述
LIFE_INDEX_MAP = {
    # 紫外线
    'ultraviolet0': '无', 'ultraviolet1': '很弱', 'ultraviolet2': '很弱',
    'ultraviolet3': '弱', 'ultraviolet4': '弱', 'ultraviolet5': '中等',
    'ultraviolet6': '中等', 'ultraviolet7': '强', 'ultraviolet8': '强',
    'ultraviolet9': '很强', 'ultraviolet10': '很强', 'ultraviolet11': '极强',
    # 穿衣
    'dressing0': '极热', 'dressing1': '极热', 'dressing2': '很热',
    'dressing3': '热', 'dressing4': '温暖', 'dressing5': '凉爽',
    'dressing6': '冷', 'dressing7': '寒冷', 'dressing8': '极冷',
    # 感冒
    'coldRisk1': '少发', 'coldRisk2': '较易发', 'coldRisk3': '易发', 'coldRisk4': '极易发',
}

# AQI 描述
AQI_MAP = {
    'missing': '缺数据', 'good': '优', 'satisfactory': '良',
    'moderate': '轻度污染', 'unhealthy': '中度污染',
    'very_poor': '重度污染', 'hazardous': '严重污染',
}


# ============================================================
# 核心 API 调用（带缓存 + 限流）
# ============================================================

# Simple in-memory cache: key=(endpoint, coord, params) → (timestamp, data)
_cache = {}
_CACHE_TTL = 60  # seconds


def _request(endpoint: str, coord: str, params: str = '') -> dict:
    """发起彩云 API 请求，带简单缓存和限流重试。"""
    cache_key = (endpoint, coord, params)
    now = time.time()

    # Check cache
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if now - ts < _CACHE_TTL:
            return data

    url = f"{API_BASE}/{CAIYUN_TOKEN}/{coord}/{endpoint}.json"
    if params:
        url += f"?{params}"

    req = urllib.request.Request(url, headers={'User-Agent': 'TravelPlanner/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            # Caiyun returns error=0 on success, or error=None in some cases
            err = data.get('error')
            if err is not None and err != 0:
                print(f"[彩云API] 错误: {err} - {data.get('error_message', '')}")
                return {}
            _cache[cache_key] = (now, data)
            return data
    except urllib.error.HTTPError as e:
        if e.code == 429:
            # Rate limited — wait and retry once
            retry_after = int(e.headers.get('Retry-After', 3))
            print(f"[彩云API] 限流 429，等待 {retry_after}s 后重试...")
            time.sleep(retry_after)
            try:
                req2 = urllib.request.Request(url, headers={'User-Agent': 'TravelPlanner/1.0'})
                with urllib.request.urlopen(req2, timeout=15) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    err2 = data.get('error')
                    if err2 is not None and err2 != 0:
                        return {}
                    _cache[cache_key] = (now, data)
                    return data
            except Exception as e2:
                print(f"[彩云API] 重试仍失败: {e2}")
                return {}
        else:
            print(f"[彩云API] HTTP 错误 ({endpoint}): {e.code} {e.reason}")
            return {}
    except Exception as e:
        print(f"[彩云API] 请求失败 ({endpoint}): {e}")
        return {}


def _resolve_coord(destination: str) -> str:
    """解析目的地为经纬度坐标字符串。"""
    # 直接匹配
    if destination in CITY_COORDS:
        return CITY_COORDS[destination]

    # 模糊匹配（去掉"市"等后缀）
    clean = destination.replace('市', '').replace('省', '').replace('县', '')
    for name, coord in CITY_COORDS.items():
        if clean in name or name in clean:
            return coord

    # 如果已经是坐标格式 "lon,lat"
    if ',' in destination and all(part.replace('.', '').replace('-', '').isdigit() for part in destination.split(',')):
        return destination

    print(f"[彩云天气] 未找到 '{destination}' 的坐标，尝试用 wttr.in 备用")
    return None


# ============================================================
# 公开接口
# ============================================================

def get_realtime(destination: str) -> dict:
    """获取实况天气。"""
    coord = _resolve_coord(destination)
    if not coord:
        return {'error': f'无法解析目的地坐标: {destination}'}

    data = _request('realtime', coord, 'unit=metric:v2')
    if not data:
        return {'error': 'API 请求失败'}

    result = data.get('result', {})
    realtime = result.get('realtime', {})

    temp = realtime.get('temperature', '-')
    humidity = realtime.get('humidity', '-')
    wind_direction = realtime.get('wind_direction', '-')
    wind_speed = realtime.get('wind_speed', '-')
    pressure = realtime.get('pressure', '-')
    cloudrate = realtime.get('cloudrate', '-')
    # precipitation is dict with local/nearest keys
    precip_data = realtime.get('precipitation', {})
    if isinstance(precip_data, dict):
        local_precip = precip_data.get('local', {})
        precipitation = f"{local_precip.get('intensity', 0)} mm/hr" if local_precip else '-'
    else:
        precipitation = precip_data
    aqi_info = realtime.get('aqi', {})
    if isinstance(aqi_info, dict):
        aqi = aqi_info.get('aqi', '-')
        pm25 = aqi_info.get('pm25', '-')
    else:
        aqi = '-'
        pm25 = '-'
    pm25 = realtime.get('aqi', {}).get('pm25', '-')
    skycon = realtime.get('skycon', '')

    weather_name, weather_emoji = SKYCON_MAP.get(skycon, (skycon, '🌤️'))

    # 风力等级（km/h → Beaufort）
    wind_level = '-'
    if isinstance(wind_speed, (int, float)) and wind_speed >= 0:
        wind_kmh = wind_speed * 3.6  # m/s → km/h (if metric:v2)
        level = min(int(wind_kmh // 10) + 1, 12) if wind_kmh > 0 else 0
        wind_level = WIND_LEVEL_MAP.get(level, f'{level}级')

    return {
        'destination': destination,
        'coord': coord,
        'temperature': temp,
        'humidity': f"{int(humidity * 100)}%" if isinstance(humidity, float) else humidity,
        'wind_direction': wind_direction,
        'wind_speed': f"{wind_speed} m/s" if isinstance(wind_speed, (int, float)) else wind_speed,
        'wind_level': wind_level,
        'pressure': f"{pressure} hPa" if isinstance(pressure, (int, float)) else pressure,
        'precipitation': precipitation,
        'cloudrate': f"{int(cloudrate * 100)}%" if isinstance(cloudrate, float) else cloudrate,
        'aqi': aqi,
        'pm25': pm25,
        'weather': weather_name,
        'weather_emoji': weather_emoji,
        'skycon': skycon,
    }


def get_hourly(destination: str, hours: int = 24) -> dict:
    """获取小时级预报。"""
    coord = _resolve_coord(destination)
    if not coord:
        return {'error': f'无法解析目的地坐标: {destination}'}

    steps = min(hours, 360)  # 最多15天
    data = _request('hourly', coord, f'unit=metric:v2&hourlysteps={steps}')
    if not data:
        return {'error': 'API 请求失败'}

    result = data.get('result', {})
    hourly = result.get('hourly', {})
    # temperature is list of {datetime, value}
    hourly_temps = hourly.get('temperature', [])
    skycon_list = hourly.get('skycon', [])

    forecasts = []
    for i in range(min(hours, len(hourly_temps))):
        temp_val = hourly_temps[i]['value'] if i < len(hourly_temps) else '-'
        temp_time = hourly_temps[i].get('datetime', '')[:13] if i < len(hourly_temps) else ''
        sky = skycon_list[i] if i < len(skycon_list) else ''
        name, emoji = SKYCON_MAP.get(sky, (sky, '🌤️'))
        forecasts.append({
            'hour': i,
            'time': temp_time,
            'temperature': temp_val,
            'weather': name,
            'emoji': emoji,
        })

    return {'destination': destination, 'forecasts': forecasts}


def get_daily(destination: str, days: int = 5) -> dict:
    """获取天级预报 + 生活指数。"""
    coord = _resolve_coord(destination)
    if not coord:
        return {'error': f'无法解析目的地坐标: {destination}'}

    steps = min(days, 15)
    # Note: free tier may only return 3-5 days regardless of dailysteps parameter
    data = _request('daily', coord, f'dailysteps={steps}&unit=metric:v2')
    if not data:
        return {'error': 'API 请求失败'}

    result = data.get('result', {})
    daily = result.get('daily', {})

    # Daily temperature: list of {date, max, min, avg}
    temps = daily.get('temperature', [])
    # Skycon: list of {date, value} where value is skycon code string
    skycon_day_raw = daily.get('skycon_08h_20h', [])
    skycon_night_raw = daily.get('skycon_20h_32h', [])

    # 生活指数: life_index.{key} = list of {date, index, desc}
    life_index = daily.get('life_index', {})
    ultraviolet_raw = life_index.get('ultraviolet', [])
    dressing_raw = life_index.get('dressing', [])
    cold_risk_raw = life_index.get('coldRisk', [])

    def _extract_value(lst, i):
        """Extract 'value' or 'index' from list-of-dict API response."""
        if i < len(lst) and isinstance(lst[i], dict):
            return lst[i].get('value', lst[i].get('index', ''))
        return ''

    forecasts = []
    for i in range(steps):
        high = temps[i]['max'] if i < len(temps) else '-'
        low = temps[i]['min'] if i < len(temps) else '-'
        date_str = temps[i].get('date', '')[:10] if i < len(temps) else ''

        day_sky = _extract_value(skycon_day_raw, i)
        night_sky = _extract_value(skycon_night_raw, i)
        day_name, day_emoji = SKYCON_MAP.get(day_sky, (day_sky or '晴', '🌤️'))
        night_name, night_emoji = SKYCON_MAP.get(night_sky, (night_sky or '晴', '🌙'))

        # Life index: use 'desc' field directly if available
        uv_item = ultraviolet_raw[i] if i < len(ultraviolet_raw) else {}
        dress_item = dressing_raw[i] if i < len(dressing_raw) else {}
        cold_item = cold_risk_raw[i] if i < len(cold_risk_raw) else {}

        uv_desc = uv_item.get('desc', '-') if isinstance(uv_item, dict) else '-'
        dress_desc = dress_item.get('desc', '-') if isinstance(dress_item, dict) else '-'
        cold_desc = cold_item.get('desc', '-') if isinstance(cold_item, dict) else '-'

        # Map index codes to descriptions for fallback
        uv_code = uv_item.get('index', '') if isinstance(uv_item, dict) else ''
        dress_code = dress_item.get('index', '') if isinstance(dress_item, dict) else ''
        cold_code = cold_item.get('index', '') if isinstance(cold_item, dict) else ''

        forecasts.append({
            'day': i + 1,
            'date': date_str,
            'temp_high': high,
            'temp_low': low,
            'day_weather': day_name,
            'day_emoji': day_emoji,
            'night_weather': night_name,
            'night_emoji': night_emoji,
            'uv': uv_desc or LIFE_INDEX_MAP.get(f'ultraviolet{uv_code}', '-') if uv_desc or uv_code else '-',
            'dressing': dress_desc or LIFE_INDEX_MAP.get(f'dressing{dress_code}', '-') if dress_desc or dress_code else '-',
            'cold_risk': cold_desc or LIFE_INDEX_MAP.get(f'coldRisk{cold_code}', '-') if cold_desc or cold_code else '-',
        })

    return {'destination': destination, 'forecasts': forecasts}


def get_minutely(destination: str) -> dict:
    """获取分钟级降雨预报（未来2小时）。"""
    coord = _resolve_coord(destination)
    if not coord:
        return {'error': f'无法解析目的地坐标: {destination}'}

    data = _request('minutely', coord, 'unit=metric:v2')
    if not data:
        return {'error': 'API 请求失败'}

    result = data.get('result', {})
    minutely = result.get('minutely', {})
    description = minutely.get('description', '')
    start_time = minutely.get('start_time', '')
    precipitation = minutely.get('precipitation_intensity', [])

    # 简单统计：未来30分钟/1小时的降雨概率
    has_rain_30m = any(p > 0.08 for p in precipitation[:30]) if precipitation else False
    has_rain_60m = any(p > 0.08 for p in precipitation[:60]) if precipitation else False

    return {
        'destination': destination,
        'description': description,
        'start_time': start_time,
        'rain_in_30min': has_rain_30m,
        'rain_in_60min': has_rain_60m,
    }


def get_alerts(destination: str) -> dict:
    """获取天气预警信息。"""
    coord = _resolve_coord(destination)
    if not coord:
        return {'error': f'无法解析目的地坐标: {destination}'}

    data = _request('weather', coord, 'unit=metric:v2&alert=true')
    if not data:
        return {'error': 'API 请求失败'}

    result = data.get('result', {})
    alerts = result.get('alert', [])

    alert_list = []
    for a in alerts:
        alert_list.append({
            'title': a.get('title', ''),
            'description': a.get('description', ''),
            'status': a.get('status', ''),
            'location': a.get('location', ''),
            'code': a.get('code', ''),
            'source': a.get('source', ''),
        })

    return {'destination': destination, 'alerts': alert_list}


# ============================================================
# 格式化输出（用于旅行规划）
# ============================================================

def format_weather_summary(destination: str) -> str:
    """生成适合旅行规划的天气摘要。"""
    lines = []
    lines.append(f"\n{'='*50}")
    lines.append(f"🌤️ {destination} 天气预报（彩云天气）")
    lines.append(f"{'='*50}\n")

    # 实况
    realtime = get_realtime(destination)
    if 'error' not in realtime:
        lines.append(f"**当前天气：** {realtime['weather_emoji']} {realtime['weather']} {realtime['temperature']}°C")
        lines.append(f"**湿度：** {realtime['humidity']}  |  **风力：** {realtime['wind_speed']} {realtime['wind_level']}")
        if realtime.get('aqi') and realtime['aqi'] != '-':
            lines.append(f"**空气质量：** AQI {realtime['aqi']} (PM2.5: {realtime['pm25']})")
        lines.append("")

    # 分钟级降雨
    minutely = get_minutely(destination)
    if 'error' not in minutely and minutely.get('description'):
        rain_icon = '🌧️' if minutely.get('rain_in_30min') else '☀️'
        lines.append(f"**{rain_icon} 短期降雨：** {minutely['description']}")
        if minutely.get('rain_in_30min'):
            lines.append("⚠️ 未来30分钟有雨，建议带好雨具！")
        else:
            lines.append("✅ 近期无降雨，适合出行")
        lines.append("")

    # 天级预报
    daily = get_daily(destination, days=5)
    if 'error' not in daily and daily.get('forecasts'):
        valid_forecasts = [f for f in daily['forecasts'] if f['temp_high'] != '-']
        if valid_forecasts:
            lines.append(f"**未来{len(valid_forecasts)}天预报：**")
            for f in valid_forecasts:
                temp_str = f"{f['temp_low']}° ~ {f['temp_high']}°"
                day_str = f"第{f['day']}天"
                if f['day'] == 1:
                    day_str = "今天"
                elif f['day'] == 2:
                    day_str = "明天"
                lines.append(f"  {day_str} {f['day_emoji']} {f['day_weather']} → {f['night_emoji']} {f['night_weather']}  {temp_str}")
                if f['dressing'] != '-':
                    lines.append(f"    👔 穿衣: {f['dressing']}  |  🤧 感冒风险: {f['cold_risk']}  |  ☀️ 紫外线: {f['uv']}")

    # 预警
    alerts = get_alerts(destination)
    if 'error' not in alerts and alerts.get('alerts'):
        lines.append("")
        lines.append("⚠️ **天气预警：**")
        for a in alerts['alerts']:
            lines.append(f"  🔔 {a['title']}")
            lines.append(f"     {a['description'][:100]}...")

    return '\n'.join(lines)


# ============================================================
# 测试
# ============================================================

if __name__ == '__main__':
    print("=== 彩云天气 API 测试 ===\n")

    # 测试实况
    print("--- 北京实况 ---")
    rt = get_realtime('北京')
    print(json.dumps(rt, ensure_ascii=False, indent=2))

    print("\n--- 东京实况 ---")
    rt_tokyo = get_realtime('东京')
    print(json.dumps(rt_tokyo, ensure_ascii=False, indent=2))

    # 测试分钟级降雨
    print("\n--- 北京降雨预报 ---")
    min_rain = get_minutely('北京')
    print(json.dumps(min_rain, ensure_ascii=False, indent=2))

    # 测试天级预报
    print("\n--- 三亚5天预报 ---")
    daily_sanya = get_daily('三亚', days=5)
    print(json.dumps(daily_sanya, ensure_ascii=False, indent=2))

    # 测试格式化输出
    print("\n" + format_weather_summary('北京'))
