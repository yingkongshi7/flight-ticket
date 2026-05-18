#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略 2.0 执行提醒脚本。

功能：
- 使用 yfinance 获取 S&P 500、Nasdaq Composite、SOX、VIX 日线数据
- 用收盘价计算 S&P 500 / Nasdaq 从高点以来的回撤
- S&P 500 或 Nasdaq 达到指定回撤档位时发送邮件提醒
- VIX 只作为恐慌辅助提醒，不单独触发加仓
- SOX 只作为辅助显示，不单独触发加仓
- 使用本地 JSON 文件记录已提醒档位，避免重复提醒

注意：本脚本只发送提醒，不连接券商 API，不自动下单。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
import yfinance as yf


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.yaml"
DEFAULT_STATE_PATH = SCRIPT_DIR / "triggered_levels.json"
DEFAULT_LOG_PATH = SCRIPT_DIR / "monitor.log"


@dataclass
class IndexSnapshot:
    key: str
    name: str
    ticker: str
    current_close: float
    high_close: float
    drawdown_pct: float
    close_date: str
    previous_close: float | None = None
    daily_change_pct: float | None = None


def setup_logging(log_path: Path) -> None:
    """同时输出到终端和日志文件。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"找不到配置文件：{config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    return config


def load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"triggered_levels": [], "pending_confirm_levels": [], "expired_pending_levels": [], "last_highs": {}}

    try:
        with state_path.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except json.JSONDecodeError:
        logging.warning("状态文件 JSON 格式异常，将使用空状态：%s", state_path)
        return {"triggered_levels": [], "pending_confirm_levels": [], "expired_pending_levels": [], "last_highs": {}}

    state.setdefault("triggered_levels", [])
    state.setdefault("pending_confirm_levels", [])
    state.setdefault("expired_pending_levels", [])
    state.setdefault("last_highs", {})
    return state


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def normalize_tickers(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        return [value]
    return value


def fetch_index_snapshot(
    key: str,
    index_config: dict[str, Any],
    lookback_period: str,
) -> IndexSnapshot:
    """获取单个指数数据。ticker 可以配置多个，按顺序尝试。"""
    name = index_config["name"]
    tickers = normalize_tickers(index_config["ticker"])
    last_error: Exception | None = None

    for ticker in tickers:
        try:
            logging.info("正在获取 %s 数据：%s", name, ticker)
            data = yf.download(
                ticker,
                period=lookback_period,
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
            )

            if data.empty:
                raise ValueError("返回数据为空")

            close = extract_close_series(data)
            close = close.dropna()

            if close.empty:
                raise ValueError("收盘价为空")

            current_close = float(close.iloc[-1])
            previous_close = float(close.iloc[-2]) if len(close) >= 2 else None
            high_close = float(close.max())
            close_date = close.index[-1].strftime("%Y-%m-%d")
            drawdown_pct = (high_close - current_close) / high_close * 100
            daily_change_pct = (
                (current_close - previous_close) / previous_close * 100
                if previous_close
                else None
            )

            return IndexSnapshot(
                key=key,
                name=name,
                ticker=ticker,
                current_close=current_close,
                high_close=high_close,
                drawdown_pct=drawdown_pct,
                close_date=close_date,
                previous_close=previous_close,
                daily_change_pct=daily_change_pct,
            )
        except Exception as exc:  # noqa: BLE001 - 单个 ticker 失败不应拖垮全局
            last_error = exc
            logging.warning("%s 数据获取失败，ticker=%s，原因：%s", name, ticker, exc)

    raise RuntimeError(f"{name} 所有 ticker 都获取失败：{last_error}")


def extract_close_series(data: pd.DataFrame) -> pd.Series:
    """兼容 yfinance 单 ticker 和多层列格式。"""
    if isinstance(data.columns, pd.MultiIndex):
        if "Close" in data.columns.get_level_values(0):
            close_data = data["Close"]
            if isinstance(close_data, pd.DataFrame):
                return close_data.iloc[:, 0]
            return close_data
        raise ValueError("数据中找不到 Close 列")

    if "Close" not in data.columns:
        raise ValueError("数据中找不到 Close 列")

    return data["Close"]


def fetch_all_snapshots(config: dict[str, Any]) -> dict[str, IndexSnapshot]:
    lookback_period = config.get("lookback_period", "5y")
    snapshots: dict[str, IndexSnapshot] = {}

    for key, index_config in config["indices"].items():
        try:
            snapshots[key] = fetch_index_snapshot(key, index_config, lookback_period)
        except Exception as exc:  # noqa: BLE001
            if index_config.get("optional", False):
                logging.warning("可选指数 %s 获取失败，将继续运行：%s", key, exc)
                continue
            logging.error("必要指数 %s 获取失败：%s", key, exc)

    return snapshots


def update_state_for_new_high(
    state: dict[str, Any],
    snapshots: dict[str, IndexSnapshot],
    primary_keys: list[str],
    reset_on_new_high: bool,
) -> bool:
    """记录最新高点；如果配置允许，在主要指数创新高时重置已触发档位。"""
    last_highs = state.setdefault("last_highs", {})
    found_new_high = False

    for key in primary_keys:
        snapshot = snapshots.get(key)
        if snapshot is None:
            continue

        previous_high = float(last_highs.get(key, 0) or 0)
        if previous_high > 0 and snapshot.high_close > previous_high:
            found_new_high = True
            logging.info(
                "%s 最近高点更新：%.2f -> %.2f",
                snapshot.name,
                previous_high,
                snapshot.high_close,
            )
        last_highs[key] = snapshot.high_close

    if found_new_high and reset_on_new_high:
        state["triggered_levels"] = []
        logging.info("检测到主要指数创新高，已根据 reset_on_new_high=true 重置已触发档位")
        return True

    return found_new_high


def level_value(level: dict[str, Any]) -> float:
    return round(float(level["drawdown_pct"]), 1)


def normalize_triggered_levels(triggered_levels: list[Any]) -> set[float]:
    normalized: set[float] = set()
    for level in triggered_levels:
        try:
            normalized.add(round(float(level), 1))
        except (TypeError, ValueError):
            logging.warning("忽略无法识别的已触发档位：%s", level)
    return normalized


def format_level(level_pct: float) -> str:
    return f"-{level_pct:.1f}%"


def format_man_yen(amount: float) -> str:
    if float(amount).is_integer():
        return f"{int(amount)}万日元"
    return f"{amount:.1f}万日元"


def primary_drawdowns(
    snapshots: dict[str, IndexSnapshot],
    primary_keys: list[str],
) -> dict[str, float]:
    return {
        key: snapshots[key].drawdown_pct
        for key in primary_keys
        if key in snapshots
    }


def get_triggering_indices(
    snapshots: dict[str, IndexSnapshot],
    primary_keys: list[str],
    level_pct: float,
) -> str:
    triggered_names = []
    for key in primary_keys:
        snapshot = snapshots.get(key)
        if snapshot is not None and snapshot.drawdown_pct >= level_pct:
            if key == "sp500":
                triggered_names.append("S&P500")
            elif key == "nasdaq":
                triggered_names.append("Nasdaq")
            else:
                triggered_names.append(snapshot.name)

    if len(triggered_names) >= 2:
        return "两者同时"
    if triggered_names:
        return triggered_names[0]
    return "未触发"


def aggregate_allocations(levels: list[dict[str, Any]]) -> dict[str, float]:
    fund_order = [
        "SBI・V・S&P500",
        "eMAXIS Slim オルカン",
        "ニッセイNASDAQ100",
        "ニッセイSOX",
        "ニッセイTOPIX",
    ]
    totals = {fund: 0.0 for fund in fund_order}
    for level in levels:
        for fund, amount in level.get("allocations", {}).items():
            totals[fund] = totals.get(fund, 0.0) + float(amount)
    return totals


def total_amount(levels: list[dict[str, Any]]) -> float:
    return sum(float(level["total_amount_man_yen"]) for level in levels)


def triggered_amount(
    levels: list[dict[str, Any]],
    triggered_levels: list[Any],
) -> float:
    triggered_set = normalize_triggered_levels(triggered_levels)
    return sum(
        float(level["total_amount_man_yen"])
        for level in levels
        if level_value(level) in triggered_set
    )


def strategy_total_amount(levels: list[dict[str, Any]]) -> float:
    return sum(float(level["total_amount_man_yen"]) for level in levels)


def choose_trigger_levels(
    snapshots: dict[str, IndexSnapshot],
    primary_keys: list[str],
    levels: list[dict[str, Any]],
    triggered_levels: list[Any],
) -> list[dict[str, Any]]:
    drawdowns = primary_drawdowns(snapshots, primary_keys)

    if not drawdowns:
        logging.error("S&P 500 和 Nasdaq 数据均不可用，无法判断触发档位")
        return []

    max_primary_drawdown = max(drawdowns.values())
    triggered_set = normalize_triggered_levels(triggered_levels)

    reached_levels = [
        level
        for level in levels
        if max_primary_drawdown >= level_value(level)
        and level_value(level) not in triggered_set
    ]

    if not reached_levels:
        logging.info(
            "当前主要指数最大回撤 %.2f%%，没有新的触发档位",
            max_primary_drawdown,
        )

    return sorted(reached_levels, key=level_value)


def choose_trigger_level(
    snapshots: dict[str, IndexSnapshot],
    primary_keys: list[str],
    levels: list[dict[str, Any]],
    triggered_levels: list[Any],
) -> dict[str, Any] | None:
    """兼容旧调用：返回当前达到的最深、且还没有提醒过的档位。"""
    reached_levels = choose_trigger_levels(
        snapshots=snapshots,
        primary_keys=primary_keys,
        levels=levels,
        triggered_levels=triggered_levels,
    )
    if not reached_levels:
        return None

    return max(reached_levels, key=level_value)


def filter_levels_by_values(
    levels: list[dict[str, Any]],
    level_values: set[float],
) -> list[dict[str, Any]]:
    return [
        level
        for level in levels
        if level_value(level) in level_values
    ]


def split_pending_confirm_levels(
    snapshots: dict[str, IndexSnapshot],
    primary_keys: list[str],
    pending_levels: list[Any],
) -> tuple[set[float], set[float]]:
    drawdowns = primary_drawdowns(snapshots, primary_keys)
    if not drawdowns:
        return set(), normalize_triggered_levels(pending_levels)

    max_primary_drawdown = max(drawdowns.values())
    pending_set = normalize_triggered_levels(pending_levels)
    confirmed = {level for level in pending_set if max_primary_drawdown >= level}
    expired = pending_set - confirmed
    return confirmed, expired


def is_extreme_move(
    snapshots: dict[str, IndexSnapshot],
    vix_status: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons = []
    nasdaq = snapshots.get("nasdaq")
    sp500 = snapshots.get("sp500")
    vxn = snapshots.get("vxn")

    if nasdaq and nasdaq.daily_change_pct is not None and nasdaq.daily_change_pct <= -5:
        reasons.append(f"Nasdaq 单日跌幅 {nasdaq.daily_change_pct:.2f}%")
    if sp500 and sp500.daily_change_pct is not None and sp500.daily_change_pct <= -4:
        reasons.append(f"S&P500 单日跌幅 {sp500.daily_change_pct:.2f}%")

    vix_value = vix_status.get("value")
    if vix_value is not None and vix_value >= 30:
        reasons.append(f"VIX {vix_value:.2f}")

    if vxn and vxn.current_close >= 35:
        reasons.append(f"VXN {vxn.current_close:.2f}")

    return bool(reasons), reasons


def generate_execution_plan(
    new_triggered_levels: list[dict[str, Any]],
    extreme_move: bool,
) -> dict[str, Any]:
    count = len(new_triggered_levels)
    if count <= 1:
        execute_count = count
        message = "本次仅触发 1 个新档位，建议按策略执行该档位。"
    elif count == 2 and not extreme_move:
        execute_count = 2
        message = (
            "本次触发 2 个新档位，常规情况下可以执行全部新触发档位。"
            "如当日市场波动异常，也可以先执行较浅一档，另一档下一个交易日确认。"
        )
    else:
        execute_count = 2
        if extreme_move:
            message = (
                "本次出现极端波动且新触发多个档位，建议当天先执行较浅的前 1～2 档。"
                "剩余档位进入待确认状态。这是执行节奏控制，不是取消加仓。"
            )
        else:
            message = (
                "本次一次性叠穿多个档位。为避免极端暴跌日一次性投入过多资金，"
                "建议当天优先执行较浅的前 2 档，其余档位进入待确认状态。"
            )

    return {
        "execute_levels": new_triggered_levels[:execute_count],
        "pending_levels": new_triggered_levels[execute_count:],
        "message": message,
    }


def determine_vix_status(snapshot: IndexSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {"label": "数据缺失", "value": None}

    value = snapshot.current_close
    if value < 15:
        label = "市场平静"
    elif value < 25:
        label = "正常波动"
    elif value < 30:
        label = "市场紧张"
    elif value < 35:
        label = "恐慌加速"
    else:
        label = "恐慌区"

    return {"label": label, "value": value, "date": snapshot.close_date}


def build_vix_auxiliary_note(
    vix_status: dict[str, Any],
    snapshots: dict[str, IndexSnapshot],
    trigger_level: dict[str, Any] | None,
) -> str:
    value = vix_status.get("value")
    label = vix_status.get("label", "数据缺失")
    lines = [f"VIX 辅助判断：{label}"]

    if value is None:
        lines.append("- VIX 数据缺失，本次不使用 VIX 辅助判断。")
        lines.append("- VIX 不单独触发加仓，主触发仍以 S&P 500 / Nasdaq 收盘价回撤为准。")
        return "\n".join(lines)

    lines.append(f"- 当前 VIX 收盘值：{value:.2f}")
    lines.append("- VIX 不单独触发加仓，主触发仍以 S&P 500 / Nasdaq 收盘价回撤为准。")

    primary_drawdowns = [
        snapshots[key].drawdown_pct
        for key in ("sp500", "nasdaq")
        if key in snapshots
    ]
    max_drawdown = max(primary_drawdowns) if primary_drawdowns else 0.0

    if value > 25:
        lines.append("- 市场波动升高，关注是否接近加仓区。")
    if value > 30 and 7 <= max_drawdown < 10:
        lines.append("- VIX 已高于 30，且主要指数接近 -10% 第一档，可提前准备第一档的一半资金，但必须人工确认。")
    if value > 30 and trigger_level is not None and float(trigger_level["drawdown_pct"]) >= 15:
        lines.append("- 这是恐慌性下跌环境，应优先检查执行计划，不要因为新闻恐慌取消。")
    if value > 35:
        lines.append("- VIX 处于恐慌区；若回撤档位已经触发，应尊重脚本，按计划执行，但仍需检查数据和资金。")

    return "\n".join(lines)


def build_execution_checklist(level_pct: int) -> str:
    return f"""执行前确认清单（当前档位：-{level_pct}%）：
1. 本次是否为收盘价触发，而非盘中瞬间跌破？
2. 当前档位是否尚未执行过？
3. NISA额度、买付余力、SBIハイブリッド余额是否足够？
4. 是否不会影响生活费、打新资金和未来3个月现金流？
5. 本次买入金额是否严格符合计划，而不是临时加码？
6. 是否因为新闻恐慌想取消执行？如果是，请回看原始策略。
7. 如果确认无误，原则上在下一个可交易日执行。
8. 极端档位可以分两笔执行，但不能因为想等更低点而取消本档。"""


def build_execution_discipline_note(level_pct: int) -> str:
    return f"""执行纪律：触发即执行，不等待下一档
- 当前 -{level_pct}% 档位一旦由收盘价触发，就执行当前档位。
- 不因为担心未来跌到下一档而跳过本档。
- 宁可在 -25% 买入后短期被套，也不要为了等 -30% 错过大底。
- 脚本负责触发，人负责确认；但人不能因为恐惧或贪婪随意否决规则。"""


def build_cash_bucket_note() -> str:
    return """现金子弹三层管理：
- 第一层：约200万日元，高流动资金，用于 -10%、-15% 前两档。
- 第二层：约300万日元，日元低风险资金，用于 -20%、-25% 和部分 -30%。
- 第三层：约200万日元，深跌资金，用于 -30%、-35%、-40%。
- 前两层尽量保持日元和高流动性，不要为了收益承受过多汇率风险。
- 现金可以有收益，但不能影响下跌时的执行速度。"""


def build_concentration_risk_note() -> str:
    return """集中度风险提醒：
- SOX 占总投资资产建议控制在 20%～25% 以内。
- NASDAQ100 占总投资资产建议控制在 30% 以内。
- NASDAQ100 + SOX 合计建议控制在 45% 以内。
- eMAXIS Slim 全世界株式（オール・カントリー）承担全球分散层，不是进攻仓。
- 如果当前 NASDAQ100 / SOX 已接近上限，本次加仓应优先确认是否需要降低 SOX 比例，或转向 S&P500 / オルカン。
- 由于用户职业本身与半导体周期相关，SOX 是进攻仓，不应成为命运仓。"""


def build_annual_anchor_note() -> str:
    return """年度时间锚点：
- 如果全年没有触发 -10% 档，年底可根据市场温度考虑投入 0～50万日元到宽基。
- 市场中性/偏冷：可投入 30～50万。
- 市场偏热：可投入 0～30万。
- 市场过热：不强制投入。
- 年度锚点只用于降低现金长期拖累，不用于追高，不额外买 SOX。
- 年度锚点资金优先投向 S&P500 或 eMAXIS Slim 全世界株式（オール・カントリー）。"""


def get_next_trigger_info(
    snapshots: dict[str, IndexSnapshot],
    primary_keys: list[str],
    levels: list[dict[str, Any]],
    triggered_levels: list[Any],
) -> dict[str, Any]:
    primary_snapshots = {
        key: snapshots[key]
        for key in primary_keys
        if key in snapshots
    }
    if not primary_snapshots:
        return {"available": False, "reason": "主要指数数据不可用"}

    max_drawdown = max(snapshot.drawdown_pct for snapshot in primary_snapshots.values())
    triggered_set = normalize_triggered_levels(triggered_levels)
    next_level = next(
        (
            level
            for level in levels
            if level_value(level) not in triggered_set
            and level_value(level) > max_drawdown
        ),
        None,
    )

    if next_level is None:
        return {
            "available": False,
            "reason": "所有配置档位均已触发，或当前回撤已超过最后档位",
            "current_max_drawdown": max_drawdown,
        }

    level_pct = level_value(next_level)
    target_prices = {
        key: snapshot.high_close * (1 - level_pct / 100)
        for key, snapshot in primary_snapshots.items()
    }
    distance_pct_points = max(0.0, level_pct - max_drawdown)

    return {
        "available": True,
        "level_pct": level_pct,
        "label": next_level.get("label", format_level(level_pct)),
        "target_prices": target_prices,
        "distance_pct_points": distance_pct_points,
        "current_max_drawdown": max_drawdown,
    }


def format_snapshot_line(snapshot: IndexSnapshot | None, label: str) -> str:
    if snapshot is None:
        return f"{label}：数据获取失败，本次无法显示。"

    return (
        f"{snapshot.name}（{snapshot.ticker}）：\n"
        f"  日期：{snapshot.close_date}\n"
        f"  当前收盘价：{snapshot.current_close:,.2f}\n"
        f"  最高收盘价：{snapshot.high_close:,.2f}\n"
        f"  回撤：{snapshot.drawdown_pct:.2f}%"
    )


def format_vix_line(snapshot: IndexSnapshot | None, vix_status: dict[str, Any]) -> str:
    if snapshot is None:
        return f"VIX：数据获取失败，状态：{vix_status['label']}"

    return (
        f"VIX（{snapshot.ticker}）：\n"
        f"  日期：{snapshot.close_date}\n"
        f"  当前收盘值：{snapshot.current_close:.2f}\n"
        f"  状态：{vix_status['label']}"
    )


def format_aux_index_line(snapshot: IndexSnapshot | None, label: str) -> str:
    if snapshot is None:
        return f"{label}：数据获取失败，辅助判断中忽略。"
    daily_text = (
        f"，单日变化：{snapshot.daily_change_pct:.2f}%"
        if snapshot.daily_change_pct is not None
        else ""
    )
    return f"{label}（{snapshot.ticker}）：当前收盘值 {snapshot.current_close:.2f}{daily_text}"


def build_pending_note(
    confirmed_pending_levels: list[dict[str, Any]],
    new_pending_levels: list[dict[str, Any]],
    expired_pending_levels: list[float],
    snapshots: dict[str, IndexSnapshot],
    primary_keys: list[str],
) -> str:
    lines = []

    if confirmed_pending_levels:
        lines.append("待确认档位已确认执行：")
        for level in confirmed_pending_levels:
            level_pct = level_value(level)
            lines.append(f"- {format_level(level_pct)}：当前仍满足，建议确认执行。")

    if new_pending_levels:
        lines.append("新增待确认档位：")
        for level in new_pending_levels:
            level_pct = level_value(level)
            trigger_index = get_triggering_indices(snapshots, primary_keys, level_pct)
            lines.append(f"- {format_level(level_pct)}：触发指数 {trigger_index}，下一个交易日确认。")

    if expired_pending_levels:
        lines.append("未确认执行档位：")
        for level_pct in sorted(expired_pending_levels):
            lines.append(
                f"- {format_level(level_pct)}：该档位曾被极端日内/单日波动叠穿，但后续未确认，暂不执行。"
            )

    if not lines:
        lines.append("- 当前没有待确认档位。")

    lines.append("- 若仍满足：确认执行。")
    lines.append("- 若不满足：暂不执行，等待下一次触发。")
    return "\n".join(lines)


def build_email(
    config: dict[str, Any],
    snapshots: dict[str, IndexSnapshot],
    execute_levels: list[dict[str, Any]],
    pending_levels: list[dict[str, Any]],
    confirmed_pending_levels: list[dict[str, Any]],
    expired_pending_levels: list[float],
    newly_reached_levels: list[dict[str, Any]],
    all_levels: list[dict[str, Any]],
    triggered_levels_after_send: list[float],
    pending_levels_after_send: list[float],
    vix_status: dict[str, Any],
    execution_plan: dict[str, Any],
    extreme_move: bool,
    extreme_reasons: list[str],
) -> tuple[str, str]:
    execute_labels = [level.get("label", format_level(level_value(level))) for level in execute_levels]
    pending_labels = [level.get("label", format_level(level_value(level))) for level in pending_levels]
    newly_reached_labels = [level.get("label", format_level(level_value(level))) for level in newly_reached_levels]
    total_execute_amount = total_amount(execute_levels)
    execute_allocations = aggregate_allocations(execute_levels)
    pending_allocations = aggregate_allocations(pending_levels)
    already_triggered_amount = triggered_amount(all_levels, triggered_levels_after_send)
    strategy_amount = strategy_total_amount(all_levels)
    remaining_amount = max(0.0, strategy_amount - already_triggered_amount)
    next_trigger_info = get_next_trigger_info(
        snapshots=snapshots,
        primary_keys=config.get("primary_trigger_indices", ["sp500", "nasdaq"]),
        levels=all_levels,
        triggered_levels=triggered_levels_after_send,
    )

    subject_levels = ", ".join(execute_labels or pending_labels or newly_reached_labels) or "待确认状态更新"
    subject_action = "新触发" if newly_reached_levels else "待确认档位更新"
    subject = f"【市场回撤加仓提醒】【需人工确认】{subject_action} {subject_levels}｜建议执行 {format_man_yen(total_execute_amount)}"

    triggered_level_lines = []
    for level in newly_reached_levels:
        level_pct = level_value(level)
        trigger_index = get_triggering_indices(
            snapshots=snapshots,
            primary_keys=config.get("primary_trigger_indices", ["sp500", "nasdaq"]),
            level_pct=level_pct,
        )
        triggered_level_lines.append(
            f"- 新触发：{level.get('label', format_level(level_pct))}\n"
            f"  触发指数：{trigger_index}\n"
            f"  档位总金额：{format_man_yen(float(level['total_amount_man_yen']))}"
        )

    execute_allocation_lines = ["基金 | 买入金额", "--- | ---:"]
    for fund, amount in execute_allocations.items():
        execute_allocation_lines.append(f"{fund} | {format_man_yen(amount)}")
    execute_allocation_lines.append(f"合计 | {format_man_yen(total_execute_amount)}")

    pending_allocation_lines = ["基金 | 待确认金额", "--- | ---:"]
    for fund, amount in pending_allocations.items():
        pending_allocation_lines.append(f"{fund} | {format_man_yen(amount)}")
    pending_allocation_lines.append(f"合计 | {format_man_yen(total_amount(pending_levels))}")

    today = datetime.now().strftime("%Y-%m-%d")
    triggered_text = ", ".join(format_level(level) for level in sorted(triggered_levels_after_send))
    pending_text = ", ".join(format_level(level) for level in sorted(pending_levels_after_send)) or "无"
    next_level_text = next_trigger_info.get("label", "暂无") if next_trigger_info else "暂无"
    next_distance_text = (
        f"{next_trigger_info['distance_pct_points']:.2f} 个百分点"
        if next_trigger_info and next_trigger_info.get("available")
        else "暂无"
    )

    body = f"""【市场回撤加仓提醒】

日期：{today}

1. 当前回撤状态

{format_snapshot_line(snapshots.get("sp500"), "S&P500")}

{format_snapshot_line(snapshots.get("nasdaq"), "Nasdaq")}

{format_snapshot_line(snapshots.get("sox"), "SOX")}

{format_vix_line(snapshots.get("vix"), vix_status)}

{format_aux_index_line(snapshots.get("vxn"), "VXN")}

2. 本次新触发档位

{chr(10).join(triggered_level_lines)}

3. 本次建议买入

本次正式建议执行档位：
{chr(10).join(f"- {label}" for label in execute_labels) if execute_labels else "- 无"}

本次合计建议买入：
{chr(10).join(execute_allocation_lines)}

待确认档位对应金额：
{chr(10).join(f"- {label}" for label in pending_labels) if pending_labels else "- 无"}
{chr(10).join(pending_allocation_lines) if pending_levels else ""}

4. 累计执行情况

- 已触发档位：{triggered_text}
- 已触发总金额：{format_man_yen(already_triggered_amount)}
- 剩余现金子弹：{format_man_yen(remaining_amount)}
- 当前待确认档位：{pending_text}
- 下一档位：{next_level_text}
- 距离下一档还差：{next_distance_text}

【多档位叠穿与执行节奏】

{execution_plan['message']}

极端波动判断：{"是" if extreme_move else "否"}
{chr(10).join(f"- {reason}" for reason in extreme_reasons) if extreme_reasons else "- 未触发极端波动条件"}

【待确认档位】

{build_pending_note(confirmed_pending_levels, pending_levels, expired_pending_levels, snapshots, config.get("primary_trigger_indices", ["sp500", "nasdaq"]))}

5. 策略解释

本策略采用 2.5% 等距拆细档位。浅跌阶段以 S&P500 和オルカン为主，深跌阶段逐步提高 NASDAQ100 比例。SOX 只作为小额进攻仓，不作为主加仓对象。TOPIX 不进入本套美股主加仓表。

{build_vix_auxiliary_note(vix_status, snapshots, max(execute_levels + pending_levels + newly_reached_levels, key=level_value) if (execute_levels or pending_levels or newly_reached_levels) else None)}

{build_concentration_risk_note()}

{build_annual_anchor_note()}

6. 纪律提醒

本提醒不是自动交易指令，需要人工确认。生活费、打新资金、紧急备用金不参与本策略。VIX、VXN、10年美债、CPI、SOX过热指标仅用于辅助判断，不单独触发买卖。

多档位叠穿时，脚本会完整识别所有已达到的档位。执行上允许在极端波动日分批确认，但这不是取消策略。第一批已确认档位应按纪律执行，待确认档位需在下一个交易日根据收盘回撤状态确认。

本策略仍然不自动交易，需要人工确认。生活费、打新资金、紧急备用金不参与本策略。

脚本不会连接券商 API，不会自动下单。
"""

    return subject, body


def send_email(config: dict[str, Any], subject: str, body: str) -> None:
    smtp_config = config["email"]
    password_env = smtp_config.get("password_env", "SMTP_PASSWORD")
    password = os.environ.get(password_env)

    if not password:
        raise RuntimeError(f"环境变量 {password_env} 未设置，无法发送邮件")

    message = EmailMessage()
    message["From"] = smtp_config["from_addr"]
    message["To"] = ", ".join(smtp_config["to_addrs"])
    message["Subject"] = subject
    message.set_content(body, subtype="plain", charset="utf-8")

    host = smtp_config["smtp_host"]
    port = int(smtp_config.get("smtp_port", 587))
    username = smtp_config["username"]
    use_tls = bool(smtp_config.get("use_tls", True))

    if use_tls:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP_SSL(host, port, timeout=30) as server:
            server.login(username, password)
            server.send_message(message)


def send_test_email(config: dict[str, Any]) -> None:
    subject = "【投资提醒】邮件测试"
    body = """这是一封测试邮件。

如果你收到这封邮件，说明 SMTP 配置和 SMTP_PASSWORD 环境变量可以正常使用。

提醒：这不是自动交易，不是确定买卖指令。脚本不会连接券商 API，不会自动下单。
"""
    send_email(config, subject, body)


def print_report(
    config: dict[str, Any],
    snapshots: dict[str, IndexSnapshot],
    trigger_levels: list[dict[str, Any]],
    vix_status: dict[str, Any],
    next_trigger_info: dict[str, Any] | None = None,
) -> None:
    print("\n========== 当前市场状态报告 ==========")
    print(format_snapshot_line(snapshots.get("sp500"), "S&P500"))
    print()
    print(format_snapshot_line(snapshots.get("nasdaq"), "Nasdaq"))
    print()
    print(format_snapshot_line(snapshots.get("sox"), "SOX"))
    print()
    print(format_vix_line(snapshots.get("vix"), vix_status))
    print()

    if not trigger_levels:
        print("当前是否触发新档位：否")
    else:
        labels = ", ".join(level.get("label", format_level(level_value(level))) for level in trigger_levels)
        print(f"当前是否触发新档位：是，{labels}")

    if next_trigger_info and next_trigger_info.get("available"):
        print("\n下一档触发提示：")
        print(f"- 下一档：{next_trigger_info['label']}")
        for key, target_price in next_trigger_info["target_prices"].items():
            name = config["indices"][key]["name"]
            print(f"- {name} 跌到约 {target_price:,.2f} 会触发")
        print(f"- 距离下一档还差约 {next_trigger_info['distance_pct_points']:.2f} 个百分点")
    elif next_trigger_info:
        print(f"\n下一档触发提示：{next_trigger_info.get('reason', '暂无')}")

    print("=====================================\n")


def resolve_config_path(config_path: Path, configured_path: str | Path) -> Path:
    """配置中的相对路径按 config.yaml 所在目录解析。"""
    path = Path(configured_path)
    if path.is_absolute():
        return path
    return (config_path.parent / path).resolve()


def run_monitor(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    config = load_config(config_path)

    log_path = resolve_config_path(config_path, config.get("log_file", DEFAULT_LOG_PATH))
    setup_logging(log_path)

    state_path = resolve_config_path(config_path, config.get("state_file", DEFAULT_STATE_PATH))
    state = load_state(state_path)

    if args.test_email:
        try:
            send_test_email(config)
            logging.info("测试邮件发送成功")
            return 0
        except Exception as exc:  # noqa: BLE001
            logging.exception("测试邮件发送失败：%s", exc)
            return 2

    levels = sorted(config["trigger_levels"], key=lambda item: float(item["drawdown_pct"]))
    primary_keys = config.get("primary_trigger_indices", ["sp500", "nasdaq"])
    reset_on_new_high = bool(config.get("reset_on_new_high", False))

    snapshots = fetch_all_snapshots(config)
    vix_status = determine_vix_status(snapshots.get("vix"))

    if args.report:
        trigger_levels_for_report = choose_trigger_levels(
            snapshots=snapshots,
            primary_keys=primary_keys,
            levels=levels,
            triggered_levels=state["triggered_levels"],
        )
        next_trigger_info = get_next_trigger_info(
            snapshots=snapshots,
            primary_keys=primary_keys,
            levels=levels,
            triggered_levels=state["triggered_levels"],
        )
        print_report(config, snapshots, trigger_levels_for_report, vix_status, next_trigger_info)
        return 0

    update_state_for_new_high(state, snapshots, primary_keys, reset_on_new_high)
    pending_confirmed, pending_expired = split_pending_confirm_levels(
        snapshots=snapshots,
        primary_keys=primary_keys,
        pending_levels=state["pending_confirm_levels"],
    )
    confirmed_pending_levels = filter_levels_by_values(levels, pending_confirmed)

    if args.force_level is not None:
        forced_level = round(float(args.force_level), 1)
        newly_reached_levels = [
            level
            for level in levels
            if level_value(level) == forced_level
        ]
        trigger_level = next(
            (
                level
                for level in newly_reached_levels
            ),
            None,
        )
        if trigger_level is None:
            logging.error("找不到 --force-level 指定的档位：%s", args.force_level)
            return 2
        logging.info("使用 --force-level 强制测试提醒：%.1f%%", forced_level)
    else:
        already_handled_levels = sorted(
            normalize_triggered_levels(state["triggered_levels"])
            | normalize_triggered_levels(state["pending_confirm_levels"])
        )
        newly_reached_levels = choose_trigger_levels(
            snapshots=snapshots,
            primary_keys=primary_keys,
            levels=levels,
            triggered_levels=already_handled_levels,
        )

    extreme_move, extreme_reasons = is_extreme_move(snapshots, vix_status)
    execution_plan = generate_execution_plan(newly_reached_levels, extreme_move)
    execute_levels = confirmed_pending_levels + execution_plan["execute_levels"]
    pending_levels = execution_plan["pending_levels"]

    if not execute_levels and not pending_levels and not pending_expired:
        next_trigger_info = get_next_trigger_info(
            snapshots=snapshots,
            primary_keys=primary_keys,
            levels=levels,
            triggered_levels=state["triggered_levels"],
        )
        if next_trigger_info.get("available"):
            logging.info(
                "下一档 %s，距离约 %.2f 个百分点",
                next_trigger_info["label"],
                next_trigger_info["distance_pct_points"],
            )
        else:
            logging.info("下一档提示：%s", next_trigger_info.get("reason", "暂无"))
        if not args.dry_run:
            save_state(state_path, state)
        return 0

    execute_level_values = {level_value(level) for level in execute_levels}
    pending_level_values = {level_value(level) for level in pending_levels}
    triggered_levels_after_send = sorted(
        normalize_triggered_levels(state["triggered_levels"]) | execute_level_values
    )
    pending_levels_after_send = sorted(
        (
            normalize_triggered_levels(state["pending_confirm_levels"])
            - pending_confirmed
            - pending_expired
        )
        | pending_level_values
    )
    subject, body = build_email(
        config=config,
        snapshots=snapshots,
        execute_levels=execute_levels,
        pending_levels=pending_levels,
        confirmed_pending_levels=confirmed_pending_levels,
        expired_pending_levels=sorted(pending_expired),
        newly_reached_levels=newly_reached_levels,
        all_levels=levels,
        triggered_levels_after_send=triggered_levels_after_send,
        pending_levels_after_send=pending_levels_after_send,
        vix_status=vix_status,
        execution_plan=execution_plan,
        extreme_move=extreme_move,
        extreme_reasons=extreme_reasons,
    )

    logging.info("准备发送提醒邮件：%s", subject)

    if args.dry_run:
        print("\n========== 邮件标题 ==========")
        print(subject)
        print("\n========== 邮件正文 ==========")
        print(body)
        print("========== dry-run：未发送邮件，未更新触发档位 ==========\n")
        return 0

    try:
        send_email(config, subject, body)
    except Exception as exc:  # noqa: BLE001
        logging.exception("邮件发送失败：%s", exc)
        save_state(state_path, state)
        return 2

    state["triggered_levels"] = triggered_levels_after_send
    state["pending_confirm_levels"] = pending_levels_after_send
    state["expired_pending_levels"] = sorted(
        normalize_triggered_levels(state.get("expired_pending_levels", [])) | pending_expired
    )
    save_state(state_path, state)
    logging.info("提醒邮件发送成功，状态已更新：%s", triggered_levels_after_send)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="策略 2.0 执行提醒脚本")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="config.yaml 路径",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="只发送一封测试邮件，不获取行情、不更新触发状态",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要发送的提醒邮件，不真正发送、不更新触发档位",
    )
    parser.add_argument(
        "--force-level",
        type=float,
        help="强制按指定档位生成提醒，用于人工测试，例如 10、12.5、25。配合 --dry-run 可避免发信。",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="打印当前所有指数状态，不发送邮件、不更新触发状态",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run_monitor(parse_args()))
