#!/usr/bin/env python3
"""Flight price monitor focused on safe link generation and email alerts.

This is not a ticket-buying bot. It never orders tickets, stores payment data,
logs in, bypasses CAPTCHAs, or sends high-frequency requests. The default
sources generate human-checkable search links. Optional price fetching is a
stub by design unless you add a compliant API or site-specific adapter.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import smtplib
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import yaml
import requests


STATE_FILE = "flight_price_state.json"
FORBIDDEN_ORIGINS = {"OSA", "KIX", "ITM", "UKB"}
ALLOWED_TOKYO_ORIGINS = {"TYO", "HND", "NRT"}
MANUAL_ONLY_SOURCES = {"google_flights", "skyscanner", "trip_com", "ctrip", "fliggy", "airline_official"}
AMADEUS_TOKEN_CACHE: dict[str, Any] = {}
AMADEUS_REQUEST_COUNT = 0
TRAVELPAYOUTS_REQUEST_COUNT = 0
WEEKLY_GROUPS = [
    ("核心路线：东京-西安", ["Core China"]),
    ("中国大陆 / 港澳台低价", ["China / HK / Taiwan"]),
    ("东南亚低价", ["Southeast Asia"]),
    ("东北亚低价", ["Northeast Asia"]),
    ("日本国内低价", ["Domestic Japan"]),
    ("海岛 / 度假低价", ["Islands"]),
    ("欧洲低价", ["Europe"]),
    ("北美低价", ["North America"]),
    ("澳洲 / 新西兰低价", ["Oceania"]),
    ("中东 / 中亚低价", ["Middle East / Central Asia"]),
]


CATEGORY_LABELS_ZH = {
    "Core China": "核心路线 / 中国大陆",
    "China / HK / Taiwan": "中国大陆 / 港澳台",
    "Southeast Asia": "东南亚",
    "Northeast Asia": "东北亚",
    "Domestic Japan": "日本国内",
    "Islands": "海岛 / 度假",
    "Europe": "欧洲",
    "North America": "北美",
    "Oceania": "澳洲 / 新西兰",
    "Middle East / Central Asia": "中东 / 中亚",
}

AIRPORT_LABELS_ZH = {
    "TYO": "东京全部机场",
    "HND": "东京羽田",
    "NRT": "东京成田",
    "XIY": "西安",
    "CTS": "札幌 / 新千岁",
    "OKA": "冲绳 / 那霸",
    "FUK": "福冈",
    "KMJ": "熊本",
    "KOJ": "鹿儿岛",
    "NGS": "长崎",
    "KMI": "宫崎",
    "OIT": "大分",
    "KKJ": "北九州",
    "ISG": "石垣岛",
    "MMY": "宫古岛",
    "ASJ": "奄美大岛",
    "ICN": "首尔 / 仁川",
    "GMP": "首尔 / 金浦",
    "PUS": "釜山",
    "CJU": "济州",
    "UBN": "乌兰巴托",
    "PVG": "上海 / 浦东",
    "SHA": "上海 / 虹桥",
    "PEK": "北京 / 首都",
    "PKX": "北京 / 大兴",
    "DLC": "大连",
    "TAO": "青岛",
    "NKG": "南京",
    "HGH": "杭州",
    "CAN": "广州",
    "SZX": "深圳",
    "TFU": "成都 / 天府",
    "CTU": "成都 / 双流",
    "CKG": "重庆",
    "XMN": "厦门",
    "FOC": "福州",
    "WUH": "武汉",
    "HKG": "香港",
    "MFM": "澳门",
    "TPE": "台北 / 桃园",
    "TSA": "台北 / 松山",
    "KHH": "高雄",
    "BKK": "曼谷 / 素万那普",
    "DMK": "曼谷 / 廊曼",
    "SIN": "新加坡",
    "KUL": "吉隆坡",
    "MNL": "马尼拉",
    "CEB": "宿务",
    "HAN": "河内",
    "SGN": "胡志明市",
    "DAD": "岘港",
    "CGK": "雅加达",
    "DPS": "巴厘岛",
    "PNH": "金边",
    "SAI": "暹粒",
    "HNL": "檀香山",
    "GUM": "关岛",
    "SPN": "塞班",
    "ROR": "帕劳",
    "HKT": "普吉岛",
    "MLE": "马累 / 马尔代夫",
    "PPT": "大溪地 / 帕皮提",
    "LHR": "伦敦 / 希思罗",
    "CDG": "巴黎 / 戴高乐",
    "FRA": "法兰克福",
    "MUC": "慕尼黑",
    "AMS": "阿姆斯特丹",
    "FCO": "罗马",
    "MXP": "米兰",
    "BCN": "巴塞罗那",
    "MAD": "马德里",
    "ZRH": "苏黎世",
    "VIE": "维也纳",
    "IST": "伊斯坦布尔",
    "HEL": "赫尔辛基",
    "CPH": "哥本哈根",
    "PRG": "布拉格",
    "BUD": "布达佩斯",
    "WAW": "华沙",
    "ATH": "雅典",
    "LAX": "洛杉矶",
    "SFO": "旧金山",
    "SEA": "西雅图",
    "YVR": "温哥华",
    "JFK": "纽约 / 肯尼迪",
    "EWR": "纽约 / 纽瓦克",
    "YYZ": "多伦多",
    "ORD": "芝加哥",
    "LAS": "拉斯维加斯",
    "SYD": "悉尼",
    "MEL": "墨尔本",
    "BNE": "布里斯班",
    "PER": "珀斯",
    "AKL": "奥克兰",
    "CHC": "基督城",
    "DXB": "迪拜",
    "AUH": "阿布扎比",
    "DOH": "多哈",
    "TAS": "塔什干",
    "ALA": "阿拉木图",
}

ROUTE_SUFFIX_LABELS_ZH = {
    "Xian": "西安",
    "Sapporo": "札幌",
    "Okinawa": "冲绳",
    "Fukuoka": "福冈",
    "Kumamoto": "熊本",
    "Kagoshima": "鹿儿岛",
    "Nagasaki": "长崎",
    "Miyazaki": "宫崎",
    "Oita": "大分",
    "Kitakyushu": "北九州",
    "Ishigaki": "石垣岛",
    "Miyakojima": "宫古岛",
    "Amami": "奄美大岛",
    "Seoul": "首尔",
    "Busan": "釜山",
    "Jeju": "济州",
    "Ulaanbaatar": "乌兰巴托",
    "Shanghai": "上海",
    "Beijing": "北京",
    "Dalian": "大连",
    "Qingdao": "青岛",
    "Nanjing": "南京",
    "Hangzhou": "杭州",
    "Guangzhou": "广州",
    "Shenzhen": "深圳",
    "Chengdu": "成都",
    "Chongqing": "重庆",
    "Xiamen": "厦门",
    "Fuzhou": "福州",
    "Wuhan": "武汉",
    "HongKong": "香港",
    "Macau": "澳门",
    "Taipei": "台北",
    "Kaohsiung": "高雄",
    "Bangkok": "曼谷",
    "Singapore": "新加坡",
    "KualaLumpur": "吉隆坡",
    "Manila": "马尼拉",
    "Cebu": "宿务",
    "Hanoi": "河内",
    "HoChiMinh": "胡志明市",
    "DaNang": "岘港",
    "Jakarta": "雅加达",
    "Bali": "巴厘岛",
    "PhnomPenh": "金边",
    "SiemReap": "暹粒",
    "Honolulu": "檀香山",
    "Guam": "关岛",
    "Saipan": "塞班",
    "Palau": "帕劳",
    "Phuket": "普吉岛",
    "Maldives": "马尔代夫",
    "Tahiti": "大溪地",
    "London": "伦敦",
    "Paris": "巴黎",
    "Frankfurt": "法兰克福",
    "Munich": "慕尼黑",
    "Amsterdam": "阿姆斯特丹",
    "Rome": "罗马",
    "Milan": "米兰",
    "Barcelona": "巴塞罗那",
    "Madrid": "马德里",
    "Zurich": "苏黎世",
    "Vienna": "维也纳",
    "Istanbul": "伊斯坦布尔",
    "Helsinki": "赫尔辛基",
    "Copenhagen": "哥本哈根",
    "Prague": "布拉格",
    "Budapest": "布达佩斯",
    "Warsaw": "华沙",
    "Athens": "雅典",
    "LosAngeles": "洛杉矶",
    "SanFrancisco": "旧金山",
    "Seattle": "西雅图",
    "Vancouver": "温哥华",
    "NewYork": "纽约",
    "Toronto": "多伦多",
    "Chicago": "芝加哥",
    "LasVegas": "拉斯维加斯",
    "Sydney": "悉尼",
    "Melbourne": "墨尔本",
    "Brisbane": "布里斯班",
    "Perth": "珀斯",
    "Auckland": "奥克兰",
    "Christchurch": "基督城",
    "Dubai": "迪拜",
    "AbuDhabi": "阿布扎比",
    "Doha": "多哈",
    "Tashkent": "塔什干",
    "Almaty": "阿拉木图",
}

SOURCE_LABELS_ZH = {
    "travelpayouts": "Travelpayouts / Aviasales 缓存价",
    "amadeus": "Amadeus 实时报价",
    "google_flights": "Google Flights 人工确认链接",
    "skyscanner": "Skyscanner 人工确认链接",
    "trip_com": "Trip.com 人工确认链接",
    "ctrip": "携程人工确认链接",
    "fliggy": "飞猪人工确认链接",
    "airline_official": "航司官网人工确认链接",
}

PRICE_MODE_LABELS_ZH = {
    "exact_date": "精确日期缓存报价",
    "flexible_cached": "灵活日期缓存低价",
    "manual": "人工确认",
}


def category_label_zh(category: str | None) -> str:
    if not category:
        return "未知分类"
    return CATEGORY_LABELS_ZH.get(str(category), str(category))


def airport_label_zh(code: str | None) -> str:
    if not code:
        return "-"
    code_str = str(code)
    name = AIRPORT_LABELS_ZH.get(code_str)
    return f"{name}（{code_str}）" if name else code_str


def route_label_zh(route_name: str | None, candidate: "SearchCandidate | None" = None) -> str:
    if not route_name:
        return "未知路线"
    route = str(route_name)
    if route.startswith("Tokyo-"):
        suffix = route.split("Tokyo-", 1)[1]
        dest_name = ROUTE_SUFFIX_LABELS_ZH.get(suffix)
        if not dest_name and candidate is not None:
            dest_name = AIRPORT_LABELS_ZH.get(candidate.destination)
        if dest_name:
            return f"东京-{dest_name}"
    return route


def source_label_zh(source_name: str | None) -> str:
    if not source_name:
        return "未知平台"
    source = str(source_name)
    if source.startswith("airline_official:"):
        airline = source.split(":", 1)[1]
        return f"航司官网人工确认链接（{airline}）"
    return SOURCE_LABELS_ZH.get(source, source)


def price_mode_label_zh(price_mode: str | None) -> str:
    if not price_mode:
        return "未知模式"
    return PRICE_MODE_LABELS_ZH.get(str(price_mode), str(price_mode))


def bool_label_zh(value: Any) -> str:
    return "是" if bool(value) else "否"




@dataclass
class SearchCandidate:
    route_name: str
    destination_category: str
    origin: str
    destination: str
    depart_date: str
    return_date: str | None
    trip_type: str
    threshold_jpy: int
    window_key: str = "normal"
    window_label: str = "normal"
    is_core_route: bool = False
    route_config: dict[str, Any] = field(default_factory=dict)

    @property
    def key_base(self) -> str:
        return "|".join(
            [
                self.route_name,
                self.origin,
                self.destination,
                self.depart_date,
                self.return_date or "",
                self.trip_type,
                self.window_key,
            ]
        )

    @property
    def alert_group_key(self) -> str:
        return "|".join(
            [
                self.route_name,
                self.destination,
                self.depart_date,
                self.return_date or "",
                self.trip_type,
                self.window_key,
            ]
        )


@dataclass
class SourceResult:
    candidate: SearchCandidate
    source_name: str
    query_link: str
    price_jpy: int | None = None
    status: str = "manual_check_required"
    message: str = "需人工确认"
    price_mode: str = "manual"
    original_depart_date: str | None = None
    original_return_date: str | None = None
    stops: int | None = None
    stops_status: str = "manual_check_required"
    filtered_by_stops: bool = False

    def __post_init__(self) -> None:
        if self.original_depart_date is None:
            self.original_depart_date = self.candidate.depart_date
        if self.original_return_date is None:
            self.original_return_date = self.candidate.return_date

    @property
    def key(self) -> str:
        return f"{self.candidate.key_base}|{self.source_name}"

    @property
    def alert_key(self) -> str:
        return f"{self.candidate.alert_group_key}|{self.source_name}|{self.price_jpy or 'manual'}|{self.price_mode}"


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    for section in ("core_routes", "domestic_routes", "global_routes"):
        for route in config.get(section, []) or []:
            origins = set(route.get("origin_codes", []))
            forbidden = sorted(origins & FORBIDDEN_ORIGINS)
            if forbidden:
                raise ValueError(f"{route.get('name')} contains forbidden Osaka origin(s): {forbidden}")
            if not origins <= ALLOWED_TOKYO_ORIGINS:
                raise ValueError(f"{route.get('name')} must only use Tokyo origins: {sorted(origins)}")


def load_state(path: str | Path = STATE_FILE) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {
            "version": 1,
            "runs": {},
            "prices": {},
            "alerts": {},
            "history_lows": {},
            "source_status": {},
            "latest_links": {},
            "latest_prices": {},
            "weekly_drops": [],
            "manual_check_links": [],
        }
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict[str, Any], path: str | Path = STATE_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def prune_state(state: dict[str, Any], keep_days: int = 30, dedup_days: int = 7) -> dict[str, Any]:
    today = dt.date.today()
    cutoff = (today - dt.timedelta(days=keep_days)).isoformat()
    alert_cutoff = (today - dt.timedelta(days=max(keep_days, dedup_days * 4))).isoformat()

    def recent_items(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        filtered = [item for item in items if str(item.get("date", "")) >= cutoff]
        return filtered[-limit:]

    state["manual_check_links"] = recent_items(state.get("manual_check_links", []), 1000)
    state["weekly_drops"] = recent_items(state.get("weekly_drops", []), 500)

    alerts = state.get("alerts", {})
    if isinstance(alerts, dict):
        state["alerts"] = {
            key: value
            for key, value in alerts.items()
            if not isinstance(value, dict) or str(value.get("date", "")) >= alert_cutoff
        }
    return state


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(str(value))


def configured_max_stops(config: dict[str, Any]) -> int:
    settings = config.get("settings", {})
    if settings.get("direct_only", False):
        return 0
    return int(settings.get("max_stops", 1))


def allow_unknown_stops(config: dict[str, Any]) -> bool:
    return bool(config.get("settings", {}).get("allow_unknown_stops", True))


def direct_only_enabled(config: dict[str, Any]) -> bool:
    return configured_max_stops(config) == 0


def stop_label(stops: int | None) -> str:
    if stops is None:
        return "无法确认"
    if stops == 0:
        return "直飞"
    return f"{stops} 次转机"


def stops_status_label(status: str) -> str:
    return {
        "confirmed": "已确认",
        "unknown": "无法确认",
        "manual_check_required": "需人工确认",
    }.get(status, status)


def add_days(value: dt.date, days: int) -> str:
    return (value + dt.timedelta(days=days)).isoformat()


def default_regular_trips(today: dt.date, trip_type: str) -> list[dict[str, str]]:
    offsets = [30, 45, 60, 90]
    if trip_type == "oneway":
        return [{"depart": add_days(today, offset)} for offset in offsets]
    return [{"depart": add_days(today, offset), "return": add_days(today, offset + 5)} for offset in offsets]


def threshold_for_route(route: dict[str, Any], window_key: str) -> int:
    if route.get("destination_category") == "Core China":
        mapping = {
            "golden_week": "golden_week_threshold_jpy",
            "year_end": "year_end_threshold_jpy",
            "spring_festival": "spring_festival_threshold_jpy",
        }
        return int(route.get(mapping.get(window_key, "normal_threshold_jpy"), route.get("normal_threshold_jpy", 40000)))
    return int(route.get("threshold_jpy", route.get("normal_threshold_jpy", 999999)))


def enabled_date_window_trips(config: dict[str, Any]) -> list[tuple[str, str, dict[str, str]]]:
    trips: list[tuple[str, str, dict[str, str]]] = []
    for key, window in (config.get("date_windows") or {}).items():
        if window.get("enabled", True) is False:
            continue
        label = window.get("label", key)
        for trip in window.get("candidate_trips") or []:
            if trip.get("depart"):
                trips.append((key, label, trip))
    return trips


def generate_candidate_searches(
    config: dict[str, Any],
    *,
    core_only: bool = False,
    domestic_only: bool = False,
    global_only: bool = False,
) -> list[SearchCandidate]:
    today = dt.date.today()
    candidates: list[SearchCandidate] = []

    route_groups: list[tuple[str, list[dict[str, Any]]]] = []
    if core_only:
        route_groups.append(("core", config.get("core_routes", [])))
    elif domestic_only:
        route_groups.append(("domestic", config.get("domestic_routes", [])))
    elif global_only:
        route_groups.append(("global", config.get("global_routes", [])))
    else:
        route_groups.extend(
            [
                ("core", config.get("core_routes", [])),
                ("domestic", config.get("domestic_routes", [])),
                ("global", config.get("global_routes", [])),
            ]
        )

    window_trips = enabled_date_window_trips(config)
    for group_name, routes in route_groups:
        for route in routes or []:
            origins = route.get("origin_codes", [])
            destinations = route.get("destination_codes", [])
            trip_type = route.get("trip_type", "roundtrip")
            is_core = group_name == "core" or route.get("destination_category") == "Core China"

            trips: list[tuple[str, str, dict[str, str]]]
            if is_core:
                trips = window_trips + [("normal", "普通时期", t) for t in default_regular_trips(today, trip_type)]
            else:
                trips = [("normal", "普通时期", t) for t in default_regular_trips(today, trip_type)]

            for origin in origins:
                for destination in destinations:
                    for window_key, window_label, trip in trips:
                        candidates.append(
                            SearchCandidate(
                                route_name=route["name"],
                                destination_category=route.get("destination_category", "Unknown"),
                                origin=origin,
                                destination=destination,
                                depart_date=str(trip["depart"]),
                                return_date=str(trip.get("return")) if trip_type == "roundtrip" else None,
                                trip_type=trip_type,
                                threshold_jpy=threshold_for_route(route, window_key),
                                window_key=window_key,
                                window_label=window_label,
                                is_core_route=is_core,
                                route_config=route,
                            )
                        )
    return candidates


def build_google_flights_link(c: SearchCandidate, config: dict[str, Any] | None = None) -> str:
    q = f"{c.origin} to {c.destination} {c.depart_date}"
    if c.return_date:
        q += f" returning {c.return_date}"
    if config and direct_only_enabled(config):
        q += " nonstop"
    return "https://www.google.com/travel/flights?" + urlencode({"q": q, "curr": "JPY"})


def build_skyscanner_link(c: SearchCandidate) -> str:
    origin = c.origin.lower()
    destination = c.destination.lower()
    depart = c.depart_date.replace("-", "")
    if c.trip_type == "oneway":
        return f"https://www.skyscanner.jp/transport/flights/{origin}/{destination}/{depart}/?currency=JPY"
    ret = (c.return_date or "").replace("-", "")
    return f"https://www.skyscanner.jp/transport/flights/{origin}/{destination}/{depart}/{ret}/?currency=JPY"


def build_tripcom_link(c: SearchCandidate) -> str:
    params = {
        "dcity": c.origin,
        "acity": c.destination,
        "ddate": c.depart_date,
        "triptype": "ow" if c.trip_type == "oneway" else "rt",
        "curr": "JPY",
    }
    if c.return_date:
        params["rdate"] = c.return_date
    return "https://www.trip.com/flights/search/?" + urlencode(params)


def build_ctrip_link(c: SearchCandidate) -> str:
    params = {
        "dcity": c.origin,
        "acity": c.destination,
        "ddate": c.depart_date,
        "triptype": "ow" if c.trip_type == "oneway" else "rt",
    }
    if c.return_date:
        params["rdate"] = c.return_date
    return "https://flights.ctrip.com/online/list/oneway-" + quote(c.origin) + "-" + quote(c.destination) + "?" + urlencode(params)


def build_fliggy_link(c: SearchCandidate) -> str:
    params = {
        "depCity": c.origin,
        "arrCity": c.destination,
        "depDate": c.depart_date,
        "tripType": "oneway" if c.trip_type == "oneway" else "roundtrip",
    }
    if c.return_date:
        params["retDate"] = c.return_date
    return "https://sjipiao.fliggy.com/flight_search_result.htm?" + urlencode(params)


def build_airline_links(c: SearchCandidate) -> dict[str, str]:
    query = quote(f"{c.origin} {c.destination} {c.depart_date} {c.return_date or ''}".strip())
    airlines = {
        "ANA": "https://www.ana.co.jp/en/jp/",
        "JAL": "https://www.jal.co.jp/jp/en/",
        "Peach": "https://www.flypeach.com/en",
        "Jetstar Japan": "https://www.jetstar.com/jp/en/home",
        "Spring Japan": "https://springjapan.com/",
        "Air China": "https://www.airchina.jp/",
        "China Eastern": "https://www.ceair.com/",
        "China Southern": "https://www.csair.com/",
        "Hainan Airlines": "https://www.hainanairlines.com/",
        "Cathay Pacific": "https://www.cathaypacific.com/",
        "Korean Air": "https://www.koreanair.com/",
        "Asiana": "https://flyasiana.com/",
        "ZIPAIR": "https://www.zipair.net/",
        "Scoot": "https://www.flyscoot.com/",
        "AirAsia": "https://www.airasia.com/",
        "VietJet": "https://www.vietjetair.com/",
        "Thai Airways": "https://www.thaiairways.com/",
        "Singapore Airlines": "https://www.singaporeair.com/",
        "Turkish Airlines": "https://www.turkishairlines.com/",
        "Emirates": "https://www.emirates.com/",
        "Qatar Airways": "https://www.qatarairways.com/",
        "Etihad": "https://www.etihad.com/",
    }
    return {name: f"{url}?search={query}" for name, url in airlines.items()}


def build_source_links(c: SearchCandidate, config: dict[str, Any]) -> list[SourceResult]:
    sources = config.get("sources", {})
    builders = {
        "google_flights": lambda candidate: build_google_flights_link(candidate, config),
        "skyscanner": build_skyscanner_link,
        "trip_com": build_tripcom_link,
        "ctrip": build_ctrip_link,
        "fliggy": build_fliggy_link,
    }
    results: list[SourceResult] = []
    for name, builder in builders.items():
        source_cfg = sources.get(name, {})
        if source_cfg.get("enabled", True):
            results.append(SourceResult(candidate=replace(c), source_name=name, query_link=builder(c)))
    if (sources.get("travelpayouts", {}) or {}).get("enabled", False):
        results.append(SourceResult(candidate=replace(c), source_name="travelpayouts", query_link=build_travelpayouts_search_link(c, config)))
    if (sources.get("amadeus", {}) or {}).get("enabled", False):
        results.append(SourceResult(candidate=replace(c), source_name="amadeus", query_link=build_amadeus_api_link(c, config)))
    if (sources.get("airline_official", {}) or {}).get("enabled", True):
        for airline, link in build_airline_links(c).items():
            results.append(SourceResult(candidate=replace(c), source_name=f"airline_official:{airline}", query_link=link))
    return results


def build_travelpayouts_search_link(c: SearchCandidate, config: dict[str, Any]) -> str:
    source_cfg = (config.get("sources") or {}).get("travelpayouts", {})
    marker = source_cfg.get("marker")
    params = {
        "origin_iata": c.origin,
        "destination_iata": c.destination,
        "depart_date": c.depart_date,
        "currency": "jpy",
        "one_way": "true" if c.trip_type == "oneway" else "false",
    }
    if c.return_date:
        params["return_date"] = c.return_date
    if marker:
        params["marker"] = marker
    return "https://www.aviasales.com/search?" + urlencode(params)


def build_travelpayouts_api_url(c: SearchCandidate, config: dict[str, Any]) -> str:
    params = travelpayouts_params(c, config)
    return "https://api.travelpayouts.com/aviasales/v3/prices_for_dates?" + urlencode(params)


def travelpayouts_params(c: SearchCandidate, config: dict[str, Any]) -> dict[str, Any]:
    source_cfg = (config.get("sources") or {}).get("travelpayouts", {})
    params: dict[str, Any] = {
        "origin": c.origin,
        "destination": c.destination,
        "currency": source_cfg.get("currency", "jpy"),
        "departure_at": c.depart_date,
        "one_way": "true" if c.trip_type == "oneway" else "false",
        "direct": "true" if direct_only_enabled(config) else "false",
        "sorting": "price",
        "direct": "true" if direct_only_enabled(config) else "false",
        "limit": int(source_cfg.get("offers_limit", 5)),
        "page": 1,
    }
    if c.return_date:
        params["return_at"] = c.return_date
    market = source_cfg.get("market")
    if market:
        params["market"] = market
    return params


def extract_stops_from_travelpayouts_item(item: dict[str, Any]) -> int | None:
    for key in ("transfers", "number_of_changes", "stops", "changes"):
        value = item.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, str) and value.strip().isdigit():
            return max(0, int(value.strip()))

    for key in ("segments", "route"):
        value = item.get(key)
        if isinstance(value, list) and value:
            return max(0, len(value) - 1)

    flight_number = item.get("flight_number")
    if isinstance(flight_number, list) and flight_number:
        return max(0, len(flight_number) - 1)
    if isinstance(flight_number, str) and "," in flight_number:
        parts = [part.strip() for part in flight_number.split(",") if part.strip()]
        if parts:
            return max(0, len(parts) - 1)

    airline = item.get("airline")
    if isinstance(airline, list) and airline:
        return max(0, len(airline) - 1)
    return None


def stops_allowed_for_item(item: dict[str, Any], config: dict[str, Any]) -> tuple[bool, int | None, str]:
    stops = extract_stops_from_travelpayouts_item(item)
    max_stops = configured_max_stops(config)
    if stops is None:
        return allow_unknown_stops(config), None, "manual_check_required"
    return stops <= max_stops, stops, "confirmed"


def travelpayouts_get(
    url: str,
    params: dict[str, Any],
    token: str,
    config: dict[str, Any],
    result: SourceResult,
    timeout: int = 30,
) -> requests.Response | None:
    global TRAVELPAYOUTS_REQUEST_COUNT
    source_cfg = (config.get("sources") or {}).get("travelpayouts", {})
    max_requests = int(source_cfg.get("max_requests_per_run", 300))
    attempts = int(source_cfg.get("retry_attempts", 3))
    base_sleep = float(source_cfg.get("retry_base_sleep_seconds", 2))
    pause_every = int(source_cfg.get("pause_every_requests", 80))
    pause_seconds = float(source_cfg.get("pause_seconds", 5))
    retry_statuses = {429, 500, 502, 503, 504}

    last_response: requests.Response | None = None
    for attempt in range(1, attempts + 1):
        if TRAVELPAYOUTS_REQUEST_COUNT >= max_requests:
            result.status = "skipped"
            result.message = f"Travelpayouts max_requests_per_run reached ({max_requests})"
            return None
        if TRAVELPAYOUTS_REQUEST_COUNT and pause_every > 0 and TRAVELPAYOUTS_REQUEST_COUNT % pause_every == 0:
            logging.info("Pausing Travelpayouts requests for %.1fs after %d requests.", pause_seconds, TRAVELPAYOUTS_REQUEST_COUNT)
            time.sleep(pause_seconds)

        TRAVELPAYOUTS_REQUEST_COUNT += 1
        try:
            response = requests.get(
                url,
                params=params,
                headers={"X-Access-Token": token, "Accept-Encoding": "gzip, deflate"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            result.status = "failed"
            result.message = f"Travelpayouts request failed: {exc}"
            if attempt < attempts:
                sleep_for = base_sleep * (2 ** (attempt - 1))
                logging.warning("Travelpayouts request error on attempt %d/%d; retrying in %.1fs: %s", attempt, attempts, sleep_for, exc)
                time.sleep(sleep_for)
                continue
            return None

        last_response = response
        if response.status_code in retry_statuses and attempt < attempts:
            sleep_for = base_sleep * (2 ** (attempt - 1))
            logging.warning("Travelpayouts HTTP %s on attempt %d/%d; retrying in %.1fs.", response.status_code, attempt, attempts, sleep_for)
            time.sleep(sleep_for)
            continue
        return response

    return last_response


def fetch_travelpayouts_price(result: SourceResult, config: dict[str, Any]) -> SourceResult:
    source_cfg = (config.get("sources") or {}).get("travelpayouts", {})
    token = os.environ.get(source_cfg.get("token_env", "TRAVELPAYOUTS_TOKEN"))
    if not token:
        result.status = "skipped"
        result.message = "Travelpayouts token missing"
        return result

    if should_use_travelpayouts_flexible(result.candidate, config):
        return fetch_travelpayouts_flexible_price(result, config, token)

    response = travelpayouts_get(
        "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
        travelpayouts_params(result.candidate, config),
        token,
        config,
        result,
    )
    if response is None:
        return result

    if response.status_code == 429:
        result.status = "rate_limited"
        result.message = "Travelpayouts rate limit reached"
        return result
    if response.status_code >= 400:
        result.status = "failed"
        result.message = f"Travelpayouts HTTP {response.status_code}: {response.text[:300]}"
        return result

    payload = response.json()
    if payload.get("success") is False:
        result.status = "failed"
        result.message = f"Travelpayouts returned success=false: {str(payload)[:300]}"
        return result

    data = payload.get("data") or []
    if isinstance(data, dict):
        data = list(data.values())

    offers: list[tuple[int, int | None, str]] = []
    filtered_by_stops = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        allowed, stops, stops_status = stops_allowed_for_item(item, config)
        if not allowed:
            filtered_by_stops += 1
            continue
        total = item.get("price", item.get("value"))
        try:
            offers.append((int(round(float(total))), stops, stops_status))
        except (TypeError, ValueError):
            continue

    if not offers:
        if should_use_travelpayouts_core_fallback(result.candidate, config):
            fallback = fetch_travelpayouts_flexible_price(result, config, token)
            if fallback.price_jpy is not None:
                fallback.status = "success_core_flexible"
                fallback.message = "Travelpayouts exact date had no offer; core flexible cached latest price returned"
            return fallback
        if filtered_by_stops:
            result.status = "filtered_stops"
            result.filtered_by_stops = True
            result.message = f"Travelpayouts offers filtered by max_stops={configured_max_stops(config)}"
            return result
        result.status = "no_price"
        result.message = "Travelpayouts returned no cached priced offers"
        return result

    price, stops, stops_status = min(offers, key=lambda item: item[0])
    result.price_jpy = price
    result.stops = stops
    result.stops_status = stops_status
    result.status = "success"
    result.price_mode = "exact_date"
    result.message = "Travelpayouts cached Aviasales price returned"
    return result


def should_use_travelpayouts_flexible(c: SearchCandidate, config: dict[str, Any]) -> bool:
    source_cfg = (config.get("sources") or {}).get("travelpayouts", {})
    if not source_cfg.get("flexible_global_fallback", True):
        return False
    return c.destination_category not in {"Domestic Japan", "Core China"}


def should_use_travelpayouts_core_fallback(c: SearchCandidate, config: dict[str, Any]) -> bool:
    source_cfg = (config.get("sources") or {}).get("travelpayouts", {})
    return bool(source_cfg.get("flexible_core_fallback", True) and c.destination_category == "Core China")


def travelpayouts_flexible_params(c: SearchCandidate, config: dict[str, Any]) -> dict[str, Any]:
    source_cfg = (config.get("sources") or {}).get("travelpayouts", {})
    today = dt.date.today()
    params: dict[str, Any] = {
        "origin": c.origin,
        "destination": c.destination,
        "currency": source_cfg.get("currency", "jpy"),
        "beginning_of_period": today.replace(day=1).isoformat(),
        "period_type": source_cfg.get("flexible_period_type", "month"),
        "group_by": "dates",
        "one_way": "true" if c.trip_type == "oneway" else "false",
        "sorting": "price",
        "limit": int(source_cfg.get("flexible_offers_limit", 10)),
        "page": 1,
    }
    market = source_cfg.get("market")
    if market:
        params["market"] = market
    return params


def fetch_travelpayouts_flexible_price(result: SourceResult, config: dict[str, Any], token: str) -> SourceResult:
    response = travelpayouts_get(
        "https://api.travelpayouts.com/aviasales/v3/get_latest_prices",
        travelpayouts_flexible_params(result.candidate, config),
        token,
        config,
        result,
    )
    if response is None:
        return result

    if response.status_code == 429:
        result.status = "rate_limited"
        result.message = "Travelpayouts flexible rate limit reached"
        return result
    if response.status_code >= 400:
        result.status = "failed"
        result.message = f"Travelpayouts flexible HTTP {response.status_code}: {response.text[:300]}"
        return result

    payload = response.json()
    if payload.get("success") is False:
        result.status = "failed"
        result.message = f"Travelpayouts flexible returned success=false: {str(payload)[:300]}"
        return result

    data = payload.get("data") or []
    if isinstance(data, dict):
        data = list(data.values())

    best: dict[str, Any] | None = None
    best_price: int | None = None
    best_stops: int | None = None
    best_stops_status = "manual_check_required"
    filtered_by_stops = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        allowed, stops, stops_status = stops_allowed_for_item(item, config)
        if not allowed:
            filtered_by_stops += 1
            continue
        total = item.get("price", item.get("value"))
        try:
            price = int(round(float(total)))
        except (TypeError, ValueError):
            continue
        if best_price is None or price < best_price:
            best_price = price
            best = item
            best_stops = stops
            best_stops_status = stops_status

    if best_price is None:
        if filtered_by_stops:
            result.status = "filtered_stops"
            result.filtered_by_stops = True
            result.message = f"Travelpayouts flexible offers filtered by max_stops={configured_max_stops(config)}"
            return result
        result.status = "no_price"
        result.message = "Travelpayouts flexible returned no cached priced offers"
        return result

    result.price_jpy = best_price
    result.stops = best_stops
    result.stops_status = best_stops_status
    if best:
        depart = best.get("depart_date") or best.get("departure_at")
        ret = best.get("return_date") or best.get("return_at")
        if depart:
            result.candidate.depart_date = str(depart)[:10]
        if ret and result.candidate.trip_type == "roundtrip":
            result.candidate.return_date = str(ret)[:10]
    result.price_mode = "flexible_cached"
    result.query_link = build_travelpayouts_search_link(result.candidate, config)
    result.status = "success_flexible"
    result.message = "Travelpayouts flexible cached latest price returned"
    return result


def amadeus_base_url(config: dict[str, Any]) -> str:
    source_cfg = (config.get("sources") or {}).get("amadeus", {})
    environment = source_cfg.get("environment", "test")
    if environment == "production":
        return "https://api.amadeus.com"
    return "https://test.api.amadeus.com"


def build_amadeus_api_link(c: SearchCandidate, config: dict[str, Any]) -> str:
    params = {
        "originLocationCode": c.origin,
        "destinationLocationCode": c.destination,
        "departureDate": c.depart_date,
        "adults": "1",
        "currencyCode": "JPY",
        "max": "1",
        "maxStops": str(configured_max_stops(config)),
    }
    if c.return_date:
        params["returnDate"] = c.return_date
    return f"{amadeus_base_url(config)}/v2/shopping/flight-offers?" + urlencode(params)


def extract_stops_from_amadeus_offer(offer: dict[str, Any]) -> int | None:
    itineraries = offer.get("itineraries") or []
    if not isinstance(itineraries, list) or not itineraries:
        return None
    stops: list[int] = []
    for itinerary in itineraries:
        if not isinstance(itinerary, dict):
            continue
        segments = itinerary.get("segments") or []
        if isinstance(segments, list) and segments:
            stops.append(max(0, len(segments) - 1))
    if not stops:
        return None
    return max(stops)


def get_amadeus_token(config: dict[str, Any]) -> str | None:
    source_cfg = (config.get("sources") or {}).get("amadeus", {})
    client_id = os.environ.get(source_cfg.get("client_id_env", "AMADEUS_CLIENT_ID"))
    client_secret = os.environ.get(source_cfg.get("client_secret_env", "AMADEUS_CLIENT_SECRET"))
    if not client_id or not client_secret:
        logging.info("Amadeus credentials are not configured; skipping API price fetch.")
        return None

    now = dt.datetime.now(dt.timezone.utc).timestamp()
    if AMADEUS_TOKEN_CACHE.get("token") and AMADEUS_TOKEN_CACHE.get("expires_at", 0) > now + 60:
        return str(AMADEUS_TOKEN_CACHE["token"])

    response = requests.post(
        f"{amadeus_base_url(config)}/v1/security/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if response.status_code >= 400:
        logging.warning("Amadeus token request failed: HTTP %s %s", response.status_code, response.text[:300])
        return None
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        logging.warning("Amadeus token response did not include access_token.")
        return None
    AMADEUS_TOKEN_CACHE["token"] = token
    AMADEUS_TOKEN_CACHE["expires_at"] = now + int(payload.get("expires_in", 0))
    return str(token)


def fetch_amadeus_price(result: SourceResult, config: dict[str, Any]) -> SourceResult:
    global AMADEUS_REQUEST_COUNT
    source_cfg = (config.get("sources") or {}).get("amadeus", {})
    max_requests = int(source_cfg.get("max_requests_per_run", 50))
    if AMADEUS_REQUEST_COUNT >= max_requests:
        result.status = "skipped"
        result.message = f"Amadeus max_requests_per_run reached ({max_requests})"
        return result

    token = get_amadeus_token(config)
    if not token:
        result.status = "skipped"
        result.message = "Amadeus credentials missing or token request failed"
        return result

    params = {
        "originLocationCode": result.candidate.origin,
        "destinationLocationCode": result.candidate.destination,
        "departureDate": result.candidate.depart_date,
        "adults": "1",
        "currencyCode": "JPY",
        "max": str(source_cfg.get("offers_limit", 3)),
        "maxStops": str(configured_max_stops(config)),
    }
    if result.candidate.return_date:
        params["returnDate"] = result.candidate.return_date

    AMADEUS_REQUEST_COUNT += 1
    try:
        response = requests.get(
            f"{amadeus_base_url(config)}/v2/shopping/flight-offers",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        result.status = "failed"
        result.message = f"Amadeus request failed: {exc}"
        return result

    if response.status_code == 429:
        result.status = "rate_limited"
        result.message = "Amadeus rate limit reached"
        return result
    if response.status_code >= 400:
        result.status = "failed"
        result.message = f"Amadeus HTTP {response.status_code}: {response.text[:300]}"
        return result

    payload = response.json()
    offers = payload.get("data") or []
    priced: list[tuple[int, int | None]] = []
    filtered_by_stops = 0
    for offer in offers:
        stops = extract_stops_from_amadeus_offer(offer)
        if stops is not None and stops > configured_max_stops(config):
            filtered_by_stops += 1
            continue
        total = (offer.get("price") or {}).get("grandTotal") or (offer.get("price") or {}).get("total")
        try:
            priced.append((int(round(float(total))), stops))
        except (TypeError, ValueError):
            continue
    if not priced:
        if filtered_by_stops:
            result.status = "filtered_stops"
            result.filtered_by_stops = True
            result.message = f"Amadeus offers filtered by max_stops={configured_max_stops(config)}"
            return result
        result.status = "no_price"
        result.message = "Amadeus returned no priced offers"
        return result

    price, stops = min(priced, key=lambda item: item[0])
    result.price_jpy = price
    result.stops = stops
    result.stops_status = "confirmed" if stops is not None else "manual_check_required"
    result.price_mode = "exact_date"
    result.status = "success"
    result.message = "Amadeus Flight Offers Search returned a price"
    return result


def fetch_price_optional(result: SourceResult, config: dict[str, Any], link_only: bool = False) -> SourceResult:
    """Optional price fetching hook.

    Default implementation intentionally does not scrape dynamic pages. Replace
    this with a legal API adapter or a very conservative Playwright adapter that
    exits on login, CAPTCHA, or bot checks.
    """
    if result.source_name == "travelpayouts" and not link_only:
        return fetch_travelpayouts_price(result, config)
    if result.source_name == "amadeus" and not link_only:
        return fetch_amadeus_price(result, config)
    source_cfg = (config.get("sources") or {}).get(result.source_name.split(":")[0], {})
    if link_only or source_cfg.get("mode", "link_only") == "link_only":
        return result
    logging.info("No compliant price adapter configured for %s; using manual link.", result.source_name)
    return result


def percent_drop(previous: int | None, current: int | None) -> float | None:
    if not previous or not current or previous <= 0:
        return None
    return round((previous - current) / previous * 100, 1)


def evaluate_price_alert(result: SourceResult, state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("settings", {})
    c = result.candidate
    previous_price = state.get("prices", {}).get(result.key, {}).get("price_jpy")
    history_low = state.get("history_lows", {}).get(c.key_base)
    drop_pct = percent_drop(previous_price, result.price_jpy)
    threshold = c.threshold_jpy
    below_threshold = result.price_jpy is not None and result.price_jpy <= threshold
    watch_margin = float(settings.get("watch_price_margin_pct", 25))
    watch_threshold = int(round(threshold * (1 + watch_margin / 100)))
    watch_enabled = bool(settings.get("watch_price_alert_enabled", True))
    watch_price = bool(
        watch_enabled
        and result.price_jpy is not None
        and not below_threshold
        and result.price_jpy <= watch_threshold
    )
    obvious_drop = drop_pct is not None and drop_pct >= float(settings.get("price_drop_alert_pct", 15))
    abnormal = result.price_jpy is not None and result.price_jpy <= threshold * (1 - float(settings.get("abnormal_discount_pct", 20)) / 100)

    if c.route_config.get("abnormal_jpy") and result.price_jpy is not None:
        abnormal = abnormal or result.price_jpy <= int(c.route_config["abnormal_jpy"])
    very_cheap = bool(c.route_config.get("very_cheap_jpy") and result.price_jpy is not None and result.price_jpy <= int(c.route_config["very_cheap_jpy"]))
    holiday_core = c.is_core_route and c.window_key != "normal"
    focus = holiday_core and result.price_jpy is not None and result.price_jpy <= 70000

    alert_needed = bool(result.price_jpy is not None and (below_threshold or obvious_drop or abnormal or focus or watch_price))
    return {
        "result": result,
        "previous_price": previous_price,
        "history_low": history_low,
        "drop_pct": drop_pct,
        "below_threshold": below_threshold,
        "watch_price": watch_price,
        "watch_threshold": watch_threshold,
        "max_stops": configured_max_stops(config),
        "allow_unknown_stops": allow_unknown_stops(config),
        "obvious_drop": obvious_drop,
        "abnormal": abnormal,
        "very_cheap": very_cheap,
        "holiday_core": holiday_core,
        "focus": focus,
        "alert_needed": alert_needed,
    }


def deduplicate_alert(alert: dict[str, Any], state: dict[str, Any], config: dict[str, Any]) -> bool:
    if not alert["alert_needed"]:
        return False
    result: SourceResult = alert["result"]
    settings = config.get("settings", {})
    dedup_days = int(settings.get("dedup_days", 7))
    repeat_drop_pct = float(settings.get("significant_drop_repeat_pct", 10))
    legacy_alert = state.get("alerts", {}).get(result.key)
    last_alert = state.get("alerts", {}).get(result.alert_key) or state.get("alerts", {}).get(result.candidate.alert_group_key) or legacy_alert
    if not last_alert:
        return True
    last_date = parse_date(last_alert.get("date", "1970-01-01"))
    if (dt.date.today() - last_date).days >= dedup_days:
        return True
    last_price = last_alert.get("price_jpy")
    current_price = result.price_jpy
    return bool(percent_drop(last_price, current_price) and percent_drop(last_price, current_price) >= repeat_drop_pct)


def alert_priority(alert: dict[str, Any]) -> int:
    if alert.get("abnormal"):
        return 0
    if alert.get("below_threshold"):
        return 1
    if alert.get("focus"):
        return 2
    if alert.get("obvious_drop"):
        return 3
    if alert.get("watch_price"):
        return 4
    return 99


def select_best_alerts_by_group(evaluated_alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for alert in evaluated_alerts:
        if not alert.get("alert_needed"):
            continue
        result: SourceResult = alert["result"]
        group_key = result.candidate.alert_group_key
        existing = best.get(group_key)
        if existing is None:
            best[group_key] = alert
            continue
        current_rank = (alert_priority(alert), result.price_jpy or 10**12)
        existing_result: SourceResult = existing["result"]
        existing_rank = (alert_priority(existing), existing_result.price_jpy or 10**12)
        if current_rank < existing_rank:
            best[group_key] = alert
    return sorted(
        best.values(),
        key=lambda a: (alert_priority(a), a["result"].price_jpy or 10**12, a["result"].candidate.route_name),
    )


def format_price(value: int | None) -> str:
    return "需人工确认" if value is None else f"{value:,}円"


def describe_price_mode(result: SourceResult) -> str:
    if result.price_mode == "exact_date":
        return "精确日期缓存报价"
    if result.price_mode == "flexible_cached":
        return "Travelpayouts 灵活日期缓存低价，日期来自缓存低价发现"
    return "需人工确认"


def build_alert_subject(alert: dict[str, Any]) -> str:
    r: SourceResult = alert["result"]
    c = r.candidate
    route = route_label_zh(c.route_name, c)
    date_part = c.depart_date
    if alert["abnormal"]:
        return f"【异常低价】{route}｜{format_price(r.price_jpy)}｜{date_part}"
    if alert["below_threshold"]:
        return f"【机票提醒】{route} 低于目标价｜{format_price(r.price_jpy)}｜{date_part}"
    if alert["focus"]:
        return f"【重点关注】{route} 节假日核心路线｜{format_price(r.price_jpy)}｜{date_part}"
    if alert["obvious_drop"]:
        return f"【机票降价】{route} 降价 {alert['drop_pct']}%｜{format_price(r.price_jpy)}｜{date_part}"
    if alert["watch_price"]:
        return f"【机票观察】{route} 接近目标价｜{format_price(r.price_jpy)}｜{date_part}"
    return f"【机票提醒】{route}｜{format_price(r.price_jpy)}｜{date_part}"


def build_alert_email(alert: dict[str, Any]) -> tuple[str, str, str]:
    r: SourceResult = alert["result"]
    c = r.candidate
    subject = build_alert_subject(alert)
    actions = []
    if alert["below_threshold"]:
        actions.append("价格低于目标价，建议尽快人工确认最终价格、行李、税费、退改签和转机时间。")
    if alert["watch_price"] and not alert["below_threshold"]:
        actions.append("价格接近目标价，建议加入观察名单，并对比 Google Flights、航司官网和 Trip.com。")
    if alert["obvious_drop"] and not alert["below_threshold"] and not alert["watch_price"]:
        actions.append("价格明显下降，但仍未低于理想阈值，可继续观察。")
    if alert["holiday_core"]:
        actions.append("节假日核心路线，建议优先确认行李、转机时间和退改签规则。")
    if alert["abnormal"]:
        actions.append("异常低价，优先确认是否含税费、是否有长转机、是否为不可退改票。")
    if r.price_mode == "flexible_cached":
        actions.append("这是缓存低价发现，不是实时出票价，最终价格和航班条件必须人工确认。")
    if not actions:
        actions.append("建议人工确认最终价格与航班条件。")

    lines = [
        "路线信息",
        f"- 路线名称：{route_label_zh(c.route_name, c)}",
        f"- 出发机场：{airport_label_zh(c.origin)}",
        f"- 到达机场：{airport_label_zh(c.destination)}",
        f"- 出发日期：{c.depart_date}",
        f"- 返回日期：{c.return_date or '-'}",
        f"- 单程/往返：{'单程' if c.trip_type == 'oneway' else '往返'}",
        f"- 目的地分类：{category_label_zh(c.destination_category)}",
        "",
        "价格信息",
        f"- 当前价格：{format_price(r.price_jpy)}",
        f"- 目标价：{format_price(c.threshold_jpy)}",
        f"- 观察价阈值：{format_price(alert['watch_threshold'])}",
        f"- 上次价格：{format_price(alert['previous_price'])}",
        f"- 历史最低价：{format_price(alert['history_low'])}",
        f"- 降价幅度：{alert['drop_pct'] if alert['drop_pct'] is not None else '-'}%",
        f"- 价格模式：{describe_price_mode(r)}",
        f"- 平台：{source_label_zh(r.source_name)}",
        f"- 查询链接：{r.query_link}",
        "",
        "转机限制",
        f"- 配置要求：最多 {alert['max_stops']} 次转机",
        f"- API 返回转机次数：{stop_label(r.stops)}",
        f"- 转机判断状态：{stops_status_label(r.stops_status)}",
        "- 备注：如果转机次数无法确认，请人工检查航班详情。",
        "",
        "触发原因",
        f"- 低于目标价：{bool_label_zh(alert['below_threshold'])}",
        f"- 接近目标价：{bool_label_zh(alert['watch_price'])}",
        f"- 明显降价：{bool_label_zh(alert['obvious_drop'])}",
        f"- 异常低价：{bool_label_zh(alert['abnormal'])}",
        f"- 节假日核心路线：{bool_label_zh(alert['holiday_core'])}",
    ]
    if r.price_mode == "flexible_cached":
        lines.extend([
            f"- 原始候选出发日期：{r.original_depart_date or '-'}",
            f"- 原始候选返回日期：{r.original_return_date or '-'}",
            f"- 实际缓存低价出发日期：{c.depart_date}",
            f"- 实际缓存低价返回日期：{c.return_date or '-'}",
            "- 提醒：此价格适合发现低价机会，最终价格和航班条件必须人工确认。",
        ])
    lines.extend([
        "",
        "建议动作",
        *[f"- {a}" for a in actions],
        "",
        "注意事项",
        "- 本脚本不会自动下单。",
        "- 本脚本不会保存支付信息。",
        "- 本脚本不会登录网站或绕过验证码。",
        "- Travelpayouts 返回的是缓存价格，最终价格以人工打开链接确认结果为准。",
        "- Google Flights / Skyscanner / Trip.com / 携程 / 飞猪链接仅用于人工确认，转机次数、行李、税费和最终价格需要手动检查。",
    ])
    html_body = "<br>".join(html.escape(line) for line in lines).replace(html.escape(r.query_link), f'<a href="{html.escape(r.query_link)}">{html.escape(r.query_link)}</a>')
    return subject, "\n".join(lines), html_body


def build_weekly_report_email(state: dict[str, Any], config: dict[str, Any]) -> tuple[str, str, str]:
    subject = "【机票周报】低价路线与人工确认链接"
    latest_prices = state.get("latest_prices", {})
    manual_links = state.get("manual_check_links", [])
    drops = sorted(state.get("weekly_drops", []), key=lambda x: x.get("drop_pct", 0), reverse=True)[:5]

    sections: list[str] = [f"# {subject}", ""]
    for title, categories in WEEKLY_GROUPS:
        rows = [
            item for item in latest_prices.values()
            if item.get("destination_category") in categories and item.get("price_jpy") is not None
        ]
        rows = sorted(rows, key=lambda x: x.get("price_jpy", 10**12))[:5]
        sections += [f"## {title}"]
        if not rows:
            sections.append("- 暂无可用抓价结果。")
        for item in rows:
            mode = price_mode_label_zh(item.get("price_mode", "unknown"))
            stops = stop_label(item.get("stops"))
            stops_status = stops_status_label(item.get("stops_status", "manual_check_required"))
            sections.append(
                f"- {route_label_zh(item.get('route_name'))} {item['depart_date']}~{item.get('return_date') or '-'} "
                f"{format_price(item.get('price_jpy'))} {source_label_zh(item.get('source_name'))} [{mode}] "
                f"转机={stops} 转机状态={stops_status} "
                f"低于目标价={bool_label_zh(item.get('below_threshold'))} 观察价={bool_label_zh(item.get('watch_price'))} "
                f"明显降价={bool_label_zh(item.get('obvious_drop'))} 异常低价={bool_label_zh(item.get('abnormal'))} "
                f"{item.get('query_link')}"
            )
        sections.append("")

    sections += ["## 最近一周降价最多的路线"]
    if not drops:
        sections.append("- 暂无降价记录。")
    for item in drops:
        sections.append(f"- {route_label_zh(item.get('route_name'))} 降价 {item.get('drop_pct')}% 当前 {format_price(item.get('price_jpy'))} {item.get('query_link')}")

    sections += ["", "## 需要人工确认的路线链接", "以下链接不会自动抓价，需要人工确认价格、转机次数、行李和税费。"]
    for item in manual_links[-50:]:
        sections.append(f"- {route_label_zh(item.get('route_name'))} {source_label_zh(item.get('source_name'))} {item.get('depart_date')} {item.get('query_link')}")
    if not manual_links:
        sections.append("- 暂无。")

    text = "\n".join(sections)
    html_body = "<br>".join(html.escape(line) for line in sections)
    return subject, text, html_body


def normalize_email_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []

    recipients: list[str] = []
    seen: set[str] = set()
    for value in values:
        address = str(value).strip()
        if not address:
            continue
        # 避免忘记替换示例地址时误发。
        if address.lower().endswith("@example.com"):
            continue
        key = address.lower()
        if key not in seen:
            recipients.append(address)
            seen.add(key)
    return recipients


def recipients_for_scope(config: dict[str, Any], scope: str, *, weekly_report: bool = False) -> list[str]:
    email_cfg = config.get("email", {})
    recipients = normalize_email_list(email_cfg.get("to", []))
    friends = normalize_email_list(email_cfg.get("friends_to", []))

    if weekly_report:
        if bool(email_cfg.get("send_weekly_report_to_friends", False)):
            recipients.extend(friends)
    elif bool(email_cfg.get("send_global_domestic_to_friends", True)):
        friend_scopes = set(email_cfg.get("friend_scopes", ["global", "domestic"]))
        if scope in friend_scopes:
            recipients.extend(friends)

    return normalize_email_list(recipients)


def send_email(
    config: dict[str, Any],
    subject: str,
    text_body: str,
    html_body: str | None = None,
    to: list[str] | None = None,
) -> None:
    smtp_cfg = config.get("smtp", {})
    email_cfg = config.get("email", {})
    password_env = email_cfg.get("password_env", "SMTP_PASSWORD")
    password = os.environ.get(password_env)
    if not password:
        raise RuntimeError(f"Missing SMTP password env var: {password_env}")

    recipients = normalize_email_list(to if to is not None else email_cfg.get("to", []))
    if not recipients:
        raise RuntimeError("No email recipients configured. Set email.to in the config file.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_cfg["from"]
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg.get("port", 587)), timeout=30) as server:
        if smtp_cfg.get("use_tls", True):
            server.starttls()
        server.login(smtp_cfg.get("username", email_cfg["from"]), password)
        server.send_message(msg)


def run_scope(args: argparse.Namespace) -> str:
    if args.core_only:
        return "core"
    if args.domestic_only:
        return "domestic"
    if args.global_only:
        return "global"
    return "all"


def build_run_summary(
    candidates: list[SearchCandidate],
    source_results: list[SourceResult],
    evaluated_alerts: list[dict[str, Any]],
    alerts_to_send: list[dict[str, Any]],
    scope: str,
    config: dict[str, Any],
) -> str:
    settings = config.get("settings", {})
    watch_margin = float(settings.get("watch_price_margin_pct", 25))
    category_counts = Counter(c.destination_category for c in candidates)
    source_counts = Counter(r.source_name.split(":")[0] for r in source_results)
    priced_count = sum(1 for r in source_results if r.price_jpy is not None)
    manual_count = sum(1 for r in source_results if r.price_jpy is None)
    status_counts = Counter(f"{r.source_name.split(':')[0]}:{r.status}" for r in source_results)
    nonstop_count = sum(1 for r in source_results if r.stops == 0 and r.stops_status == "confirmed" and r.price_jpy is not None)
    one_stop_count = sum(1 for r in source_results if r.stops == 1 and r.stops_status == "confirmed" and r.price_jpy is not None)
    unknown_stops_count = sum(1 for r in source_results if r.stops is None and r.price_jpy is not None)
    filtered_stops_count = sum(1 for r in source_results if r.status == "filtered_stops" or r.filtered_by_stops)
    alert_candidates = [a for a in evaluated_alerts if a.get("alert_needed")]
    watch_candidates = [a for a in evaluated_alerts if a.get("watch_price")]
    watch_emails = [a for a in alerts_to_send if a.get("watch_price")]
    suppressed_alerts = max(0, len(alert_candidates) - len(alerts_to_send))
    reason_counts = Counter()
    for alert in alert_candidates:
        for reason in ("below_threshold", "watch_price", "obvious_drop", "abnormal", "focus"):
            if alert.get(reason):
                reason_counts[reason] += 1
    rate_limited_count = sum(count for status, count in status_counts.items() if status.endswith(":rate_limited"))
    lines = [
        "## Flight Price Monitor Summary",
        "",
        f"- Mode: `{scope}`",
        f"- Candidate searches: `{len(candidates)}`",
        f"- Source checks / links generated: `{len(source_results)}`",
        f"- Priced results: `{priced_count}`",
        f"- Manual-check links: `{manual_count}`",
        f"- Alert candidates before dedup: `{len(alert_candidates)}`",
        f"- Watch-price candidates before dedup: `{len(watch_candidates)}`",
        f"- Alert candidates suppressed by dedup: `{suppressed_alerts}`",
        f"- Alert emails prepared: `{len(alerts_to_send)}`",
        f"- Watch-price emails prepared: `{len(watch_emails)}`",
        "",
        "### Categories",
    ]
    for category, count in sorted(category_counts.items()):
        lines.append(f"- {category}: `{count}`")
    lines += [
        "",
        "### Stop filtering",
        f"- Max stops configured: `{configured_max_stops(config)}`",
        f"- Allow unknown stops: `{str(allow_unknown_stops(config)).lower()}`",
        f"- Confirmed nonstop results: `{nonstop_count}`",
        f"- Confirmed one-stop results: `{one_stop_count}`",
        f"- Unknown stops results: `{unknown_stops_count}`",
        f"- Filtered by max stops: `{filtered_stops_count}`",
        "- Manual-link stops status: `manual_check_required`",
    ]
    lines += ["", "### Sources"]
    for source, count in sorted(source_counts.items()):
        lines.append(f"- {source}: `{count}`")
    lines += ["", "### Source statuses"]
    for status, count in sorted(status_counts.items()):
        lines.append(f"- {status}: `{count}`")
    lines += ["", "### Alerts by reason"]
    for reason in ("below_threshold", "watch_price", "obvious_drop", "abnormal", "focus"):
        lines.append(f"- {reason}: `{reason_counts.get(reason, 0)}`")
    examples: dict[str, str] = {}
    for result in source_results:
        if result.source_name == "travelpayouts" and result.status in {"no_price", "rate_limited", "failed", "skipped", "filtered_stops"} and result.status not in examples:
            examples[result.status] = result.message
    if examples:
        lines += ["", "### Travelpayouts status examples"]
        for status, message in sorted(examples.items()):
            lines.append(f"- {status}: {message}")
    priced_results = sorted(
        [r for r in source_results if r.price_jpy is not None],
        key=lambda r: (r.price_jpy or 10**12, r.candidate.route_name, r.candidate.depart_date),
    )
    if priced_results:
        lines += ["", "### Lowest priced results"]
        for result in unique_display_results(priced_results)[:10]:
            c = result.candidate
            route_dates = f"{c.depart_date}" if not c.return_date else f"{c.depart_date} -> {c.return_date}"
            watch_threshold = int(round(c.threshold_jpy * (1 + watch_margin / 100)))
            if result.price_jpy is not None and result.price_jpy <= c.threshold_jpy:
                price_status = "below_threshold"
            elif result.price_jpy is not None and result.price_jpy <= watch_threshold:
                price_status = "watch"
            else:
                price_status = "normal"
            lines.append(
                f"- {route_label_zh(c.route_name, c)} {airport_label_zh(c.origin)}->{airport_label_zh(c.destination)} {route_dates} "
                f"{format_price(result.price_jpy)} via {source_label_zh(result.source_name)} [{price_mode_label_zh(result.price_mode)}] "
                f"({price_status}, threshold {format_price(c.threshold_jpy)}, watch {format_price(watch_threshold)}, stops {stop_label(result.stops)} / {stops_status_label(result.stops_status)})"
            )
    below_threshold_alerts = sorted(
        [a for a in evaluated_alerts if a.get("below_threshold") and a["result"].price_jpy is not None],
        key=lambda a: (a["result"].price_jpy or 10**12, a["result"].candidate.route_name),
    )
    if below_threshold_alerts:
        lines += ["", "### Below-threshold results"]
        for alert in unique_display_alerts(below_threshold_alerts)[:10]:
            result = alert["result"]
            c = result.candidate
            route_dates = f"{c.depart_date}" if not c.return_date else f"{c.depart_date} -> {c.return_date}"
            dedup_note = "will email" if alert in alerts_to_send else "dedup suppressed"
            lines.append(
                f"- {route_label_zh(c.route_name, c)} {airport_label_zh(c.origin)}->{airport_label_zh(c.destination)} {route_dates} "
                f"{format_price(result.price_jpy)} below {format_price(c.threshold_jpy)} "
                f"via {source_label_zh(result.source_name)} ({dedup_note})"
            )
    watch_alerts = sorted(
        [a for a in evaluated_alerts if a.get("watch_price") and a["result"].price_jpy is not None],
        key=lambda a: (a["result"].price_jpy or 10**12, a["result"].candidate.route_name),
    )
    if watch_alerts:
        lines += ["", "### Watch-price results"]
        for alert in unique_display_alerts(watch_alerts)[:10]:
            result = alert["result"]
            c = result.candidate
            route_dates = f"{c.depart_date}" if not c.return_date else f"{c.depart_date} -> {c.return_date}"
            dedup_note = "will email" if alert in alerts_to_send else "dedup suppressed"
            lines.append(
                f"- {route_label_zh(c.route_name, c)} {airport_label_zh(c.origin)}->{airport_label_zh(c.destination)} {route_dates} "
                f"{format_price(result.price_jpy)} <= watch {format_price(alert['watch_threshold'])} "
                f"via {source_label_zh(result.source_name)} [{price_mode_label_zh(result.price_mode)}] ({dedup_note})"
            )
    if rate_limited_count:
        lines += [
            "",
            f"Travelpayouts returned rate_limited for {rate_limited_count} requests. Consider reducing max_requests_per_run or increasing pause_seconds.",
        ]
    if not alerts_to_send:
        lines += [
            "",
            "No alert email was sent/prepared. If `Priced results` is `0`, check `Source statuses`; if `Priced results` is greater than `0`, prices did not meet threshold/watch threshold/drop rules or were suppressed by dedup.",
        ]
    return "\n".join(lines)


def unique_display_results(results: list[SourceResult]) -> list[SourceResult]:
    best_by_group: dict[str, SourceResult] = {}
    for result in results:
        c = result.candidate
        key = "|".join([c.route_name, c.destination, c.depart_date, c.return_date or "", result.source_name])
        existing = best_by_group.get(key)
        if existing is None or (result.price_jpy or 10**12) < (existing.price_jpy or 10**12):
            best_by_group[key] = result
    return sorted(
        best_by_group.values(),
        key=lambda r: (r.price_jpy or 10**12, r.candidate.route_name, r.candidate.depart_date),
    )


def unique_display_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_group: dict[str, dict[str, Any]] = {}
    for alert in alerts:
        result: SourceResult = alert["result"]
        c = result.candidate
        key = "|".join([c.route_name, c.destination, c.depart_date, c.return_date or "", result.source_name])
        existing = best_by_group.get(key)
        if existing is None or (result.price_jpy or 10**12) < (existing["result"].price_jpy or 10**12):
            best_by_group[key] = alert
    return sorted(
        best_by_group.values(),
        key=lambda a: (a["result"].price_jpy or 10**12, a["result"].candidate.route_name, a["result"].candidate.depart_date),
    )


def publish_github_step_summary(summary: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(summary + "\n")
    except OSError as exc:
        logging.warning("Could not write GitHub step summary: %s", exc)


def update_state_for_result(state: dict[str, Any], alert: dict[str, Any]) -> None:
    result: SourceResult = alert["result"]
    c = result.candidate
    today = dt.date.today().isoformat()
    state.setdefault("source_status", {})[result.source_name] = {"date": today, "status": result.status, "message": result.message}
    state.setdefault("latest_links", {})[result.key] = result.query_link
    if result.price_jpy is None:
        state.setdefault("manual_check_links", []).append(
            {
                "date": today,
                "route_name": c.route_name,
                "source_name": result.source_name,
                "depart_date": c.depart_date,
                "return_date": c.return_date,
                "query_link": result.query_link,
                "price_mode": result.price_mode,
                "message": result.message,
                "stops": result.stops,
                "stops_status": result.stops_status,
            }
        )
        return

    previous = state.setdefault("prices", {}).get(result.key, {}).get("price_jpy")
    state["prices"][result.key] = {"date": today, "price_jpy": result.price_jpy}
    low = state.setdefault("history_lows", {}).get(c.key_base)
    if low is None or result.price_jpy < low:
        state["history_lows"][c.key_base] = result.price_jpy
    latest = {
        "date": today,
        "route_name": c.route_name,
        "destination_category": c.destination_category,
        "depart_date": c.depart_date,
        "return_date": c.return_date,
        "price_jpy": result.price_jpy,
        "source_name": result.source_name,
        "query_link": result.query_link,
        "price_mode": result.price_mode,
        "original_depart_date": result.original_depart_date,
        "original_return_date": result.original_return_date,
        "stops": result.stops,
        "stops_status": result.stops_status,
        "below_threshold": alert["below_threshold"],
        "watch_price": alert["watch_price"],
        "watch_threshold": alert["watch_threshold"],
        "obvious_drop": alert["obvious_drop"],
        "abnormal": alert["abnormal"],
    }
    state.setdefault("latest_prices", {})[result.key] = latest
    drop = percent_drop(previous, result.price_jpy)
    if drop and drop > 0:
        state.setdefault("weekly_drops", []).append({**latest, "drop_pct": drop})


def mark_alert_sent(state: dict[str, Any], alert: dict[str, Any]) -> None:
    result: SourceResult = alert["result"]
    state.setdefault("alerts", {})[result.alert_key] = {"date": dt.date.today().isoformat(), "price_jpy": result.price_jpy}
    state.setdefault("alerts", {})[result.candidate.alert_group_key] = {"date": dt.date.today().isoformat(), "price_jpy": result.price_jpy}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safe flight price monitor and link generator.")
    parser.add_argument("--config", default="flight_price_config.yaml")
    parser.add_argument("--state", default=STATE_FILE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-email", action="store_true")
    parser.add_argument("--weekly-report", action="store_true")
    parser.add_argument("--core-only", action="store_true")
    parser.add_argument("--domestic-only", action="store_true")
    parser.add_argument("--global-only", action="store_true")
    parser.add_argument("--link-only", action="store_true")
    parser.add_argument("--force", action="store_true", help="Run even if this mode already ran today.")
    parser.add_argument("--force-alerts", action="store_true", help="Bypass alert deduplication for testing.")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    setup_logging(args.verbose)
    config = load_config(args.config)
    dry_run = args.dry_run or bool(config.get("settings", {}).get("dry_run_default", False))
    state = load_state(args.state)
    today = dt.date.today().isoformat()
    scope = run_scope(args)

    if args.test_email:
        subject = "【机票监控】SMTP 测试邮件"
        text = "这是一封 SMTP 测试邮件。脚本不会自动下单、不会保存支付信息、不会登录网站或绕过验证码。"
        if dry_run:
            print(subject)
            print(text)
        else:
            send_email(config, subject, text)
        return 0

    if args.weekly_report:
        subject, text, html_body = build_weekly_report_email(state, config)
        if dry_run:
            print(text)
        else:
            send_email(config, subject, text, html_body, to=recipients_for_scope(config, scope, weekly_report=True))
        return 0

    run_key = f"last_{scope}_run_date"
    if not dry_run and not args.force and state.setdefault("runs", {}).get(run_key) == today:
        logging.info("Monitor mode '%s' already ran today (%s); exiting to avoid duplicate daily runs.", scope, today)
        return 0

    candidates = generate_candidate_searches(
        config,
        core_only=args.core_only,
        domestic_only=args.domestic_only,
        global_only=args.global_only,
    )
    logging.info("Generated %d candidate searches.", len(candidates))
    evaluated_alerts: list[dict[str, Any]] = []
    source_results: list[SourceResult] = []

    for candidate in candidates:
        for source_result in build_source_links(candidate, config):
            result = fetch_price_optional(source_result, config, link_only=args.link_only)
            source_results.append(result)
            alert = evaluate_price_alert(result, state, config)
            evaluated_alerts.append(alert)

    best_alerts = select_best_alerts_by_group(evaluated_alerts)
    if args.force_alerts:
        alerts_to_send = best_alerts
    else:
        alerts_to_send = [alert for alert in best_alerts if deduplicate_alert(alert, state, config)]

    for alert in evaluated_alerts:
        update_state_for_result(state, alert)

    logging.info("Prepared %d alert email(s).", len(alerts_to_send))
    summary = build_run_summary(candidates, source_results, evaluated_alerts, alerts_to_send, scope, config)
    logging.info("\n%s", summary)
    publish_github_step_summary(summary)
    for alert in alerts_to_send:
        subject, text_body, html_body = build_alert_email(alert)
        if dry_run:
            print("\n" + "=" * 80)
            print(subject)
            print(text_body)
        else:
            send_email(config, subject, text_body, html_body, to=recipients_for_scope(config, scope))
            mark_alert_sent(state, alert)

    if dry_run:
        logging.info("Dry run: state not saved.")
    else:
        state.setdefault("runs", {})[run_key] = today
        prune_state(
            state,
            keep_days=int(config.get("settings", {}).get("state_keep_days", 30)),
            dedup_days=int(config.get("settings", {}).get("dedup_days", 7)),
        )
        save_state(state, args.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
