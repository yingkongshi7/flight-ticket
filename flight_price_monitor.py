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
from dataclasses import dataclass, field
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

    @property
    def key(self) -> str:
        return f"{self.candidate.key_base}|{self.source_name}"

    @property
    def alert_key(self) -> str:
        return f"{self.candidate.alert_group_key}|{self.source_name}|{self.price_jpy or 'manual'}"


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
    if (sources.get("travelpayouts", {}) or {}).get("enabled", False):
        results.append(SourceResult(candidate=c, source_name="travelpayouts", query_link=build_travelpayouts_search_link(c, config)))
    if (sources.get("amadeus", {}) or {}).get("enabled", False):
        results.append(SourceResult(candidate=c, source_name="amadeus", query_link=build_amadeus_api_link(c, config)))
    if (sources.get("airline_official", {}) or {}).get("enabled", True):
        for airline, link in build_airline_links(c).items():
            results.append(SourceResult(candidate=c, source_name=f"airline_official:{airline}", query_link=link))
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
        "sorting": "price",
        "direct": "false",
        "limit": int(source_cfg.get("offers_limit", 5)),
        "page": 1,
    }
    if c.return_date:
        params["return_at"] = c.return_date
    market = source_cfg.get("market")
    if market:
        params["market"] = market
    return params


def fetch_travelpayouts_price(result: SourceResult, config: dict[str, Any]) -> SourceResult:
    global TRAVELPAYOUTS_REQUEST_COUNT
    source_cfg = (config.get("sources") or {}).get("travelpayouts", {})
    max_requests = int(source_cfg.get("max_requests_per_run", 80))
    if TRAVELPAYOUTS_REQUEST_COUNT >= max_requests:
        result.status = "skipped"
        result.message = f"Travelpayouts max_requests_per_run reached ({max_requests})"
        return result

    token = os.environ.get(source_cfg.get("token_env", "TRAVELPAYOUTS_TOKEN"))
    if not token:
        result.status = "skipped"
        result.message = "Travelpayouts token missing"
        return result

    if should_use_travelpayouts_flexible(result.candidate, config):
        return fetch_travelpayouts_flexible_price(result, config, token)

    TRAVELPAYOUTS_REQUEST_COUNT += 1
    try:
        response = requests.get(
            "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
            params=travelpayouts_params(result.candidate, config),
            headers={"X-Access-Token": token},
            timeout=30,
        )
    except requests.RequestException as exc:
        result.status = "failed"
        result.message = f"Travelpayouts request failed: {exc}"
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

    prices: list[int] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        total = item.get("price", item.get("value"))
        try:
            prices.append(int(round(float(total))))
        except (TypeError, ValueError):
            continue

    if not prices:
        if should_use_travelpayouts_core_fallback(result.candidate, config):
            fallback = fetch_travelpayouts_flexible_price(result, config, token)
            if fallback.price_jpy is not None:
                fallback.status = "success_core_flexible"
                fallback.message = "Travelpayouts exact date had no offer; core flexible cached latest price returned"
            return fallback
        result.status = "no_price"
        result.message = "Travelpayouts returned no cached priced offers"
        return result

    result.price_jpy = min(prices)
    result.status = "success"
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
    global TRAVELPAYOUTS_REQUEST_COUNT
    source_cfg = (config.get("sources") or {}).get("travelpayouts", {})
    if TRAVELPAYOUTS_REQUEST_COUNT and TRAVELPAYOUTS_REQUEST_COUNT % int(source_cfg.get("pause_every_requests", 250)) == 0:
        time.sleep(float(source_cfg.get("pause_seconds", 2)))

    TRAVELPAYOUTS_REQUEST_COUNT += 1
    try:
        response = requests.get(
            "https://api.travelpayouts.com/aviasales/v3/get_latest_prices",
            params=travelpayouts_flexible_params(result.candidate, config),
            headers={"X-Access-Token": token, "Accept-Encoding": "gzip, deflate"},
            timeout=30,
        )
    except requests.RequestException as exc:
        result.status = "failed"
        result.message = f"Travelpayouts flexible request failed: {exc}"
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
    for item in data:
        if not isinstance(item, dict):
            continue
        total = item.get("price", item.get("value"))
        try:
            price = int(round(float(total)))
        except (TypeError, ValueError):
            continue
        if best_price is None or price < best_price:
            best_price = price
            best = item

    if best_price is None:
        result.status = "no_price"
        result.message = "Travelpayouts flexible returned no cached priced offers"
        return result

    result.price_jpy = best_price
    if best:
        depart = best.get("depart_date") or best.get("departure_at")
        ret = best.get("return_date") or best.get("return_at")
        if depart:
            result.candidate.depart_date = str(depart)[:10]
        if ret and result.candidate.trip_type == "roundtrip":
            result.candidate.return_date = str(ret)[:10]
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
    }
    if c.return_date:
        params["returnDate"] = c.return_date
    return f"{amadeus_base_url(config)}/v2/shopping/flight-offers?" + urlencode(params)


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
    prices: list[int] = []
    for offer in offers:
        total = (offer.get("price") or {}).get("grandTotal") or (offer.get("price") or {}).get("total")
        try:
            prices.append(int(round(float(total))))
        except (TypeError, ValueError):
            continue
    if not prices:
        result.status = "no_price"
        result.message = "Amadeus returned no priced offers"
        return result

    result.price_jpy = min(prices)
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
) -> str:
    category_counts = Counter(c.destination_category for c in candidates)
    source_counts = Counter(r.source_name.split(":")[0] for r in source_results)
    priced_count = sum(1 for r in source_results if r.price_jpy is not None)
    manual_count = sum(1 for r in source_results if r.price_jpy is None)
    status_counts = Counter(f"{r.source_name.split(':')[0]}:{r.status}" for r in source_results)
    alert_candidates = [a for a in evaluated_alerts if a.get("alert_needed")]
    suppressed_alerts = max(0, len(alert_candidates) - len(alerts_to_send))
    lines = [
        "## Flight Price Monitor Summary",
        "",
        f"- Mode: `{scope}`",
        f"- Candidate searches: `{len(candidates)}`",
        f"- Source checks / links generated: `{len(source_results)}`",
        f"- Priced results: `{priced_count}`",
        f"- Manual-check links: `{manual_count}`",
        f"- Alert candidates before dedup: `{len(alert_candidates)}`",
        f"- Alert candidates suppressed by dedup: `{suppressed_alerts}`",
        f"- Alert emails prepared: `{len(alerts_to_send)}`",
        "",
        "### Categories",
    ]
    for category, count in sorted(category_counts.items()):
        lines.append(f"- {category}: `{count}`")
    lines += ["", "### Sources"]
    for source, count in sorted(source_counts.items()):
        lines.append(f"- {source}: `{count}`")
    lines += ["", "### Source statuses"]
    for status, count in sorted(status_counts.items()):
        lines.append(f"- {status}: `{count}`")
    priced_results = sorted(
        [r for r in source_results if r.price_jpy is not None],
        key=lambda r: (r.price_jpy or 10**12, r.candidate.route_name, r.candidate.depart_date),
    )
    if priced_results:
        lines += ["", "### Lowest priced results"]
        for result in unique_display_results(priced_results)[:10]:
            c = result.candidate
            route_dates = f"{c.depart_date}" if not c.return_date else f"{c.depart_date} -> {c.return_date}"
            lines.append(
                f"- {c.route_name} {c.origin}->{c.destination} {route_dates} "
                f"{format_price(result.price_jpy)} via {result.source_name} "
                f"(threshold {format_price(c.threshold_jpy)})"
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
                f"- {c.route_name} {c.origin}->{c.destination} {route_dates} "
                f"{format_price(result.price_jpy)} below {format_price(c.threshold_jpy)} "
                f"via {result.source_name} ({dedup_note})"
            )
    if not alerts_to_send:
        lines += [
            "",
            "No alert email was sent/prepared because no result met the alert rules. If `Priced results` is `0`, check `Source statuses` to see whether API credentials are missing, the API returned no offers, or only link-only sources ran.",
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
    alerts_to_send: list[dict[str, Any]] = []
    evaluated_alerts: list[dict[str, Any]] = []
    queued_alert_groups: set[str] = set()
    source_results: list[SourceResult] = []

    for candidate in candidates:
        for source_result in build_source_links(candidate, config):
            result = fetch_price_optional(source_result, config, link_only=args.link_only)
            source_results.append(result)
            alert = evaluate_price_alert(result, state, config)
            evaluated_alerts.append(alert)
            run_group_key = result.candidate.alert_group_key
            if args.force_alerts and alert["alert_needed"]:
                if run_group_key not in queued_alert_groups:
                    alerts_to_send.append(alert)
                    queued_alert_groups.add(run_group_key)
            elif deduplicate_alert(alert, state, config) and run_group_key not in queued_alert_groups:
                alerts_to_send.append(alert)
                queued_alert_groups.add(run_group_key)
            update_state_for_result(state, alert)

    logging.info("Prepared %d alert email(s).", len(alerts_to_send))
    summary = build_run_summary(candidates, source_results, evaluated_alerts, alerts_to_send, scope)
    logging.info("\n%s", summary)
    publish_github_step_summary(summary)
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
        state.setdefault("runs", {})[run_key] = today
        save_state(state, args.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
