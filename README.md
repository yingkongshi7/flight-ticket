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
4. 如需真实价格查询，新增 Amadeus secrets：`AMADEUS_CLIENT_ID` 和 `AMADEUS_CLIENT_SECRET`。
5. 如需持久化 `flight_price_state.json`，建议把 state 提交到私有仓库，或改用 artifact/cache/外部存储。

## Amadeus 真实价格源

`flight_price_config.yaml` 默认启用 `amadeus` API source，并限制每次最多 50 个请求：

```yaml
sources:
  amadeus:
    enabled: true
    mode: "api"
    environment: "test"
    client_id_env: AMADEUS_CLIENT_ID
    client_secret_env: AMADEUS_CLIENT_SECRET
    max_requests_per_run: 50
```

需要在 [Amadeus for Developers](https://developers.amadeus.com/) 创建 app，然后把 API Key / API Secret 放入 GitHub Actions Secrets：

- `AMADEUS_CLIENT_ID`
- `AMADEUS_CLIENT_SECRET`

本地测试：

```bash
export AMADEUS_CLIENT_ID="your_api_key"
export AMADEUS_CLIENT_SECRET="your_api_secret"
python flight_price_monitor.py --config flight_price_config.yaml --core-only
```

Windows PowerShell：

```powershell
$env:AMADEUS_CLIENT_ID="your_api_key"
$env:AMADEUS_CLIENT_SECRET="your_api_secret"
python flight_price_monitor.py --config flight_price_config.yaml --core-only
```

注意：Amadeus Self-Service 的 Flight Offers Search 不覆盖所有航空公司和低成本航司，测试环境数据也可能不完整。查不到价格时脚本会保留人工确认链接。

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

- `23:00 UTC` = 日本时间次日 `08:00`，运行核心路线。
- `00:00 UTC Saturday` = 日本时间周六 `09:00`，发送周报。

手动运行时可在 Actions 页面选择 `workflow_dispatch`。
