# Flight Price Monitor

安全优先的 Python 3 机票价格监控脚本，适合放在 GitHub Actions 每天运行一次。

默认行为是生成 Google Flights、Skyscanner、Trip.com、携程、飞猪、航空公司官网的查询链接，并把不能稳定抓取价格的平台标记为“需人工确认”。脚本不会自动下单、不会保存支付信息、不会登录、不会绕过验证码，也不会高频请求。

## 本地运行

```bash
pip install -r requirements.txt
python flight_price_monitor.py --config flight_price_config.yaml --dry-run --core-only --link-only
python flight_price_monitor.py --config flight_price_config.yaml --weekly-report --dry-run
```

## Gmail SMTP 环境变量

建议使用 Gmail App Password，不要使用主密码。

Linux / macOS:

```bash
export SMTP_PASSWORD="your_gmail_app_password"
```

Windows PowerShell:

```powershell
$env:SMTP_PASSWORD="your_gmail_app_password"
```

GitHub Actions:

1. 进入仓库 `Settings`。
2. 打开 `Secrets and variables` -> `Actions`。
3. 新增 secret：`SMTP_PASSWORD`。
4. 如需真实价格查询，新增 Travelpayouts secret：`TRAVELPAYOUTS_TOKEN`。
5. 如需持久化 `flight_price_state.json`，建议把 state 提交到私有仓库，或改用 artifact/cache/外部存储。

## Travelpayouts / Aviasales 真实价格源

`flight_price_config.yaml` 默认启用 `travelpayouts` API source，并限制每次最多 80 个请求：

```yaml
sources:
  travelpayouts:
    enabled: true
    mode: "api"
    token_env: TRAVELPAYOUTS_TOKEN
    currency: jpy
    market: jp
    max_requests_per_run: 80
```

需要在 Travelpayouts 账户里获取 Data API token，然后放入 GitHub Actions Secrets：

- `TRAVELPAYOUTS_TOKEN`

本地测试：

```bash
export TRAVELPAYOUTS_TOKEN="your_token"
python flight_price_monitor.py --config flight_price_config.yaml --core-only
```

Windows PowerShell：

```powershell
$env:TRAVELPAYOUTS_TOKEN="your_token"
python flight_price_monitor.py --config flight_price_config.yaml --core-only
```

注意：Travelpayouts / Aviasales Data API 返回的是缓存数据，通常来自最近用户搜索数据，适合每天监控低价趋势，不适合强实时出票。查不到价格时脚本会保留人工确认链接。

## Amadeus 可选源

如果以后恢复使用 Amadeus，可把 `flight_price_config.yaml` 里的 `sources.amadeus.enabled` 改为 `true`，并设置 GitHub Secrets：

- `AMADEUS_CLIENT_ID`
- `AMADEUS_CLIENT_SECRET`

## Cron 示例

服务器时区如果是日本时间：

```cron
# 每天日本时间早上 8 点运行核心路线
0 8 * * * cd /path/to/repo && /usr/bin/python3 flight_price_monitor.py --config flight_price_config.yaml --core-only

# 每周六日本时间早上 9 点发送周报
0 9 * * 6 cd /path/to/repo && /usr/bin/python3 flight_price_monitor.py --config flight_price_config.yaml --weekly-report
```

服务器时区如果是 UTC，日本时间 08:00 = UTC 23:00 前一日，日本时间周六 09:00 = UTC 周六 00:00：

```cron
0 23 * * * cd /path/to/repo && /usr/bin/python3 flight_price_monitor.py --config flight_price_config.yaml --core-only
0 0 * * 6 cd /path/to/repo && /usr/bin/python3 flight_price_monitor.py --config flight_price_config.yaml --weekly-report
```

## GitHub Actions

已提供 `.github/workflows/flight-price-monitor.yml`。它使用 UTC cron：

- `23:00 UTC` = 日本时间次日 `08:00`，每天运行核心路线。
- `23:30 UTC` = 日本时间次日 `08:30`，每天运行全球路线。
- `00:00 UTC Saturday` = 日本时间周六 `09:00`，发送周报。
- `00:30 UTC Saturday` = 日本时间周六 `09:30`，每周运行日本国内路线。

手动运行时可在 Actions 页面选择 `workflow_dispatch`。

手动测试同一天重复运行时，选择：

- `core-force`
- `global-force`
- `domestic-force`
