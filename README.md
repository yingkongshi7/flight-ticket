# Flight Price Monitor

安全优先的 Python 3 机票价格监控脚本，适合放在 GitHub Actions 定时运行。

本项目不是自动购票工具：不会自动下单、不会保存支付信息、不会登录网站、不会绕过验证码，也不会爬取 Google Flights / Skyscanner / Trip.com / 携程 / 飞猪等动态网页。

## 数据源策略

- Travelpayouts / Aviasales Data API：主要真实价格源，返回缓存价格。
- Amadeus：可选真实价格源，默认关闭。
- Google Flights / Skyscanner / Trip.com / 携程 / 飞猪 / 航司官网：只生成人工确认链接，不自动抓价。

Travelpayouts 返回的是缓存数据，不保证实时、不保证最终含税价、不保证包含托运行李。最终价格、转机次数、行李、税费、退改签和签证要求都必须人工确认。

## 转机限制

默认配置：

```yaml
settings:
  max_stops: 1
  direct_only: false
  allow_unknown_stops: true
```

含义：

- `max_stops: 0`：只接受直飞。
- `max_stops: 1`：最多一次转机。
- `max_stops: 2`：最多两次转机。
- `direct_only: true`：等价于 `max_stops: 0`。
- `allow_unknown_stops: true`：如果 API 没返回可判断的转机次数，保留报价，但邮件中标注“转机次数需人工确认”。
- `allow_unknown_stops: false`：无法判断转机次数的报价会被丢弃。

Travelpayouts 的 `direct` 参数只能区分“只直飞”和“不限制直飞”，不能 100% 原生保证“最多一次转机”。脚本会尽量从 `transfers`、`number_of_changes`、`stops`、`segments`、`route` 等字段解析转机次数；能解析就过滤，不能解析就按 `allow_unknown_stops` 决定保留或丢弃。

Amadeus 如果启用，会向 Flight Offers Search 传入 `maxStops`，并从 `itineraries / segments` 判断转机次数。它的转机判断通常比 Travelpayouts 缓存 API 更可靠。

Google Flights / Trip.com / 携程 / 飞猪 / Skyscanner 等人工确认链接仍需手动确认转机次数、行李、税费和最终价格。直飞模式下，Google Flights 查询词会追加 `nonstop`，但仍需人工确认。

## 当前监控策略

当前 Travelpayouts 配置：

```yaml
sources:
  travelpayouts:
    max_requests_per_run: 300
    pause_every_requests: 80
    pause_seconds: 5
    retry_attempts: 3
    retry_base_sleep_seconds: 2
```

大致候选数量：

- `core-only`: 48
- `domestic-only`: 144
- `global-only`: 352
- `all`: 544

建议日常分开运行，不建议把 `all` 作为每天自动任务。

价格模式：

- `domestic`：使用 Travelpayouts `prices_for_dates`，精确日期缓存报价。
- `core`：先用 `prices_for_dates` 查精确日期；没有报价时 fallback 到 `get_latest_prices` flexible cached low-price。
- `global`：使用 `get_latest_prices` flexible cached latest price，适合发现低价机会，不代表精确日期实时价格。
- `manual`：人工确认链接。

## 提醒规则

强提醒：

- 低于路线目标价。
- 明显降价。
- 异常低价。
- 东京-西安节假日重点低价。

观察提醒：

```yaml
settings:
  watch_price_alert_enabled: true
  watch_price_margin_pct: 25
```

如果价格没有低于目标价，但在 `threshold_jpy * 1.25` 内，会发送 `【机票观察】`。观察提醒参与去重，避免每天重复提醒。

邮件标题和正文以中文为主。邮件正文会显示：

- 价格模式。
- 原始候选日期和 flexible cached 实际低价日期。
- 配置的最大转机次数。
- API 返回转机次数。
- 转机判断状态：已确认 / 需人工确认 / 无法确认。

如果 `Priced results > 0` 但 `Alert emails prepared = 0`，通常说明价格未达到目标价、观察价阈值或降价规则，或者被重复提醒控制抑制。

如果出现 `rate_limited`，建议降低 `max_requests_per_run`、增大 `pause_seconds`，或继续分批运行。

## GitHub Secrets

在仓库 `Settings -> Secrets and variables -> Actions -> Repository secrets` 中设置：

- `SMTP_PASSWORD`: Gmail App Password，用于发送邮件。
- `TRAVELPAYOUTS_TOKEN`: Travelpayouts / Aviasales Data API token。

可选：

- `AMADEUS_CLIENT_ID`
- `AMADEUS_CLIENT_SECRET`

## GitHub Actions

`.github/workflows/flight-price-monitor.yml` 使用 UTC cron：

- `23:00 UTC` = 日本时间次日 `08:00`，每天运行核心路线。
- `23:30 UTC` = 日本时间次日 `08:30`，每天运行全球路线。
- `00:00 UTC Saturday` = 日本时间周六 `09:00`，发送周报。
- `00:30 UTC Saturday` = 日本时间周六 `09:30`，每周运行日本国内路线。

手动测试同一天重复运行：

- `core-force`
- `global-force`
- `domestic-force`

测试邮件并临时绕过 7 天重复提醒：

- `core-force-alerts`
- `global-force-alerts`
- `domestic-force-alerts`

## 本地运行

```bash
pip install -r requirements.txt
python flight_price_monitor.py --config flight_price_config.yaml --dry-run --core-only --link-only
python flight_price_monitor.py --config flight_price_config.yaml --weekly-report --dry-run
```

Linux / macOS:

```bash
export SMTP_PASSWORD="your_gmail_app_password"
export TRAVELPAYOUTS_TOKEN="your_token"
```

Windows PowerShell:

```powershell
$env:SMTP_PASSWORD="your_gmail_app_password"
$env:TRAVELPAYOUTS_TOKEN="your_token"
```

## Cron 示例

如果服务器时区是日本时间：

```cron
0 8 * * * cd /path/to/repo && /usr/bin/python3 flight_price_monitor.py --config flight_price_config.yaml --core-only
30 8 * * * cd /path/to/repo && /usr/bin/python3 flight_price_monitor.py --config flight_price_config.yaml --global-only
0 9 * * 6 cd /path/to/repo && /usr/bin/python3 flight_price_monitor.py --config flight_price_config.yaml --weekly-report
30 9 * * 6 cd /path/to/repo && /usr/bin/python3 flight_price_monitor.py --config flight_price_config.yaml --domestic-only
```

## 限制

“最多一次转机”在 Travelpayouts 上不一定能 100% 保证，因为缓存 API 可能不返回完整航段。最稳的做法是：能解析就过滤，不能解析就邮件标注“需人工确认”。
