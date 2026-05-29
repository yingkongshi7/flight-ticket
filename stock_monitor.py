import argparse
import csv
import json
import logging
import os
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yaml
import yfinance as yf


JST = timezone(timedelta(hours=9))


@dataclass
class StockInfo:
    ticker: str
    name: str
    sector: str


SIGNAL_LOG_COLUMNS = [
    "signal_id",
    "signal_date",
    "run_datetime_jst",
    "ticker",
    "name",
    "sector",
    "signal_type",
    "raw_alerts",
    "action_level",
    "script_title",
    "price_at_signal",
    "high_52w",
    "drawdown_pct",
    "ma20",
    "ma50",
    "ma200",
    "above_ma200",
    "above_ma200_pct",
    "recent_cross_above_ma200",
    "return_1m_pct",
    "return_3m_pct",
    "return_6m_pct",
    "return_20d_pct",
    "return_60d_pct",
    "topix_return_20d_pct",
    "topix_return_3m_pct",
    "relative_topix_20d_pct",
    "relative_topix_3m_pct",
    "volume_spike",
    "recent_volume_spike_days",
    "current_volume",
    "avg_volume_20d",
    "avg_turnover_20d",
    "manual_review_done",
    "review_date",
    "fundamental_status",
    "news_risk",
    "earnings_risk",
    "valuation_comment",
    "personal_decision",
    "decision_reason",
    "paper_entry_date",
    "paper_entry_price",
    "planned_position_pct",
    "stop_loss_price",
    "target_review_price",
    "exit_rule",
    "actual_buy",
    "actual_position_note",
    "price_1w",
    "return_1w_pct",
    "topix_return_1w_pct",
    "relative_return_1w_pct",
    "price_1m",
    "return_1m_forward_pct",
    "topix_return_1m_pct",
    "relative_return_1m_pct",
    "price_3m",
    "return_3m_forward_pct",
    "topix_return_3m_pct",
    "relative_return_3m_pct",
    "price_6m",
    "return_6m_forward_pct",
    "topix_return_6m_pct",
    "relative_return_6m_pct",
    "max_gain_1m_pct",
    "max_drawdown_1m_pct",
    "max_gain_3m_pct",
    "max_drawdown_3m_pct",
    "result_label",
    "notes",
]


def setup_logging(log_file: str) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not config:
        raise ValueError("config.yaml is empty or invalid")

    required = ["stocks", "smtp", "email", "thresholds"]
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"Missing config sections: {', '.join(missing)}")

    return config


def flatten_stock_pool(config: Dict[str, Any]) -> List[StockInfo]:
    stocks: List[StockInfo] = []
    seen_tickers = set()
    for sector, items in config["stocks"].items():
        for item in items:
            ticker = item["ticker"]
            if ticker in seen_tickers:
                logging.warning("Duplicate ticker %s in sector %s skipped", ticker, sector)
                continue
            seen_tickers.add(ticker)
            stocks.append(
                StockInfo(
                    ticker=ticker,
                    name=item["name"],
                    sector=sector,
                )
            )
    return stocks


def fetch_price_data(ticker: str, period: str = "18mo") -> Optional[pd.DataFrame]:
    try:
        data = yf.download(
            ticker,
            period=period,
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        )
    except Exception as exc:
        logging.exception("Failed to fetch %s: %s", ticker, exc)
        return None

    if data is None or data.empty:
        logging.warning("No price data for %s", ticker)
        return None

    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    data = data.dropna(subset=["Close"])
    if data.empty:
        logging.warning("No valid close data for %s", ticker)
        return None

    if "Adj Close" in data.columns and data["Adj Close"].notna().any():
        base_close = data["Adj Close"]
    else:
        base_close = data["Close"]

    data["Signal Close"] = normalize_price_series(base_close, ticker)

    return data


def normalize_price_series(price: pd.Series, ticker: str) -> pd.Series:
    normalized = price.astype(float).copy()
    ratios = normalized / normalized.shift(1)
    split_like_ratios = ratios[(ratios > 0) & ((ratios < 0.5) | (ratios > 2.0))]

    for date, ratio in split_like_ratios.items():
        prior_mask = normalized.index < date
        if not prior_mask.any():
            continue
        normalized.loc[prior_mask] = normalized.loc[prior_mask] * float(ratio)
        logging.warning(
            "Detected split-like price gap for %s on %s, normalized prior prices by %.4f",
            ticker,
            date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else date,
            float(ratio),
        )

    return normalized


def get_price_at_or_before(data: pd.DataFrame, target_index: int) -> Optional[float]:
    if len(data) <= abs(target_index):
        return None
    value = data["Close"].iloc[target_index]
    if pd.isna(value) or value == 0:
        return None
    return float(value)


def pct_change_from_lookback(
    data: pd.DataFrame,
    lookback_days: int,
    price_col: str = "Signal Close",
) -> Optional[float]:
    if len(data) < lookback_days + 1:
        return None
    current = float(data[price_col].iloc[-1])
    past = float(data[price_col].iloc[-lookback_days - 1])
    if past == 0 or pd.isna(past):
        return None
    return (current / past - 1) * 100


def calculate_indicators(data: pd.DataFrame) -> Dict[str, Any]:
    raw_close = data["Close"]
    signal_close = data["Signal Close"]
    volume = data["Volume"] if "Volume" in data.columns else pd.Series(dtype=float)

    ma20 = signal_close.rolling(20).mean()
    ma50 = signal_close.rolling(50).mean()
    ma200 = signal_close.rolling(200).mean()

    last_close = float(raw_close.iloc[-1])
    last_signal_close = float(signal_close.iloc[-1])
    high_52w = float(signal_close.tail(252).max())
    drawdown_pct = (high_52w - last_signal_close) / high_52w * 100 if high_52w else 0
    current_ma200 = ma200.iloc[-1]
    current_ma20 = ma20.iloc[-1]
    current_ma50 = ma50.iloc[-1]
    above_ma20 = bool(pd.notna(current_ma20) and last_signal_close > current_ma20)
    above_ma50 = bool(pd.notna(current_ma50) and last_signal_close > current_ma50)
    above_ma200 = bool(pd.notna(current_ma200) and last_signal_close > current_ma200)

    recent_cross_above_ma200 = False
    if len(data) >= 205:
        recent_close = signal_close.tail(6)
        recent_ma200 = ma200.tail(6)
        for i in range(1, len(recent_close)):
            prev_below_or_equal = recent_close.iloc[i - 1] <= recent_ma200.iloc[i - 1]
            now_above = recent_close.iloc[i] > recent_ma200.iloc[i]
            if pd.notna(recent_ma200.iloc[i - 1]) and pd.notna(recent_ma200.iloc[i]) and prev_below_or_equal and now_above:
                recent_cross_above_ma200 = True
                break

    avg_volume_20d = float(volume.tail(20).mean()) if not volume.empty else 0.0
    last_volume = float(volume.iloc[-1]) if not volume.empty and pd.notna(volume.iloc[-1]) else 0.0
    volume_spike = bool(avg_volume_20d > 0 and last_volume > avg_volume_20d * 1.5)
    avg_turnover_20d = float((raw_close * volume).tail(20).mean()) if not volume.empty else 0.0
    rolling_avg_volume_20d = volume.rolling(20).mean() if not volume.empty else pd.Series(dtype=float)
    recent_volume_spike_days = 0
    if not volume.empty and len(volume) >= 23:
        recent_volume_spike_days = int((volume.tail(3) > rolling_avg_volume_20d.tail(3) * 1.5).sum())

    high_60d = float(signal_close.tail(60).max()) if len(signal_close) >= 60 else None
    is_60d_high = bool(high_60d is not None and last_signal_close >= high_60d)
    above_ma200_pct = None
    if pd.notna(current_ma200) and current_ma200 != 0:
        above_ma200_pct = (last_signal_close / float(current_ma200) - 1) * 100

    return {
        "current_price": last_close,
        "high_52w": high_52w,
        "high_60d": high_60d,
        "is_60d_high": is_60d_high,
        "is_52w_high": last_signal_close >= high_52w,
        "drawdown_pct": drawdown_pct,
        "ma20": float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else None,
        "ma50": float(ma50.iloc[-1]) if pd.notna(ma50.iloc[-1]) else None,
        "ma200": float(current_ma200) if pd.notna(current_ma200) else None,
        "above_ma20": above_ma20,
        "above_ma50": above_ma50,
        "above_ma200_pct": above_ma200_pct,
        "above_ma200": above_ma200,
        "recent_cross_above_ma200": recent_cross_above_ma200,
        "return_1m_pct": pct_change_from_lookback(data, 21),
        "return_3m_pct": pct_change_from_lookback(data, 63),
        "return_6m_pct": pct_change_from_lookback(data, 126),
        "return_20d_pct": pct_change_from_lookback(data, 20),
        "return_60d_pct": pct_change_from_lookback(data, 60),
        "avg_volume_20d": avg_volume_20d,
        "current_volume": last_volume,
        "volume_spike": volume_spike,
        "recent_volume_spike_days": recent_volume_spike_days,
        "avg_turnover_20d": avg_turnover_20d,
        "last_date": data.index[-1].strftime("%Y-%m-%d"),
    }


def calculate_relative_strength(stock_indicators: Dict[str, Any], topix_data: pd.DataFrame) -> Dict[str, Any]:
    topix_return_3m = pct_change_from_lookback(topix_data, 63)
    topix_return_20d = pct_change_from_lookback(topix_data, 20)
    stock_return_3m = stock_indicators.get("return_3m_pct")
    stock_return_20d = stock_indicators.get("return_20d_pct")

    if topix_return_3m is None or stock_return_3m is None:
        relative_3m = None
    else:
        relative_3m = stock_return_3m - topix_return_3m

    if topix_return_20d is None or stock_return_20d is None:
        relative_20d = None
    else:
        relative_20d = stock_return_20d - topix_return_20d

    return {
        "topix_return_20d_pct": topix_return_20d,
        "topix_return_3m_pct": topix_return_3m,
        "relative_topix_20d_pct": relative_20d,
        "relative_topix_3m_pct": relative_3m,
    }


def load_state(path: str) -> Dict[str, Any]:
    state_path = Path(path)
    if not state_path.exists():
        return {"alerts": {}}

    try:
        with state_path.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logging.warning("Failed to load state file %s: %s. Starting fresh.", path, exc)
        return {"alerts": {}}

    state.setdefault("alerts", {})
    return state


def save_state(state: Dict[str, Any], path: str) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def make_signal_id(signal_date: str, ticker: str, signal_type: str) -> str:
    return f"{signal_date}_{ticker}_{signal_type}"


def load_existing_signal_ids(path: str) -> set[str]:
    signal_path = Path(path)
    if not signal_path.exists():
        return set()

    try:
        with signal_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            return {
                row["signal_id"]
                for row in reader
                if row.get("signal_id")
            }
    except OSError as exc:
        logging.warning("Failed to load signal log %s: %s", path, exc)
        return set()


def signal_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def build_signal_log_row(
    stock: StockInfo,
    indicators: Dict[str, Any],
    combined_alert: Dict[str, Any],
    run_datetime: datetime,
) -> Dict[str, Any]:
    signal_date = indicators.get("last_date") or run_datetime.strftime("%Y-%m-%d")
    signal_type = combined_alert["type"]
    raw_alerts = ";".join(alert["type"] for alert in combined_alert.get("raw_alerts", []))

    row = {column: "" for column in SIGNAL_LOG_COLUMNS}
    row.update(
        {
            "signal_id": make_signal_id(signal_date, stock.ticker, signal_type),
            "signal_date": signal_date,
            "run_datetime_jst": run_datetime.strftime("%Y-%m-%d %H:%M:%S JST"),
            "ticker": stock.ticker,
            "name": stock.name,
            "sector": stock.sector,
            "signal_type": signal_type,
            "raw_alerts": raw_alerts,
            "action_level": combined_alert.get("action_level") or trade_action_level(signal_type),
            "script_title": combined_alert.get("title", ""),
            "price_at_signal": indicators.get("current_price"),
            "high_52w": indicators.get("high_52w"),
            "drawdown_pct": indicators.get("drawdown_pct"),
            "ma20": indicators.get("ma20"),
            "ma50": indicators.get("ma50"),
            "ma200": indicators.get("ma200"),
            "above_ma200": indicators.get("above_ma200"),
            "above_ma200_pct": indicators.get("above_ma200_pct"),
            "recent_cross_above_ma200": indicators.get("recent_cross_above_ma200"),
            "return_1m_pct": indicators.get("return_1m_pct"),
            "return_3m_pct": indicators.get("return_3m_pct"),
            "return_6m_pct": indicators.get("return_6m_pct"),
            "return_20d_pct": indicators.get("return_20d_pct"),
            "return_60d_pct": indicators.get("return_60d_pct"),
            "topix_return_20d_pct": indicators.get("topix_return_20d_pct"),
            "topix_return_3m_pct": indicators.get("topix_return_3m_pct"),
            "relative_topix_20d_pct": indicators.get("relative_topix_20d_pct"),
            "relative_topix_3m_pct": indicators.get("relative_topix_3m_pct"),
            "volume_spike": indicators.get("volume_spike"),
            "recent_volume_spike_days": indicators.get("recent_volume_spike_days"),
            "current_volume": indicators.get("current_volume"),
            "avg_volume_20d": indicators.get("avg_volume_20d"),
            "avg_turnover_20d": indicators.get("avg_turnover_20d"),
        }
    )
    return {column: signal_value(row.get(column, "")) for column in SIGNAL_LOG_COLUMNS}


def append_signal_log_rows(path: str, rows: List[Dict[str, Any]]) -> int:
    signal_path = Path(path)
    signal_path.parent.mkdir(parents=True, exist_ok=True)
    existing_ids = load_existing_signal_ids(path)
    new_rows = []
    for row in rows:
        signal_id = row.get("signal_id")
        if not signal_id or signal_id in existing_ids:
            continue
        existing_ids.add(str(signal_id))
        new_rows.append({column: row.get(column, "") for column in SIGNAL_LOG_COLUMNS})

    if not new_rows:
        return 0

    file_exists = signal_path.exists() and signal_path.stat().st_size > 0
    with signal_path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(SIGNAL_LOG_COLUMNS)
        for row in new_rows:
            seen_columns: set[str] = set()
            values = []
            for column in SIGNAL_LOG_COLUMNS:
                if column in seen_columns:
                    values.append("")
                else:
                    values.append(row.get(column, ""))
                    seen_columns.add(column)
            writer.writerow(values)

    return len(new_rows)


def signal_log_settings(config: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    configured = config.get("signal_log", {}) or {}
    enabled = bool(configured.get("enabled", True)) or bool(args.export_signals)
    path = args.signal_log or configured.get("path", "stock_signal_log.csv")
    append_only = bool(configured.get("append_only", True))
    return {"enabled": enabled, "path": path, "append_only": append_only}


def email_policy_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    policy = config.get("email_policy", {}) or {}
    return {
        "daily_summary_default": bool(policy.get("daily_summary_default", True)),
        "send_individual_alerts": bool(policy.get("send_individual_alerts", True)),
        "notify_action_levels": set(str(level).strip() for level in policy.get("notify_action_levels", ["A", "B"])),
        "send_summary_when_no_notify_alerts": bool(policy.get("send_summary_when_no_notify_alerts", False)),
        "individual_alert_types": set(
            policy.get(
                "individual_alert_types",
                [
                    "weak_deep_pullback",
                    "pullback_but_overheated",
                    "breakout_but_overheated",
                    "pullback_watch",
                    "deep_pullback_trend_intact",
                ],
            )
        ),
        "send_sector_heat_individual": bool(policy.get("send_sector_heat_individual", False)),
    }


def action_level_code(action_level: str) -> str:
    return action_level.split("：", 1)[0].strip()


def is_notify_level(combined_alert: Dict[str, Any], email_policy: Dict[str, Any]) -> bool:
    level = action_level_code(combined_alert.get("action_level", ""))
    return level in email_policy["notify_action_levels"]


def should_send_individual_alert(combined_alert: Dict[str, Any], policy: Dict[str, Any]) -> bool:
    allowed_by_type = combined_alert["type"] in policy["individual_alert_types"]
    return bool(policy["send_individual_alerts"] and allowed_by_type and is_notify_level(combined_alert, policy))


def prune_old_state(state: Dict[str, Any], dedup_days: int) -> None:
    cutoff = datetime.now(JST).date() - timedelta(days=dedup_days * 2)
    alerts = state.get("alerts", {})
    for key in list(alerts.keys()):
        last_alert_date = alerts[key].get("last_alert_date")
        if not last_alert_date:
            continue
        try:
            parsed = datetime.strptime(last_alert_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if parsed < cutoff:
            del alerts[key]


def should_send_alert(
    state: Dict[str, Any],
    ticker: str,
    alert_type: str,
    indicators: Dict[str, Any],
    thresholds: Dict[str, Any],
) -> bool:
    alerts = state.setdefault("alerts", {})
    key = f"{ticker}|{alert_type}"
    existing = alerts.get(key)
    today = datetime.now(JST).date()
    dedup_days = int(thresholds.get("dedup_days", 30))
    extra_drawdown_pct = float(thresholds.get("extra_drawdown_pct", 10))

    if not existing:
        return True

    if indicators.get("is_52w_high") and alert_type in {
        "pullback_watch",
        "deep_pullback_trend_intact",
        "pullback_but_overheated",
        "weak_deep_pullback",
    }:
        return True

    last_alert_date_str = existing.get("last_alert_date")
    last_drawdown = float(existing.get("drawdown_pct", 0))
    current_drawdown = float(indicators.get("drawdown_pct", 0))

    if current_drawdown >= last_drawdown + extra_drawdown_pct:
        return True

    if not last_alert_date_str:
        return True

    try:
        last_alert_date = datetime.strptime(last_alert_date_str, "%Y-%m-%d").date()
    except ValueError:
        return True

    return (today - last_alert_date).days >= dedup_days


def record_alert_state(state: Dict[str, Any], ticker: str, alert_type: str, indicators: Dict[str, Any]) -> None:
    key = f"{ticker}|{alert_type}"
    state.setdefault("alerts", {})[key] = {
        "last_alert_date": datetime.now(JST).strftime("%Y-%m-%d"),
        "drawdown_pct": indicators.get("drawdown_pct"),
        "high_52w": indicators.get("high_52w"),
        "current_price": indicators.get("current_price"),
    }


def summary_email_already_sent(state: Dict[str, Any], summary_date: str) -> bool:
    return summary_date in state.setdefault("summary_emails", {})


def record_summary_email_state(state: Dict[str, Any], summary_date: str) -> None:
    state.setdefault("summary_emails", {})[summary_date] = {
        "sent_at_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    }


def daily_run_completed(state: Dict[str, Any], run_date: str) -> bool:
    return bool(state.setdefault("daily_runs", {}).get(run_date, {}).get("completed_at_jst"))


def record_daily_run_state(
    state: Dict[str, Any],
    run_date: str,
    sent_summary: bool,
    sent_stock_count: int,
) -> None:
    state.setdefault("daily_runs", {})[run_date] = {
        "completed_at_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "sent_summary": bool(sent_summary),
        "sent_stock_count": sent_stock_count,
    }


def is_stateful_normal_run(args: argparse.Namespace) -> bool:
    return (
        not args.dry_run
        and not args.report
        and not args.summary_email
        and not args.test_email
    )


def reset_drawdown_alerts_on_new_high(state: Dict[str, Any], ticker: str) -> None:
    for alert_type in (
        "pullback_watch",
        "deep_pullback_trend_intact",
        "pullback_but_overheated",
        "weak_deep_pullback",
    ):
        state.get("alerts", {}).pop(f"{ticker}|{alert_type}", None)


def is_trend_intact(indicators: Dict[str, Any], thresholds: Dict[str, Any]) -> bool:
    relative_3m = indicators.get("relative_topix_3m_pct")
    above_ma200 = indicators.get("above_ma200")
    above_ma200_pct = indicators.get("above_ma200_pct")

    return (
        bool(above_ma200)
        and relative_3m is not None
        and relative_3m > float(thresholds.get("trend_intact_relative_3m_min_pct", -5))
        and (
            above_ma200_pct is None
            or above_ma200_pct > float(thresholds.get("trend_intact_above_ma200_min_pct", 0))
        )
    )


def check_alert_conditions(
    stock: StockInfo,
    indicators: Dict[str, Any],
    thresholds: Dict[str, Any],
    state: Dict[str, Any],
) -> List[Dict[str, str]]:
    alerts: List[Dict[str, str]] = []
    drawdown = indicators.get("drawdown_pct")
    relative_3m = indicators.get("relative_topix_3m_pct")
    avg_turnover_20d = indicators.get("avg_turnover_20d", 0)
    trend_intact = is_trend_intact(indicators, thresholds)
    pullback_trend_ok = trend_intact or (
        indicators.get("recent_cross_above_ma200")
        and relative_3m is not None
        and relative_3m > 0
    )
    liquidity_ok = avg_turnover_20d >= float(thresholds.get("min_avg_turnover_20d_jpy", 0))

    if indicators.get("is_52w_high"):
        reset_drawdown_alerts_on_new_high(state, stock.ticker)

    if drawdown is None or relative_3m is None:
        return alerts

    candidates = [
        (
            "pullback_watch",
            "回撤观察",
            drawdown >= float(thresholds["pullback_min_pct"])
            and drawdown <= float(thresholds["pullback_max_pct"])
            and pullback_trend_ok
            and relative_3m > float(thresholds["relative_3m_min_pct"])
            and liquidity_ok,
        ),
        (
            "breakout_strength",
            "强势突破观察",
            indicators.get("is_52w_high")
            and indicators.get("volume_spike")
            and relative_3m > float(thresholds["breakout_relative_3m_min_pct"]),
        ),
        (
            "deep_pullback_trend_intact",
            "深度回撤但趋势未坏",
            drawdown >= float(thresholds["deep_pullback_min_pct"]) and trend_intact,
        ),
        (
            "trend_weakness",
            "趋势转弱，谨慎观察",
            not indicators.get("above_ma200")
            and indicators.get("ma200") is not None
            and relative_3m < float(thresholds["risk_relative_3m_max_pct"]),
        ),
        (
            "overheat_risk",
            "过热提醒，避免追高",
            (indicators.get("return_60d_pct") is not None
            and indicators.get("return_60d_pct") >= float(thresholds["overheat_return_60d_min_pct"]))
            or (indicators.get("above_ma200_pct") is not None
            and indicators.get("above_ma200_pct") >= float(thresholds["overheat_above_ma200_min_pct"]))
            or indicators.get("recent_volume_spike_days", 0) >= int(thresholds["overheat_volume_spike_days"]),
        ),
    ]

    for alert_type, title, triggered in candidates:
        if triggered:
            alerts.append({"type": alert_type, "title": title})

    return alerts


def fmt_pct(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.2f}%"


def fmt_num(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:,.2f}"


def alert_action_prefix(alert_type: str) -> str:
    """Return a short action prefix for email subjects."""
    mapping = {
        "pullback_watch": "观察池",
        "deep_pullback_trend_intact": "重点研究",
        "breakout_strength": "强势观察",
        "overheat_risk": "不宜追高",
        "trend_weakness": "风险警告",
        "sector_heat": "行业热度",
        "weak_deep_pullback": "高风险复查",
        "pullback_but_overheated": "观察名单",
        "breakout_but_overheated": "强势观察",
    }
    return mapping.get(alert_type, "观察提醒")


def trade_action_level(alert_type: str) -> str:
    """Return the research priority shown in the email body.

    A/B/C are research priority labels, not buy ratings.
    """
    mapping = {
        "deep_pullback_trend_intact": "A：重点研究。技术形态和主题逻辑较强，但仍需人工确认估值、财报和仓位。",
        "pullback_watch": "B：加入观察池。部分条件满足，但仍需等待基本面和估值确认。",
        "breakout_strength": "B：强势观察。趋势较强，但不代表可以追高。",
        "overheat_risk": "C：暂不关注。短期过热或仓位风险较高。",
        "trend_weakness": "C：暂不关注。趋势或相对强度风险较高。",
        "sector_heat": "B：行业热度观察。用于发现资金方向，不代表个股买入评级。",
        "weak_deep_pullback": "C：暂不关注。深度回撤叠加趋势转弱，需要高风险复查。",
        "pullback_but_overheated": "B：加入观察池。中期值得研究，但短线不宜追高。",
        "breakout_but_overheated": "B：强势观察。趋势较强，但短线过热。",
    }
    return mapping.get(alert_type, "C：暂不关注。仅作为观察提醒。")


def build_trade_recommendation(alert_type: str, indicators: Dict[str, Any]) -> str:
    """Build a plain-language recommendation for a stock-level alert."""
    if alert_type == "pullback_watch":
        return """交易建议：适合买入候选观察。

理由：
- 股价已经从52周高点回撤到合理观察区间。
- 趋势尚未明显破坏，或正在重新站上200日线。
- 相对TOPIX没有明显走弱。

建议操作：
- 可以加入重点观察名单。
- 如果基本面和财报没有恶化，可考虑小额分批买入。
- 不建议一次性重仓。
- 第一笔可控制在计划仓位的20%～30%。
"""

    if alert_type == "deep_pullback_trend_intact":
        return """交易建议：适合重点研究，可能存在较好买点。

理由：
- 股价已经深度回撤。
- 但长期趋势没有完全破坏，或已经重新站上200日线。
- 这类信号可能出现在优质股错杀或行业阶段性恐慌后。

建议操作：
- 优先检查最新财报、业绩修正和行业新闻。
- 如果基本面没有恶化，可考虑分批买入。
- 第一笔可控制在计划仓位的30%左右。
- 如果之后继续回撤，但基本面仍然稳健，可再分批加仓。
"""

    if alert_type == "breakout_strength":
        return """交易建议：趋势强，但不宜追高重仓。

理由：
- 股价创出52周新高，并伴随成交量放大。
- 相对TOPIX表现较强，说明资金正在流入。
- 但突破后短期可能出现回踩或过热。

建议操作：
- 不建议看到邮件后立刻重仓追入。
- 可加入强势股观察名单。
- 如果已有仓位，可以继续持有。
- 如果没有仓位，建议等待回踩20日线/50日线，或只做小仓试探。
"""

    if alert_type == "overheat_risk":
        return """交易建议：不宜购买，避免追高。

理由：
- 股价短期涨幅过大，或明显远离200日线。
- 最近可能伴随连续放量，说明交易拥挤。
- 此类信号不是买入信号，而是风险提醒。

建议操作：
- 不建议新买入。
- 已有仓位可以考虑部分止盈。
- 至少应停止继续加仓。
- 如果后续回撤到20日线/50日线附近，再重新观察。
"""

    if alert_type == "trend_weakness":
        return """交易建议：不建议买入，已有仓位需要复查。

理由：
- 股价跌破200日线。
- 相对TOPIX明显走弱。
- 说明个股趋势可能已经转弱，不能简单理解为便宜。

建议操作：
- 不建议新买入。
- 已有仓位需要检查基本面是否恶化。
- 如果业绩、订单或行业逻辑变差，应考虑减仓。
- 如果只是短期市场波动，可以等待重新站上200日线后再观察。
"""

    return """交易建议：仅作为观察提醒。

建议操作：
- 不自动买入。
- 先检查基本面、估值、财报和行业消息。
- 再决定是否加入买入候选。
"""


def build_sector_recommendation(alert: Dict[str, Any]) -> str:
    """Build a recommendation block for a sector heat alert."""
    return """交易建议：行业热度上升，但不代表可以无差别追买。

建议操作：
- 优先从该行业中筛选已经回撤、但趋势没有破坏的个股。
- 对已经连续大涨、远离200日线的个股，避免追高。
- 如果你已有该行业仓位，可以继续持有，但不建议因热度提醒直接重仓加仓。
- 更好的做法是等待行业内优质股回踩20日线或50日线后再观察。
- 行业热度提醒主要用于发现资金流入方向，而不是立即买入指令。
"""


THEME_CONNECTIONS_BY_TICKER: Dict[str, List[str]] = {
    "8035.T": ["半导体设备", "AI硬件"],
    "6857.T": ["半导体测试设备", "AI硬件"],
    "6920.T": ["半导体设备"],
    "6146.T": ["半导体设备", "精密加工"],
    "7735.T": ["半导体设备"],
    "4063.T": ["半导体材料"],
    "3436.T": ["半导体材料"],
    "6315.T": ["半导体封装设备"],
    "6590.T": ["半导体设备"],
    "6526.T": ["半导体设计", "AI硬件"],
    "6723.T": ["车载 / 功率 / 工业"],
    "6963.T": ["车载 / 功率 / 工业"],
    "5805.T": ["光通信", "电力设备", "AI数据中心"],
    "5801.T": ["光通信", "电力设备", "AI数据中心"],
    "5802.T": ["光通信", "电力设备", "AI数据中心"],
    "5803.T": ["光通信", "AI数据中心"],
    "6367.T": ["冷却系统", "AI数据中心"],
    "6501.T": ["电力设备", "服务器供应链", "AI数据中心"],
    "6503.T": ["电力设备", "工业自动化"],
    "1942.T": ["电力施工", "AI数据中心"],
    "1969.T": ["冷却系统", "数据中心工程"],
    "1951.T": ["通信基础设施", "数据中心工程"],
    "6861.T": ["工业自动化", "精密制造"],
    "6954.T": ["工业自动化", "机器人"],
    "6506.T": ["工业自动化", "机器人"],
    "6645.T": ["工业自动化"],
    "6273.T": ["工业自动化"],
    "6383.T": ["物流自动化"],
    "6324.T": ["机器人零部件"],
    "6481.T": ["工业自动化", "机器人零部件"],
    "6479.T": ["精密部件", "车载 / 工业"],
    "7011.T": ["防卫", "能源设备"],
    "7012.T": ["防卫", "航空航天"],
    "7013.T": ["防卫", "航空航天", "能源设备"],
    "6701.T": ["防卫", "服务器供应链", "AI基础设施"],
    "6702.T": ["服务器供应链", "IT基础设施"],
}


def config_section(config: Optional[Dict[str, Any]], key: str, defaults: Dict[str, Any]) -> Dict[str, Any]:
    configured = (config or {}).get(key, {}) or {}
    merged = dict(defaults)
    merged.update(configured)
    return merged


def bool_ja(value: Any) -> str:
    return "是" if bool(value) else "否"


def money_jpy(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:,.0f}円"


def pct_value(value: Any) -> Optional[float]:
    return float(value) if value is not None else None


def classify_technical_status(indicators: Dict[str, Any], config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    screening = config_section(
        config,
        "screening",
        {
            "deep_pullback_threshold_pct": 35,
            "min_relative_return_vs_topix_3m_pct": -5,
            "require_above_200dma": True,
            "reentry_days_above_200dma": 5,
        },
    )
    deep_threshold = float(screening.get("deep_pullback_threshold_pct", 35))
    drawdown = pct_value(indicators.get("drawdown_pct")) or 0.0
    return_6m = pct_value(indicators.get("return_6m_pct"))
    return_20d = pct_value(indicators.get("return_20d_pct"))
    above_ma200 = bool(indicators.get("above_ma200"))
    above_ma20 = bool(indicators.get("above_ma20"))
    above_ma50 = bool(indicators.get("above_ma50"))
    recent_cross = bool(indicators.get("recent_cross_above_ma200"))
    deep_pullback = drawdown >= deep_threshold

    short_weak_long_intact = above_ma200 and not above_ma20 and not above_ma50
    if not above_ma200 and not recent_cross:
        classification = "趋势转坏预警"
        interpretation = "当前价格低于200日均线，且最近5个交易日没有重新站上，暂不关注或等待重新站上200日线。"
    elif deep_pullback and above_ma200 and short_weak_long_intact and return_6m is not None and return_6m >= 40:
        classification = "高位强势股深回调"
        interpretation = "高位强势股深回调，值得研究，但不等于低估；可能只是从过热区回落。"
    elif return_6m is not None and return_6m >= 60 and return_20d is not None and return_20d < -10:
        classification = "高位杀估值"
        interpretation = "过去6个月涨幅仍高，但近期快速下跌，需警惕主题降温后的估值压缩。"
    elif deep_pullback and above_ma200:
        classification = "强势回调"
        interpretation = "高于200日线且回撤较深，中长期趋势仍在，但回撤不等于低估。"
    elif recent_cross and above_ma200:
        classification = "低位反转观察"
        interpretation = "长期低迷后重新站上200日线，可以观察反转是否得到成交量和基本面确认。"
    else:
        classification = "常规观察"
        interpretation = "技术条件有部分改善，但尚未形成明确的高优先级研究信号。"

    if short_weak_long_intact:
        interpretation += " 短线偏弱，中长期趋势暂未完全破坏。"

    return {
        "deep_pullback": deep_pullback,
        "short_weak_long_intact": short_weak_long_intact,
        "classification": classification,
        "interpretation": interpretation,
    }


def analyze_theme_relevance(stock: StockInfo) -> Dict[str, Any]:
    connections = THEME_CONNECTIONS_BY_TICKER.get(stock.ticker, [])
    if connections:
        relevance = "高"
        logic = "、".join(connections)
        need_confirm = "是，需要查看公司具体业务收入占比和公司说明是否支持该主题。"
    elif any(keyword in stock.sector for keyword in ["金融", "商社", "资源", "医疗"]):
        relevance = "低"
        logic = "与AI数据中心、半导体设备、电力冷却等主题的直接连接较弱。"
        need_confirm = "是，不能仅凭市场叙事判断为AI受益股。"
    else:
        relevance = "中"
        logic = "行业主题存在间接联系，但主营业务受益程度需要资料确认。"
        need_confirm = "是，需要查看收入结构、订单说明和决算资料。"
    return {"relevance": relevance, "logic": logic, "need_confirm": need_confirm}


def analyze_valuation_risk(indicators: Dict[str, Any]) -> Dict[str, Any]:
    drawdown = pct_value(indicators.get("drawdown_pct")) or 0.0
    return_6m = pct_value(indicators.get("return_6m_pct"))
    warnings = ["估值数据不足，需要人工确认。不可仅凭回撤幅度判断便宜。"]
    if drawdown >= 20 and return_6m is not None and return_6m >= 40:
        warnings.append("该股可能只是从过热区回落，未必已经低估。")
    return {
        "per": "数据不足",
        "pbr": "数据不足",
        "psr": "数据不足",
        "ev_ebitda": "数据不足",
        "roe": "数据不足",
        "operating_margin": "数据不足",
        "historical_percentile_5y": "数据不足",
        "judgement": "待确认",
        "risk": " ".join(warnings),
    }


def analyze_fundamentals(stock: StockInfo) -> Dict[str, Any]:
    return {
        "revenue_growth": "数据不足",
        "operating_profit_growth": "数据不足",
        "net_profit_growth": "数据不足",
        "guidance_revision": "数据不足",
        "operating_margin_change": "数据不足",
        "free_cash_flow_change": "数据不足",
        "theme_demand_comment": "数据不足",
        "status": "待确认",
        "support_theme": "待确认",
        "materials": "最新决算短信 / 有価証券報告書 / 決算説明資料 / 订单和需求说明",
        "note": "基本面数据不足，需要人工查看最新决算短信 / 有価証券報告書 / 決算説明資料。",
    }


def analyze_position_feasibility(indicators: Dict[str, Any], config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    user_profile = config_section(
        config,
        "user_profile",
        {
            "investable_capital_jpy": 10000000,
            "single_stock_initial_limit_pct": 1.5,
            "single_stock_max_limit_pct": 3.0,
            "allow_odd_lot": False,
        },
    )
    risk_labels = config_section(
        config,
        "risk_labels",
        {
            "high_position_warning_pct": 3.0,
            "very_high_position_warning_pct": 5.0,
        },
    )
    price = float(indicators.get("current_price") or 0)
    lot_size = int(user_profile.get("lot_size", 100))
    capital = float(user_profile.get("investable_capital_jpy", 10000000))
    initial_limit = float(user_profile.get("single_stock_initial_limit_pct", 1.5))
    max_limit = float(user_profile.get("single_stock_max_limit_pct", 3.0))
    allow_odd_lot = bool(user_profile.get("allow_odd_lot", False))
    lot_amount = price * lot_size if price else None
    lot_pct = lot_amount / capital * 100 if lot_amount is not None and capital else None
    high_warning = float(risk_labels.get("high_position_warning_pct", 3.0))
    very_high_warning = float(risk_labels.get("very_high_position_warning_pct", 5.0))

    if lot_pct is None:
        advice = "一手金额无法计算，需要确认价格数据。"
    elif lot_pct <= initial_limit:
        advice = "一手金额在观察仓范围内。"
    elif lot_pct <= max_limit:
        advice = "一手金额偏大，需谨慎。"
    elif lot_pct > very_high_warning:
        advice = "单股初始仓位过高，不建议直接买入一手。"
    elif lot_pct > high_warning:
        advice = "一手金额过大，不适合直接一手买入，除非使用単元未満株 / S株 / 小额买入。"
    else:
        advice = "一手金额超过普通观察仓上限，需要降低单次投入。"

    if allow_odd_lot and lot_pct is not None and lot_pct > initial_limit:
        advice += " 已允许単元未満株，可优先用更小金额观察。"

    return {
        "price": price,
        "lot_size": lot_size,
        "lot_amount": lot_amount,
        "capital": capital,
        "lot_pct": lot_pct,
        "exceeds_initial_limit": bool(lot_pct is not None and lot_pct > initial_limit),
        "exceeds_max_limit": bool(lot_pct is not None and lot_pct > max_limit),
        "allow_odd_lot": allow_odd_lot,
        "advice": advice,
    }


def build_research_analysis(stock: StockInfo, indicators: Dict[str, Any], config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    screening = config_section(
        config,
        "screening",
        {
            "deep_pullback_threshold_pct": 35,
            "min_relative_return_vs_topix_3m_pct": -5,
            "require_above_200dma": True,
            "reentry_days_above_200dma": 5,
        },
    )
    technical = classify_technical_status(indicators, config)
    theme = analyze_theme_relevance(stock)
    valuation = analyze_valuation_risk(indicators)
    fundamentals = analyze_fundamentals(stock)
    position = analyze_position_feasibility(indicators, config)

    relative_3m = pct_value(indicators.get("relative_topix_3m_pct"))
    min_relative = float(screening.get("min_relative_return_vs_topix_3m_pct", -5))
    above_ma200 = bool(indicators.get("above_ma200"))
    high_theme = theme["relevance"] == "高"
    trend_broken = technical["classification"] == "趋势转坏预警"

    if trend_broken:
        priority = "C"
        final_action = "暂不关注"
        final_note = "跌破200日线且无法收回，趋势风险优先于回撤幅度。"
    elif position["exceeds_max_limit"] and not position["allow_odd_lot"]:
        priority = "B" if high_theme and above_ma200 else "C"
        final_action = "仓位过大，不适合直接买入"
        final_note = "即使研究价值存在，一手金额也超过用户设定仓位上限。"
    elif technical["short_weak_long_intact"]:
        priority = "A" if high_theme and technical["deep_pullback"] else "B"
        final_action = "等待技术企稳"
        final_note = "短线低于20/50日均线，先等待回踩结束或重新站上短中期均线。"
    elif high_theme and technical["deep_pullback"] and above_ma200 and (relative_3m is None or relative_3m >= min_relative):
        priority = "A"
        final_action = "等待财报确认"
        final_note = "技术和主题条件较强，但估值与基本面仍需人工确认；A级代表研究优先级高，不代表买入评级。"
    elif above_ma200 and (relative_3m is None or relative_3m >= min_relative):
        priority = "B"
        final_action = "重点研究" if high_theme else "小额观察"
        final_note = "部分条件满足，可进入观察池，但仍需确认估值、财报和仓位。"
    else:
        priority = "C"
        final_action = "暂不关注"
        final_note = "主题、趋势或相对强度条件不足，先等待更清晰的信号。"

    return {
        "priority": priority,
        "priority_note": f"{priority}级代表研究优先级，不代表买入评级。",
        "technical": technical,
        "theme": theme,
        "valuation": valuation,
        "fundamentals": fundamentals,
        "position": position,
        "final_action": final_action,
        "final_note": final_note,
    }


def make_combined_alert(
    alert_type: str,
    title: str,
    raw_alerts: List[Dict[str, str]],
    medium_term_view: str,
    short_term_view: str,
    recommendation: str,
) -> Dict[str, Any]:
    return {
        "type": alert_type,
        "title": title,
        "action_prefix": alert_action_prefix(alert_type),
        "action_level": trade_action_level(alert_type),
        "raw_alerts": raw_alerts,
        "medium_term_view": medium_term_view,
        "short_term_view": short_term_view,
        "recommendation": recommendation,
    }


def simple_combined_alert(raw_alert: Dict[str, str], raw_alerts: List[Dict[str, str]]) -> Dict[str, Any]:
    alert_type = raw_alert["type"]
    if alert_type == "trend_weakness":
        return make_combined_alert(
            alert_type,
            raw_alert["title"],
            raw_alerts,
            "个股已经跌破关键趋势线，位置便宜本身不足以构成买入理由。",
            "相对 TOPIX 明显走弱，短期需要把风险控制放在第一位。",
            "不建议新买入；已有仓位需要复查基本面、财报、订单和行业逻辑。",
        )
    if alert_type == "overheat_risk":
        return make_combined_alert(
            alert_type,
            raw_alert["title"],
            raw_alerts,
            "中期趋势可能仍强，但当前价格已经明显透支部分预期。",
            "短期涨幅、均线偏离或成交量拥挤，继续追高的风险较高。",
            "不建议新买入；已有仓位可考虑部分止盈，至少停止继续加仓。",
        )
    if alert_type == "deep_pullback_trend_intact":
        return make_combined_alert(
            alert_type,
            raw_alert["title"],
            raw_alerts,
            "股价深度回撤，但当前仍站在长期趋势线之上，值得重点研究。",
            "需要确认回撤是估值消化还是基本面恶化，不能只看跌幅。",
            "优先检查财报、业绩修正和行业新闻；基本面未恶化时再考虑分批观察。",
        )
    if alert_type == "pullback_watch":
        return make_combined_alert(
            alert_type,
            raw_alert["title"],
            raw_alerts,
            "股价从 52 周高点回撤到观察区间，且相对强弱没有明显恶化。",
            "短期仍需等待价格和成交量确认，不宜一次性重仓。",
            "可以加入重点观察名单；若基本面稳定，可考虑小额分批研究。",
        )
    if alert_type == "breakout_strength":
        return make_combined_alert(
            alert_type,
            raw_alert["title"],
            raw_alerts,
            "股价创出 52 周新高，且相对 TOPIX 表现较强，说明资金可能正在流入。",
            "突破后短期可能回踩或波动加大，不适合看到邮件后重仓追入。",
            "已有仓位可继续观察；没有仓位时，等待回踩 20 日线/50 日线或小仓试探。",
        )
    return make_combined_alert(
        alert_type,
        raw_alert["title"],
        raw_alerts,
        "该信号仅表示值得进入观察范围。",
        "短期方向仍需结合价格、成交量和市场环境复查。",
        "不自动买入；先检查基本面、估值、财报和行业消息。",
    )


def combine_stock_alerts(
    stock: StockInfo,
    indicators: Dict[str, Any],
    raw_alerts: List[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    """Merge raw stock alerts into one non-conflicting stock-level alert."""
    if not raw_alerts:
        return None

    raw_types = {alert["type"] for alert in raw_alerts}
    if "trend_weakness" in raw_types and "deep_pullback_trend_intact" in raw_types:
        return make_combined_alert(
            "weak_deep_pullback",
            "深度回撤但趋势转弱，谨慎复查",
            raw_alerts,
            "股价从 52 周高点深度回撤，位置可能值得跟踪。",
            "当前跌破关键均线，且相对 TOPIX 明显走弱，不能简单理解为便宜。",
            "不建议新买入；已有仓位应复查基本面、财报、订单和行业逻辑。",
        )
    if "pullback_watch" in raw_types and "overheat_risk" in raw_types:
        return make_combined_alert(
            "pullback_but_overheated",
            "回撤后修复，但短线过热",
            raw_alerts,
            "股价仍明显低于 52 周高点，并重新站上或接近 200 日线，可以加入观察名单。",
            "近期涨幅较大且成交量放大，说明短线可能拥挤，不适合追高。",
            "等待回踩 20 日线 / 50 日线、成交量降温，或下一次财报确认基本面后再评估。",
        )
    if "breakout_strength" in raw_types and "overheat_risk" in raw_types:
        return make_combined_alert(
            "breakout_but_overheated",
            "强势突破但短线过热",
            raw_alerts,
            "股价创新高并有资金流入迹象。",
            "短期涨幅和成交量可能过热，新仓追高的风险较高。",
            "已有仓位可以继续观察；不建议新仓重仓追高。",
        )

    priority = [
        "trend_weakness",
        "overheat_risk",
        "deep_pullback_trend_intact",
        "pullback_watch",
        "breakout_strength",
    ]
    raw_by_type = {alert["type"]: alert for alert in raw_alerts}
    for alert_type in priority:
        if alert_type in raw_by_type:
            return simple_combined_alert(raw_by_type[alert_type], raw_alerts)

    return simple_combined_alert(raw_alerts[0], raw_alerts)


def build_raw_alert_lines(raw_alerts: List[Dict[str, str]]) -> str:
    return "\n".join(f"- {alert['type']}：{alert['title']}" for alert in raw_alerts) or "- 无"


def build_email_body(stock: StockInfo, indicators: Dict[str, Any], combined_alert: Dict[str, Any]) -> str:
    above_ma200 = "是" if indicators.get("above_ma200") else "否"
    recent_cross = "是" if indicators.get("recent_cross_above_ma200") else "否"
    volume_spike = "是" if indicators.get("volume_spike") else "否"

    return f"""提醒类型：{combined_alert["title"]}
操作等级：{combined_alert["action_level"]}

股票代码：{stock.ticker}
股票名称：{stock.name}
产业分类：{stock.sector}
数据日期：{indicators.get("last_date", "N/A")}

核心指标：
当前价格：{fmt_num(indicators.get("current_price"))}
52周最高收盘价：{fmt_num(indicators.get("high_52w"))}
从52周高点回撤：{fmt_pct(indicators.get("drawdown_pct"))}

20日均线：{fmt_num(indicators.get("ma20"))}
50日均线：{fmt_num(indicators.get("ma50"))}
200日均线：{fmt_num(indicators.get("ma200"))}
当前价格偏离200日均线：{fmt_pct(indicators.get("above_ma200_pct"))}
当前价格高于200日均线：{above_ma200}
最近5个交易日重新站上200日均线：{recent_cross}

过去1个月涨幅：{fmt_pct(indicators.get("return_1m_pct"))}
过去3个月涨幅：{fmt_pct(indicators.get("return_3m_pct"))}
过去6个月涨幅：{fmt_pct(indicators.get("return_6m_pct"))}
过去60日涨幅：{fmt_pct(indicators.get("return_60d_pct"))}
过去3个月TOPIX涨幅：{fmt_pct(indicators.get("topix_return_3m_pct"))}
过去3个月相对TOPIX超额收益：{fmt_pct(indicators.get("relative_topix_3m_pct"))}
过去20日相对TOPIX超额收益：{fmt_pct(indicators.get("relative_topix_20d_pct"))}

当前成交量：{fmt_num(indicators.get("current_volume"))}
过去20日平均成交量：{fmt_num(indicators.get("avg_volume_20d"))}
成交量超过20日均量1.5倍：{volume_spike}
最近3日放量天数：{indicators.get("recent_volume_spike_days", 0)}
过去20日平均成交额（日元）：{fmt_num(indicators.get("avg_turnover_20d"))}

原始触发信号：
{build_raw_alert_lines(combined_alert.get("raw_alerts", []))}

综合判断：
中期判断：
{combined_alert["medium_term_view"]}

短期判断：
{combined_alert["short_term_view"]}

建议动作：
{combined_alert["recommendation"]}

提醒：这不是自动交易，也不是确定买卖指令，只是观察名单提醒，需要人工确认。
"""


def build_raw_alert_lines(raw_alerts: List[Dict[str, str]]) -> str:
    return "\n".join(f"- {alert['type']}：{alert['title']}" for alert in raw_alerts) or "- 无"


def build_email_body(
    stock: StockInfo,
    indicators: Dict[str, Any],
    combined_alert: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> str:
    analysis = combined_alert.get("research_analysis") or build_research_analysis(stock, indicators, config)
    technical = analysis["technical"]
    theme = analysis["theme"]
    valuation = analysis["valuation"]
    fundamentals = analysis["fundamentals"]
    position = analysis["position"]

    return f"""【研究提醒】
股票代码：{stock.ticker}
股票名称：{stock.name}
行业主题：{stock.sector}
数据日期：{indicators.get("last_date", "N/A")}

一、研究优先级：

* {analysis["priority"]}
* 说明：{analysis["priority_note"]} A级代表研究优先级高，不代表买入评级。
* 原始触发信号：
{build_raw_alert_lines(combined_alert.get("raw_alerts", []))}

二、技术状态：

* 当前价格：{fmt_num(indicators.get("current_price"))}
* 52周高点：{fmt_num(indicators.get("high_52w"))}
* 从52周高点回撤：{fmt_pct(indicators.get("drawdown_pct"))}
* 是否达到深度回撤标准：{bool_ja(technical["deep_pullback"])}
* 说明：回撤不等于低估，只代表价格从高点明显降温。
* 20日均线：{fmt_num(indicators.get("ma20"))}
* 50日均线：{fmt_num(indicators.get("ma50"))}
* 200日均线：{fmt_num(indicators.get("ma200"))}
* 当前价格是否高于20日均线：{bool_ja(indicators.get("above_ma20"))}
* 当前价格是否高于50日均线：{bool_ja(indicators.get("above_ma50"))}
* 当前价格是否高于200日均线：{bool_ja(indicators.get("above_ma200"))}
* 当前价格距离200日均线：{fmt_pct(indicators.get("above_ma200_pct"))}
* 最近5个交易日是否重新站上200日均线：{bool_ja(indicators.get("recent_cross_above_ma200"))}
* 过去1个月涨幅：{fmt_pct(indicators.get("return_1m_pct"))}
* 过去3个月涨幅：{fmt_pct(indicators.get("return_3m_pct"))}
* 过去6个月涨幅：{fmt_pct(indicators.get("return_6m_pct"))}
* 过去60日涨幅：{fmt_pct(indicators.get("return_60d_pct"))}
* 过去3个月TOPIX涨幅：{fmt_pct(indicators.get("topix_return_3m_pct"))}
* 过去3个月相对TOPIX：{fmt_pct(indicators.get("relative_topix_3m_pct"))}
* 过去20日相对TOPIX：{fmt_pct(indicators.get("relative_topix_20d_pct"))}
* 技术分类：{technical["classification"]}
* 技术解读：{technical["interpretation"]}

三、主题相关性：

* 主题相关性：{theme["relevance"]}
* 相关逻辑：{theme["logic"]}
* 是否需要人工确认业务收入占比：{theme["need_confirm"]}

四、估值风险：

* PER：{valuation["per"]}
* PBR：{valuation["pbr"]}
* PSR：{valuation["psr"]}
* EV/EBITDA：{valuation["ev_ebitda"]}
* ROE：{valuation["roe"]}
* 营业利润率：{valuation["operating_margin"]}
* 过去5年估值分位：{valuation["historical_percentile_5y"]}
* 估值判断：{valuation["judgement"]}
* 风险提示：{valuation["risk"]}

五、基本面确认：

* 最近季度营收增长：{fundamentals["revenue_growth"]}
* 最近季度营业利润增长：{fundamentals["operating_profit_growth"]}
* 最近季度净利润增长：{fundamentals["net_profit_growth"]}
* 最近是否上修/下修业绩：{fundamentals["guidance_revision"]}
* 营业利润率变化：{fundamentals["operating_margin_change"]}
* 自由现金流变化：{fundamentals["free_cash_flow_change"]}
* 主题需求说明：{fundamentals["theme_demand_comment"]}
* 基本面状态：{fundamentals["status"]}
* 业绩是否支持股价主题：{fundamentals["support_theme"]}
* 需要人工查看的资料：{fundamentals["materials"]}
* 说明：{fundamentals["note"]}

六、仓位可执行性：

* 当前价格：{fmt_num(position["price"])}
* 交易单位：{position["lot_size"]}股
* 一手金额：{money_jpy(position["lot_amount"])}
* 用户投资资金：{money_jpy(position["capital"])}
* 一手金额占比：{fmt_pct(position["lot_pct"])}
* 是否超过单股初始仓位上限：{bool_ja(position["exceeds_initial_limit"])}
* 是否超过单股最大仓位上限：{bool_ja(position["exceeds_max_limit"])}
* 是否允许単元未満株 / S株：{bool_ja(position["allow_odd_lot"])}
* 仓位建议：{position["advice"]}

七、最终操作建议：

* {analysis["final_action"]}
* 说明：{analysis["final_note"]}

八、提醒：

* 本脚本只做观察，不自动交易。
* 回撤不等于低估。
* A级不等于买入评级。
* 个股不进入主回撤加仓表。
* 主策略仍应以指数定投和规则化回撤加仓为核心。
* 这不是投资建议，也不是确定买卖指令，需要人工确认。"""


def check_sector_heat_conditions(
    sector_results: Dict[str, List[Tuple[StockInfo, Dict[str, Any]]]],
    thresholds: Dict[str, Any],
    state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = []
    min_ratio = float(thresholds.get("sector_heat_60d_high_ratio", 0.3))
    min_relative_20d = float(thresholds.get("sector_heat_relative_20d_min_pct", 5))

    for sector, results in sector_results.items():
        valid_results = [
            (stock, indicators)
            for stock, indicators in results
            if indicators.get("relative_topix_20d_pct") is not None
        ]
        if not valid_results:
            continue

        high_60d_stocks = [(stock, indicators) for stock, indicators in valid_results if indicators.get("is_60d_high")]
        outperformers = [
            (stock, indicators)
            for stock, indicators in valid_results
            if indicators.get("relative_topix_20d_pct") >= min_relative_20d
        ]
        high_60d_ratio = len(high_60d_stocks) / len(valid_results)

        if high_60d_ratio >= min_ratio or len(outperformers) / len(valid_results) >= min_ratio:
            pseudo_indicators = {
                "drawdown_pct": 0,
                "is_52w_high": False,
            }
            alert_type = "sector_heat"
            if should_send_alert(state, sector, alert_type, pseudo_indicators, thresholds):
                alerts.append(
                    {
                        "sector": sector,
                        "valid_count": len(valid_results),
                        "high_60d_stocks": high_60d_stocks,
                        "outperformers": outperformers,
                        "high_60d_ratio": high_60d_ratio,
                        "outperformer_ratio": len(outperformers) / len(valid_results),
                        "type": alert_type,
                        "title": "行业热度提醒",
                    }
                )

    return alerts


def build_sector_heat_email_body(alert: Dict[str, Any]) -> str:
    def format_stock_line(item: Tuple[StockInfo, Dict[str, Any]]) -> str:
        stock, indicators = item
        return (
            f"- {stock.ticker} {stock.name}: "
            f"20日相对TOPIX {fmt_pct(indicators.get('relative_topix_20d_pct'))}, "
            f"60日涨幅 {fmt_pct(indicators.get('return_60d_pct'))}"
        )

    high_60d_lines = "\n".join(format_stock_line(item) for item in alert["high_60d_stocks"]) or "- 无"
    outperformer_lines = "\n".join(format_stock_line(item) for item in alert["outperformers"]) or "- 无"

    return f"""提醒类型：{alert["title"]}
操作等级：{trade_action_level(alert["type"])}

产业分类：{alert["sector"]}
有效样本数：{alert["valid_count"]}
创60日新高比例：{fmt_pct(alert["high_60d_ratio"] * 100)}
20日明显跑赢TOPIX比例：{fmt_pct(alert["outperformer_ratio"] * 100)}

创60日新高股票：
{high_60d_lines}

过去20日相对TOPIX超过阈值股票：
{outperformer_lines}

{build_sector_recommendation(alert)}
提醒：这是行业层面的热度观察，不是自动交易，也不是确定买卖指令，需要人工确认。
"""


def print_report(rows: List[Dict[str, Any]]) -> None:
    """Print a compact indicator report even when no alert is triggered."""
    if not rows:
        print("No valid stock data to report.")
        return

    headers = [
        "ticker",
        "name",
        "sector",
        "price",
        "drawdown",
        "above200",
        "rel3m",
        "turnover20d_jpy",
        "raw_alerts",
        "combined",
    ]
    table_rows = []
    for row in rows:
        indicators = row["indicators"]
        raw_alerts = row["raw_alerts"]
        combined_alert = row["combined_alert"]
        table_rows.append(
            {
                "ticker": row["stock"].ticker,
                "name": row["stock"].name,
                "sector": row["stock"].sector,
                "price": fmt_num(indicators.get("current_price")),
                "drawdown": fmt_pct(indicators.get("drawdown_pct")),
                "above200": "Y" if indicators.get("above_ma200") else "N",
                "rel3m": fmt_pct(indicators.get("relative_topix_3m_pct")),
                "turnover20d_jpy": fmt_num(indicators.get("avg_turnover_20d")),
                "raw_alerts": ",".join(alert["type"] for alert in raw_alerts) if raw_alerts else "-",
                "combined": combined_alert["type"] if combined_alert else "-",
            }
        )

    widths = {
        header: max(len(header), *(len(str(row[header])) for row in table_rows))
        for header in headers
    }
    print(" | ".join(header.ljust(widths[header]) for header in headers))
    print("-+-".join("-" * widths[header] for header in headers))
    for row in table_rows:
        print(" | ".join(str(row[header]).ljust(widths[header]) for header in headers))


def send_email(config: Dict[str, Any], subject: str, body: str) -> None:
    smtp_config = config["smtp"]
    email_config = config["email"]
    password = os.environ.get("SMTP_PASSWORD")
    if not password:
        raise RuntimeError("SMTP_PASSWORD environment variable is not set")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = email_config["from"]
    msg["To"] = ", ".join(email_config["to"])

    host = smtp_config["host"]
    port = int(smtp_config.get("port", 587))
    use_tls = bool(smtp_config.get("use_tls", True))

    with smtplib.SMTP(host, port, timeout=30) as server:
        if use_tls:
            server.starttls()
        server.login(smtp_config["username"], password)
        server.sendmail(email_config["from"], email_config["to"], msg.as_string())


def fetch_topix_data(candidates: List[str]) -> Tuple[str, pd.DataFrame]:
    for ticker in candidates:
        data = fetch_price_data(ticker)
        if data is not None and len(data) >= 70:
            logging.info("Using %s as TOPIX benchmark", ticker)
            return ticker, data
        logging.warning("TOPIX candidate %s unavailable, trying next", ticker)
    raise RuntimeError("No usable TOPIX benchmark data found")


def build_test_email_body() -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S %Z")
    return f"""日本股票监控脚本 SMTP 测试邮件

发送时间：{now}

如果你收到这封邮件，说明 SMTP 配置和 SMTP_PASSWORD 环境变量可用。
"""


def build_summary_email_body(
    benchmark_ticker: str,
    scanned_count: int,
    success_count: int,
    failed_count: int,
    raw_alert_count: int,
    combined_alerts: List[Dict[str, Any]],
    sent_stock_count: int,
    sector_alert_count: int,
    sent_sector_count: int,
    dry_run: bool,
    sector_alerts: List[Dict[str, Any]],
    summary_scope: str,
) -> str:
    """Build the daily run summary email body."""
    run_time = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    if combined_alerts:
        alert_lines = "\n".join(
            f"- {item['stock'].ticker} {item['stock'].name}: "
            f"{item['combined_alert']['title']} / {item['combined_alert']['action_level']}"
            for item in combined_alerts
        )
    else:
        alert_lines = "今日无触发提醒。"

    if sector_alerts:
        sector_lines = "\n".join(
            f"- {alert['sector']}: {alert['title']} "
            f"(60日新高比例 {fmt_pct(alert['high_60d_ratio'] * 100)}, "
            f"20日跑赢比例 {fmt_pct(alert['outperformer_ratio'] * 100)})"
            for alert in sector_alerts
        )
    else:
        sector_lines = "今日无行业热度提醒。"

    return f"""日本股票监控每日运行摘要

运行时间 JST：{run_time}
Benchmark ticker：{benchmark_ticker}
扫描股票数量：{scanned_count}
成功取得数据的股票数量：{success_count}
数据获取失败数量：{failed_count}
触发 raw alert 数量：{raw_alert_count}
触发 combined alert 数量：{len(combined_alerts)}
实际发送个股提醒邮件数量：{sent_stock_count}
触发 sector heat 数量：{sector_alert_count}
实际发送行业提醒邮件数量：{sent_sector_count}
dry_run 状态：{dry_run}

综合提醒摘要（{summary_scope}）：
{alert_lines}

行业热度摘要：
{sector_lines}

提醒：这不是投资建议，不是自动交易，也不是确定买卖指令，只是观察名单提醒，需要人工确认。
"""


def send_summary_email_if_needed(config: Dict[str, Any], args: argparse.Namespace, body: str) -> None:
    """Send or print the daily summary when --summary-email is requested."""
    if not args.summary_email:
        return

    subject = f"[日本股票监控] 每日运行摘要 - {datetime.now(JST).strftime('%Y-%m-%d')}"
    if args.dry_run:
        print("=" * 80)
        print(subject)
        print(body)
        return

    send_email(config, subject, body)
    logging.info("Summary email sent")


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor Japanese stock pullbacks and relative strength.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts without sending email")
    parser.add_argument("--report", action="store_true", help="Print a compact indicator report for all processed stocks")
    parser.add_argument("--summary-email", action="store_true", help="Send a daily run summary email")
    parser.add_argument("--test-email", action="store_true", help="Send a test email and exit")
    parser.add_argument("--export-signals", action="store_true", help="Write combined stock alerts to the signal CSV log")
    parser.add_argument("--signal-log", default=None, help="Override signal CSV log path")
    parser.add_argument("--log-signals-dry-run", action="store_true", help="Allow dry-run to write signal CSV rows")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.get("log_file", "monitor.log"))
    run_datetime = datetime.now(JST)
    summary_date = run_datetime.strftime("%Y-%m-%d")
    signal_settings = signal_log_settings(config, args)
    email_policy = email_policy_settings(config)
    summary_requested = bool(args.summary_email or email_policy["daily_summary_default"])
    should_write_signal_log = signal_settings["enabled"] and (not args.dry_run or args.log_signals_dry_run)
    logging.info(
        "Signal log %s. path=%s dry_run_write=%s",
        "enabled" if signal_settings["enabled"] else "disabled",
        signal_settings["path"],
        bool(args.log_signals_dry_run),
    )

    if args.test_email:
        send_email(config, "[日本股票监控] SMTP 测试", build_test_email_body())
        logging.info("Test email sent")
        return

    state_file = config.get("state_file", "alert_state.json")
    state = load_state(state_file)
    thresholds = config["thresholds"]
    prune_old_state(state, int(thresholds.get("dedup_days", 30)))
    if is_stateful_normal_run(args) and daily_run_completed(state, summary_date):
        logging.info("Daily run for %s already completed; exiting without sending mail.", summary_date)
        return

    topix_ticker, topix_data = fetch_topix_data(config.get("topix_candidates", ["^TOPX", "1306.T"]))
    stocks = flatten_stock_pool(config)
    sector_results: Dict[str, List[Tuple[StockInfo, Dict[str, Any]]]] = {}
    report_rows: List[Dict[str, Any]] = []
    stock_alert_items: List[Dict[str, Any]] = []
    notify_alert_items: List[Dict[str, Any]] = []
    signal_log_rows: List[Dict[str, Any]] = []
    raw_alert_count = 0
    success_count = 0
    failed_count = 0
    sent_stock_count = 0
    sent_sector_count = 0
    summary_only = args.summary_email and not args.dry_run and not args.report

    for stock in stocks:
        logging.info("Processing %s %s", stock.ticker, stock.name)
        data = fetch_price_data(stock.ticker)
        if data is None:
            failed_count += 1
            continue

        try:
            indicators = calculate_indicators(data)
            indicators.update(calculate_relative_strength(indicators, topix_data))
            success_count += 1
            sector_results.setdefault(stock.sector, []).append((stock, indicators))
            raw_alerts = check_alert_conditions(stock, indicators, thresholds, state)
            raw_alert_count += len(raw_alerts)
            combined_alert = combine_stock_alerts(stock, indicators, raw_alerts)
            if combined_alert:
                research_analysis = build_research_analysis(stock, indicators, config)
                combined_alert["research_analysis"] = research_analysis
                combined_alert["action_level"] = (
                    f"{research_analysis['priority']}：研究优先级。"
                    "不代表买入评级，仍需人工确认估值、财报和仓位。"
                )
            report_rows.append(
                {
                    "stock": stock,
                    "indicators": indicators,
                    "raw_alerts": raw_alerts,
                    "combined_alert": combined_alert,
                }
            )
        except Exception as exc:
            logging.exception("Failed to process %s: %s", stock.ticker, exc)
            failed_count += 1
            continue

        if not combined_alert:
            logging.info("No alert for %s", stock.ticker)
            continue

        if not should_send_alert(state, stock.ticker, combined_alert["type"], indicators, thresholds):
            logging.info("Alert for %s %s suppressed by dedup state", stock.ticker, combined_alert["type"])
            continue

        stock_alert_items.append(
            {
                "stock": stock,
                "indicators": indicators,
                "combined_alert": combined_alert,
            }
        )
        if is_notify_level(combined_alert, email_policy):
            notify_alert_items.append(
                {
                    "stock": stock,
                    "indicators": indicators,
                    "combined_alert": combined_alert,
                }
            )
        signal_log_rows.append(build_signal_log_row(stock, indicators, combined_alert, run_datetime))
        prefix = combined_alert["action_prefix"]
        subject = f"[日本股票监控][{prefix}] {combined_alert['title']} - {stock.ticker} {stock.name}"
        body = build_email_body(stock, indicators, combined_alert, config)
        send_individual = should_send_individual_alert(combined_alert, email_policy)

        if args.dry_run and send_individual:
            print("=" * 80)
            print(subject)
            print(body)
        elif args.dry_run:
            logging.info("Individual email suppressed by policy for %s %s", stock.ticker, combined_alert["type"])
        elif not args.report and not summary_only and send_individual:
            try:
                send_email(config, subject, body)
                sent_stock_count += 1
                record_alert_state(state, stock.ticker, combined_alert["type"], indicators)
                logging.info("Email sent for %s %s", stock.ticker, combined_alert["type"])
            except Exception as exc:
                logging.exception("Failed to send email for %s: %s", stock.ticker, exc)
                continue
        else:
            logging.info("Individual email suppressed by policy for %s %s", stock.ticker, combined_alert["type"])

    sector_alerts = check_sector_heat_conditions(sector_results, thresholds, state)
    for sector_alert in sector_alerts:
        prefix = alert_action_prefix(sector_alert["type"])
        subject = f"[日本股票监控][{prefix}] {sector_alert['title']} - {sector_alert['sector']}"
        body = build_sector_heat_email_body(sector_alert)

        send_sector_individual = bool(email_policy["send_sector_heat_individual"])
        if args.dry_run and send_sector_individual:
            print("=" * 80)
            print(subject)
            print(body)
        elif args.dry_run:
            logging.info("Sector heat individual email suppressed by policy for %s", sector_alert["sector"])
        elif not args.report and not summary_only and send_sector_individual:
            try:
                send_email(config, subject, body)
                sent_sector_count += 1
                record_alert_state(
                    state,
                    sector_alert["sector"],
                    sector_alert["type"],
                    {"drawdown_pct": 0, "high_52w": None, "current_price": None},
                )
                logging.info("Sector heat email sent for %s", sector_alert["sector"])
            except Exception as exc:
                logging.exception("Failed to send sector heat email for %s: %s", sector_alert["sector"], exc)
                continue
        else:
            logging.info("Sector heat individual email suppressed by policy for %s", sector_alert["sector"])

    if args.report:
        print_report(report_rows)

    summary_alert_items = stock_alert_items if args.summary_email else notify_alert_items
    summary_sector_alerts = sector_alerts if args.summary_email else []
    summary_body = build_summary_email_body(
        benchmark_ticker=topix_ticker,
        scanned_count=len(stocks),
        success_count=success_count,
        failed_count=failed_count,
        raw_alert_count=raw_alert_count,
        combined_alerts=summary_alert_items,
        sent_stock_count=sent_stock_count,
        sector_alert_count=len(sector_alerts),
        sent_sector_count=sent_sector_count,
        dry_run=args.dry_run,
        sector_alerts=summary_sector_alerts,
        summary_scope="全部信号" if args.summary_email else "A/B通知信号",
    )
    summary_sent = False
    should_send_summary = summary_requested and (
        args.summary_email
        or bool(summary_alert_items)
        or email_policy["send_summary_when_no_notify_alerts"]
    )
    if summary_requested and not should_send_summary:
        logging.info("Summary email suppressed because there are no notify-level alerts.")

    if should_send_summary:
        if args.dry_run:
            print("=" * 80)
            print(f"[日本股票监控] 每日运行摘要 - {summary_date}")
            print(summary_body)
        elif not args.report:
            if not args.summary_email and summary_email_already_sent(state, summary_date):
                logging.info("Summary email for %s already sent; suppressed duplicate.", summary_date)
            else:
                send_email(config, f"[日本股票监控] 每日运行摘要 - {summary_date}", summary_body)
                record_summary_email_state(state, summary_date)
                summary_sent = True
                logging.info("Summary email sent")

    logging.info("Signal log rows prepared: %d", len(signal_log_rows))
    if should_write_signal_log:
        existing_count = len(load_existing_signal_ids(signal_settings["path"]))
        appended_count = append_signal_log_rows(signal_settings["path"], signal_log_rows)
        skipped_count = max(len(signal_log_rows) - appended_count, 0)
        logging.info("Signal log path: %s", signal_settings["path"])
        logging.info("Signal log rows appended: %d", appended_count)
        logging.info("Signal log duplicate rows skipped: %d", skipped_count)
        logging.info("Signal log existing ids before append: %d", existing_count)
    else:
        logging.info("Signal log disabled for this run.")

    if not (args.dry_run or args.report or summary_only):
        record_daily_run_state(state, summary_date, summary_sent, sent_stock_count)

    if args.dry_run or args.report or summary_only:
        logging.info("Dry run: state not saved." if args.dry_run else "Read-only run: state not saved.")
    else:
        save_state(state, state_file)

    logging.info(
        "Finished. Benchmark=%s, raw_triggered=%d, combined_triggered=%d, stock_sent=%d, sector_triggered=%d, sector_sent=%d, dry_run=%s",
        topix_ticker,
        raw_alert_count,
        len(stock_alert_items),
        sent_stock_count,
        len(sector_alerts),
        sent_sector_count,
        args.dry_run,
        )


if __name__ == "__main__":
    main()
