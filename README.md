# Flight Price Monitor

安全优先的 Python 3 机票价格监控脚本，适合放在 GitHub Actions 定时运行。

脚本不会自动下单、不会保存支付信息、不会登录网站、不会绕过验证码，也不会高频请求。Google Flights、Skyscanner、Trip.com、携程、飞猪继续作为人工确认链接；真实自动价格数据主要来自 Travelpayouts / Aviasales Data API 的缓存价格。Amadeus 保留为可选源，默认关闭。

## 运行方式

```bash
pip install -r requirements.txt
python flight_price_monitor.py --config flight_price_config.yaml --dry-run --core-only --link-only
python flight_price_monitor.py --config flight_price_config.yaml --weekly-report --dry-run
```

## GitHub Secrets

在仓库 `Settings -> Secrets and variables -> Actions -> Repository secrets` 中设置：

- `SMTP_PASSWORD`: Gmail App Password，用于发送邮件。
- `TRAVELPAYOUTS_TOKEN`: Travelpayouts / Aviasales Data API token。

可选：

- `AMADEUS_CLIENT_ID`
- `AMADEUS_CLIENT_SECRET`

## 当前监控策略

当前配置中的 Travelpayouts 请求上限是：

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

建议日常分开运行，不建议把 `all` 作为每天自动任务，因为全量候选较多。

价格模式：

- `domestic`: 使用 Travelpayouts `prices_for_dates`，即精确日期缓存报价。
- `core`: 先用 `prices_for_dates` 查精确日期；没有报价时 fallback 到 `get_latest_prices` flexible cached low-price。
- `global`: 使用 `get_latest_prices` flexible cached latest price，适合发现低价机会，不代表精确日期实时价格。
- `manual`: Google Flights / Trip.com / 携程 / 飞猪 / Skyscanner 等人工确认链接。

Travelpayouts 返回的是缓存数据，不保证实时，不保证最终含税价，也不保证包含托运行李。最终价格、税费、行李、转机、退改签、签证要求都必须人工确认。

## 提醒规则

强提醒：

- 低于路线阈值。
- 明显降价。
- 异常低价。
- 东京-西安节假日重点低价。

观察提醒：

```yaml
settings:
  watch_price_alert_enabled: true
  watch_price_margin_pct: 25
```

如果价格没有低于理想阈值，但在 `threshold_jpy * 1.25` 内，会发送 `【机票观察】`。观察提醒会参与去重，避免每天重复提醒。

如果 `Priced results > 0` 但 `Alert emails prepared = 0`，通常说明价格没有达到 threshold/watch threshold/drop 规则，或被 7 天去重抑制。

如果出现 `rate_limited`，建议降低 `max_requests_per_run`、增大 `pause_seconds`，或继续分批运行。

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

## 本地环境变量

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

## 安全边界

- 不自动下单。
- 不保存支付信息。
- 不登录网站。
- 不绕过 CAPTCHA。
- 不模拟购买。
- 不高频请求 Google Flights / Skyscanner / 携程 / 飞猪等网页。
