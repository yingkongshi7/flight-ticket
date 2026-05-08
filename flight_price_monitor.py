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
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import yaml


STATE_FILE = "flight_price_state.json"
FORBIDDEN_ORIGINS = {"OSA", "KIX", "ITM", "UKB"}
ALLOWED_TOKYO_ORIGINS = {"TYO", "HND", "NRT"}
MANUAL_ONLY_SOURCES = {"google_flights", "skyscanner", "trip_com", "ctrip", "fliggy", "airline_official"}
WEEKLY_GROUPS = [
    ("核心路线：东京-西安", ["Core China"]),
    ("中国大陆/港澳台低价", ["China / HK / Taiwan"]),
    ("东南亚低价", ["Southeast Asia"]),
    ("东北亚低价", ["Northeast Asia"]),
    ("日本国内低价", ["Domestic Japan"]),
    ("海岛/度假低价", ["Islands"]),
    ("欧洲低价", ["Europe"]),
    ("北美低价", ["North America"]),
    ("澳洲/新西兰低价", ["Oceania"]),
    ("中东/中亚低价", ["Middle East / Central Asia"]),
]


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
    window_label: str = "普通时期"
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


@dataclass
class SourceResult:
    candidate: SearchCandidate
    source_name: str
    query_link: str
    price_jpy: int | None = None
    status: str = "manual_check_required"
    message: str = "需人工确认"

    @property
    def key(self) -> str:
        return f"{self.candidate.key_base}|{self.source_name}"


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


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(str(value))


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


def build_google_flights_link(c: SearchCandidate) -> str:
    q = f"{c.origin} to {c.destination} {c.depart_date}"
    if c.return_date:
        q += f" returning {c.return_date}"
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
        "google_flights": build_google_flights_link,
        "skyscanner": build_skyscanner_link,
        "trip_com": build_tripcom_link,
        "ctrip": build_ctrip_link,
        "fliggy": build_fliggy_link,
    }
    results: list[SourceResult] = []
    for name, builder in builders.items():
        source_cfg = sources.get(name, {})
        if source_cfg.get("enabled", True):
            results.append(SourceResult(candidate=c, source_name=name, query_link=builder(c)))
    if (sources.get("airline_official", {}) or {}).get("enabled", True):
        for airline, link in build_airline_links(c).items():
            results.append(SourceResult(candidate=c, source_name=f"airline_official:{airline}", query_link=link))
    return results


def fetch_price_optional(result: SourceResult, config: dict[str, Any], link_only: bool = False) -> SourceResult:
    """Optional price fetching hook.

    Default implementation intentionally does not scrape dynamic pages. Replace
    this with a legal API adapter or a very conservative Playwright adapter that
    exits on login, CAPTCHA, or bot checks.
    """
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
    obvious_drop = drop_pct is not None and drop_pct >= float(settings.get("price_drop_alert_pct", 15))
    abnormal = result.price_jpy is not None and result.price_jpy <= threshold * (1 - float(settings.get("abnormal_discount_pct", 20)) / 100)

    if c.route_config.get("abnormal_jpy") and result.price_jpy is not None:
        abnormal = abnormal or result.price_jpy <= int(c.route_config["abnormal_jpy"])
    very_cheap = bool(c.route_config.get("very_cheap_jpy") and result.price_jpy is not None and result.price_jpy <= int(c.route_config["very_cheap_jpy"]))
    holiday_core = c.is_core_route and c.window_key != "normal"
    focus = holiday_core and result.price_jpy is not None and result.price_jpy <= 70000

    alert_needed = bool(result.price_jpy is not None and (below_threshold or obvious_drop or abnormal or focus))
    return {
        "result": result,
        "previous_price": previous_price,
        "history_low": history_low,
        "drop_pct": drop_pct,
        "below_threshold": below_threshold,
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
    last_alert = state.get("alerts", {}).get(result.key)
    if not last_alert:
        return True
    last_date = parse_date(last_alert.get("date", "1970-01-01"))
    if (dt.date.today() - last_date).days >= dedup_days:
        return True
    last_price = last_alert.get("price_jpy")
    current_price = result.price_jpy
    return bool(percent_drop(last_price, current_price) and percent_drop(last_price, current_price) >= repeat_drop_pct)


def format_price(value: int | None) -> str:
    return "需人工确认" if value is None else f"{value:,}円"


def build_alert_subject(alert: dict[str, Any]) -> str:
    r: SourceResult = alert["result"]
    c = r.candidate
    tags = ["【机票提醒】"]
    if alert["focus"]:
        tags.append("【重点】")
    if alert["abnormal"]:
        tags.append("【异常低价】")
    route = c.route_name.replace("Tokyo-", "东京-").replace("Xian", "西安")
    trip_label = "单程" if c.trip_type == "oneway" else "往返"
    if alert["obvious_drop"] and not alert["below_threshold"]:
        return f"{''.join(tags)}{route} {c.window_label} 降价 {alert['drop_pct']}%｜当前 {format_price(r.price_jpy)}"
    return f"{''.join(tags)}{route} {c.window_label if c.window_key != 'normal' else ''} {trip_label} {format_price(r.price_jpy)}".replace("  ", " ").strip()


def build_alert_email(alert: dict[str, Any]) -> tuple[str, str, str]:
    r: SourceResult = alert["result"]
    c = r.candidate
    subject = build_alert_subject(alert)
    actions = []
    if alert["below_threshold"]:
        actions.append("价格低于阈值，建议尽快人工确认")
    if alert["obvious_drop"] and not alert["below_threshold"]:
        actions.append("明显降价，但未低于理想阈值，可观察")
    if alert["holiday_core"]:
        actions.append("黄金周/年末年始/春节核心路线，建议优先确认行李、转机时间、退改签规则")
    if not actions:
        actions.append("建议人工确认最终价格与航班条件")

    lines = [
        f"路线名称: {c.route_name}",
        f"destination_category: {c.destination_category}",
        f"出发机场: {c.origin}",
        f"到达机场: {c.destination}",
        f"出发日期: {c.depart_date}",
        f"返回日期: {c.return_date or '-'}",
        f"单程/往返: {'单程' if c.trip_type == 'oneway' else '往返'}",
        f"当前价格: {format_price(r.price_jpy)}",
        f"上次价格: {format_price(alert['previous_price'])}",
        f"历史最低价: {format_price(alert['history_low'])}",
        f"降价幅度: {alert['drop_pct'] if alert['drop_pct'] is not None else '-'}%",
        f"平台名称: {r.source_name}",
        f"查询链接: {r.query_link}",
        f"是否低于阈值: {alert['below_threshold']}",
        f"是否明显降价: {alert['obvious_drop']}",
        f"是否异常低价: {alert['abnormal']}",
        f"是否节假日核心路线: {alert['holiday_core']}",
        "",
        "建议动作:",
        *[f"- {a}" for a in actions],
        "",
        "注意事项:",
        "- 请人工确认是否含税费、托运行李、中转、红眼航班。",
        "- 请确认是否需要签证或转机签证。",
        "- 中国平台价格可能需要人工确认最终含税价。",
        "- 本脚本不自动下单、不保存支付信息、不绕过验证码。",
    ]
    html_body = "<br>".join(html.escape(line) for line in lines).replace(html.escape(r.query_link), f'<a href="{html.escape(r.query_link)}">{html.escape(r.query_link)}</a>')
    return subject, "\n".join(lines), html_body


def build_weekly_report_email(state: dict[str, Any], config: dict[str, Any]) -> tuple[str, str, str]:
    subject = "flight_price_weekly_report｜机票价格周报"
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
            sections.append(
                f"- {item['route_name']} {item['depart_date']}~{item.get('return_date') or '-'} "
                f"{format_price(item.get('price_jpy'))} {item['source_name']} "
                f"低于阈值={item.get('below_threshold')} 明显降价={item.get('obvious_drop')} 异常低价={item.get('abnormal')} "
                f"{item.get('query_link')}"
            )
        sections.append("")

    sections += ["## 最近一周降价最多的路线"]
    if not drops:
        sections.append("- 暂无降价记录。")
    for item in drops:
        sections.append(f"- {item.get('route_name')} {item.get('drop_pct')}% 当前 {format_price(item.get('price_jpy'))} {item.get('query_link')}")

    sections += ["", "## 需要人工确认的路线链接"]
    for item in manual_links[-50:]:
        sections.append(f"- {item.get('route_name')} {item.get('source_name')} {item.get('depart_date')} {item.get('query_link')}")
    if not manual_links:
        sections.append("- 暂无。")

    text = "\n".join(sections)
    html_body = "<br>".join(html.escape(line) for line in sections)
    return subject, text, html_body


def send_email(config: dict[str, Any], subject: str, text_body: str, html_body: str | None = None) -> None:
    smtp_cfg = config.get("smtp", {})
    email_cfg = config.get("email", {})
    password_env = email_cfg.get("password_env", "SMTP_PASSWORD")
    password = os.environ.get(password_env)
    if not password:
        raise RuntimeError(f"Missing SMTP password env var: {password_env}")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_cfg["from"]
    msg["To"] = ", ".join(email_cfg.get("to", []))
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg.get("port", 587)), timeout=30) as server:
        if smtp_cfg.get("use_tls", True):
            server.starttls()
        server.login(smtp_cfg.get("username", email_cfg["from"]), password)
        server.send_message(msg)


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
        "below_threshold": alert["below_threshold"],
        "obvious_drop": alert["obvious_drop"],
        "abnormal": alert["abnormal"],
    }
    state.setdefault("latest_prices", {})[result.key] = latest
    drop = percent_drop(previous, result.price_jpy)
    if drop and drop > 0:
        state.setdefault("weekly_drops", []).append({**latest, "drop_pct": drop})


def mark_alert_sent(state: dict[str, Any], alert: dict[str, Any]) -> None:
    result: SourceResult = alert["result"]
    state.setdefault("alerts", {})[result.key] = {"date": dt.date.today().isoformat(), "price_jpy": result.price_jpy}


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
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    setup_logging(args.verbose)
    config = load_config(args.config)
    dry_run = args.dry_run or bool(config.get("settings", {}).get("dry_run_default", False))
    state = load_state(args.state)
    today = dt.date.today().isoformat()

    if args.test_email:
        subject = "机票监控测试邮件"
        text = "这是一封 SMTP 测试邮件。脚本不会自动下单、不会保存支付信息。"
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
            send_email(config, subject, text, html_body)
        return 0

    if not dry_run and state.setdefault("runs", {}).get("last_monitor_run_date") == today:
        logging.info("Monitor already ran today (%s); exiting to avoid duplicate daily runs.", today)
        return 0

    candidates = generate_candidate_searches(
        config,
        core_only=args.core_only,
        domestic_only=args.domestic_only,
        global_only=args.global_only,
    )
    logging.info("Generated %d candidate searches.", len(candidates))
    alerts_to_send: list[dict[str, Any]] = []

    for candidate in candidates:
        for source_result in build_source_links(candidate, config):
            result = fetch_price_optional(source_result, config, link_only=args.link_only)
            alert = evaluate_price_alert(result, state, config)
            update_state_for_result(state, alert)
            if deduplicate_alert(alert, state, config):
                alerts_to_send.append(alert)

    logging.info("Prepared %d alert email(s).", len(alerts_to_send))
    for alert in alerts_to_send:
        subject, text_body, html_body = build_alert_email(alert)
        if dry_run:
            print("\n" + "=" * 80)
            print(subject)
            print(text_body)
        else:
            send_email(config, subject, text_body, html_body)
            mark_alert_sent(state, alert)

    if dry_run:
        logging.info("Dry run: state not saved.")
    else:
        state.setdefault("runs", {})["last_monitor_run_date"] = today
        save_state(state, args.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
