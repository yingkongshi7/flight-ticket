# Market Drawdown Monitor

这是一个美股指数回撤加仓提醒脚本，用于监控 S&P500 / Nasdaq / SOX / VIX。

它只发送邮件提醒，不连接券商 API，不自动下单。它不是日本股票监控脚本。

## 策略逻辑

- S&P500 / Nasdaq 是主触发器。
- SOX 只作为半导体辅助判断，不单独触发加仓。
- VIX 只作为恐慌辅助判断，不单独触发邮件或大额加仓。
- 同一档位只提醒一次。
- 本策略从原 7 档升级为 13 档，档位间隔为 2.5%。
- 加仓资金总额仍为 700万日元。
- 中间版分配原则：浅跌宽基，深跌增强 NASDAQ100，SOX 小仓，TOPIX 不参与主加仓。
- 当市场一次性跌穿多个 2.5% 档位时，脚本会识别所有新触发档位。
- 通常情况下，新触发 1～2 档可以直接执行。
- 如果一次性新触发 3 档以上，或出现极端波动，脚本会建议当天优先执行较浅的前 1～2 档，其余档位进入待确认。
- 邮件发送成功后才更新 `triggered_levels.json`。
- `--dry-run` 不发送邮件、不更新触发档位。
- `--report` 只打印状态，不发送邮件、不更新状态。
- 所有买入都需要人工确认；脚本不会连接券商 API，不会自动下单。

## 文件结构

```text
.
├── .github/workflows/daily-monitor.yml
├── archive/market_drawdown_monitor_v1.py
├── market_drawdown_monitor.py
├── config.yaml
├── requirements.txt
├── README.md
└── .gitignore
```

`market_drawdown_monitor.py` 是当前正式策略 2.0 脚本。`archive/market_drawdown_monitor_v1.py` 是旧版 1.0 归档文件，不再作为正式入口。

## 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 邮箱配置

编辑 `config.yaml`：

```yaml
email:
  smtp_host: "smtp.gmail.com"
  smtp_port: 587
  use_tls: true
  username: "your_email@gmail.com"
  from_addr: "your_email@gmail.com"
  to_addrs:
    - "your_email@gmail.com"
  password_env: "SMTP_PASSWORD"
```

邮箱密码不要写入 `config.yaml`。请使用环境变量：

```bash
export SMTP_PASSWORD="你的邮箱应用专用密码"
```

如果使用 Gmail，请使用 Google 账号生成的 16 位“应用专用密码”，不是普通登录密码。

GitHub Actions 中需要设置：

`Settings -> Secrets and variables -> Actions -> New repository secret`

名称：

```text
SMTP_PASSWORD
```

值：你的 Gmail 应用专用密码。

## 常用命令

语法检查：

```bash
python -m py_compile market_drawdown_monitor.py
```

报告：

```bash
python market_drawdown_monitor.py --config config.yaml --report
```

dry-run 测试：

```bash
python market_drawdown_monitor.py --config config.yaml --dry-run --force-level 10
```

深跌档测试：

```bash
python market_drawdown_monitor.py --config config.yaml --dry-run --force-level 25
```

测试邮件：

```bash
python market_drawdown_monitor.py --config config.yaml --test-email
```

正式运行：

```bash
python market_drawdown_monitor.py --config config.yaml
```

## 运行环境选择

建议只选择一种正式运行环境：

- 本地 cron
- 或 GitHub Actions

不要长期同时在本地和 GitHub Actions 中正式运行，否则 `triggered_levels.json` 状态可能不一致，导致重复提醒或漏提醒。

GitHub Actions 的 workflow 会使用 `git add -f triggered_levels.json` 强制提交状态文件，用于保留已触发档位，避免下一次运行重复提醒。

## GitHub Actions

`.github/workflows/daily-monitor.yml` 会在日本时间每天早上 8 点运行。

GitHub Actions 使用 UTC，所以 cron 是：

```yaml
- cron: "0 23 * * *"
```

也可以在 GitHub 的 `Actions` 页面手动运行，模式包括：

- `normal`：正常判断是否触发提醒
- `test-email`：只测试邮件
- `dry-run-10`：生成 -10% 档邮件内容但不发送
- `dry-run-25`：生成 -25% 档邮件内容但不发送
- `report`：打印当前市场状态

## cron 每天运行

如果在 macOS/Linux 上运行：

```bash
crontab -e
```

加入：

```cron
TZ=Asia/Tokyo
0 8 * * * cd /path/to/MarketDrawdownMonitor && SMTP_PASSWORD="你的邮箱应用专用密码" /path/to/MarketDrawdownMonitor/.venv/bin/python market_drawdown_monitor.py --config config.yaml >> cron.log 2>&1
```

## VIX 说明

VIX 不会单独触发邮件。

本脚本只有在 S&P500 或 Nasdaq 达到配置的回撤档位时才发送行动提醒。VIX 只会在邮件中作为辅助判断，用于提示市场是否进入恐慌状态。

辅助规则：

- VIX 高于 25：提示市场波动升高
- VIX 高于 30 且接近 -10%：提示可提前准备第一档的一半资金，但必须人工确认
- VIX 高于 30 且已经触发 -15% 或更深档：提示不要因为恐慌新闻取消计划
- VIX 高于 35：恐慌区，若回撤档位已触发，应尊重脚本并检查资金和数据

主触发器始终是 S&P500 / Nasdaq 的收盘价回撤。

VXN、10年美债、CPI、SOX 过热指标等宏观或估值信息也只能作为辅助解释，不单独触发买卖。

## 多档位叠穿与待确认机制

当市场一次性跌穿多个 2.5% 档位时，脚本会完整识别所有新触发档位。

执行节奏规则：

- 新触发 1 档：建议直接执行该档。
- 新触发 2 档：常规情况下可以执行全部新触发档位。
- 新触发 3 档以上：建议当天优先执行较浅的前 2 档，其余进入待确认。
- 如果出现极端波动，且本次新触发 2 档以上：建议当天先执行较浅的前 1～2 档，其余进入待确认。

极端波动的简单判断：

- Nasdaq 单日跌幅 <= -5%
- S&P500 单日跌幅 <= -4%
- VIX >= 30
- VXN >= 35

待确认档位会在下一次正式运行时根据收盘回撤是否仍满足条件来决定是否执行：

- 如果仍满足该档位或更深：移入 `triggered_levels`，邮件中列为“待确认档位已确认执行”。
- 如果不再满足：从 `pending_confirm_levels` 移除，并在邮件中说明“未确认执行”。

这个机制是为了避免极端暴跌日一次性投入过多资金，不改变总加仓计划，也不取消已经触发的策略纪律。

## 触发即执行，不等待下一档

策略 2.0 的纪律是：

- 当前档位一旦由收盘价触发，就执行当前档位
- 不因为担心未来跌到下一档而跳过本档
- 宁可在 -25% 买入后短期被套，也不要为了等 -30% 错过大底
- 脚本负责触发，人负责确认；但人不能因为恐惧或贪婪随意否决规则

## 当前 NISA 每月定投组合

| 基金 | 每月金额 | 作用 |
|---|---:|---|
| SBI・V・S&P500 | 40,000円 | 美股核心宽基 |
| eMAXIS Slim 全世界株式（オール・カントリー） | 25,000円 | 全球分散层 |
| ニッセイNASDAQ100 | 20,000円 | AI / 科技成长卫星 |
| ニッセイSOX | 10,000円 | 半导体进攻仓 |
| ニッセイTOPIX | 5,000円 | 日本 / 日元资产补充 |

- S&P500 是核心。
- オルカン承担全球分散，不再使用ニッセイ外国株式作为加仓配置。
- NASDAQ100 和 SOX 是进攻仓，需要控制比例。
- TOPIX 作为日元资产和日本市场补充。
- 现金子弹加仓时，仍以 S&P500、オルカン、NASDAQ100、SOX 为主。
- TOPIX 不作为美股回撤加仓表的主要对象。

## 当前触发档位

配置在 `config.yaml`：

| 回撤 | 金额 | 分配 |
| --- | ---: | --- |
| -10.0% | 30万 | S&P500 16万、オルカン 10万、NASDAQ100 4万、SOX 0万、TOPIX 0万 |
| -12.5% | 30万 | S&P500 16万、オルカン 10万、NASDAQ100 4万、SOX 0万、TOPIX 0万 |
| -15.0% | 35万 | S&P500 18万、オルカン 11万、NASDAQ100 6万、SOX 0万、TOPIX 0万 |
| -17.5% | 35万 | S&P500 17万、オルカン 11万、NASDAQ100 7万、SOX 0万、TOPIX 0万 |
| -20.0% | 45万 | S&P500 19万、オルカン 13万、NASDAQ100 12万、SOX 1万、TOPIX 0万 |
| -22.5% | 45万 | S&P500 19万、オルカン 12万、NASDAQ100 12万、SOX 2万、TOPIX 0万 |
| -25.0% | 55万 | S&P500 21万、オルカン 15万、NASDAQ100 17万、SOX 2万、TOPIX 0万 |
| -27.5% | 55万 | S&P500 21万、オルカン 15万、NASDAQ100 16万、SOX 3万、TOPIX 0万 |
| -30.0% | 70万 | S&P500 24万、オルカン 18万、NASDAQ100 23万、SOX 5万、TOPIX 0万 |
| -32.5% | 70万 | S&P500 24万、オルカン 17万、NASDAQ100 24万、SOX 5万、TOPIX 0万 |
| -35.0% | 80万 | S&P500 28万、オルカン 20万、NASDAQ100 27万、SOX 5万、TOPIX 0万 |
| -37.5% | 80万 | S&P500 27万、オルカン 20万、NASDAQ100 28万、SOX 5万、TOPIX 0万 |
| -40.0% | 70万 | S&P500 25万、オルカン 15万、NASDAQ100 25万、SOX 5万、TOPIX 0万 |

合计：S&P500 275万、オルカン 187万、NASDAQ100 205万、SOX 33万、TOPIX 0万，总计 700万。

## 状态文件

`triggered_levels.json` 记录已经提醒过的档位。同一档位只提醒一次。

`pending_confirm_levels` 记录已经被市场叠穿、但因多档位叠穿或极端波动暂缓到下一次正式运行确认的档位。

如果你想重新开始一轮提醒，可以手动把它改回：

```json
{
  "last_highs": {},
  "pending_confirm_levels": [],
  "expired_pending_levels": [],
  "triggered_levels": []
}
```

## 日本股票脚本说明

本仓库只用于美股指数回撤加仓提醒。

日本股票观察脚本是另一个独立项目，不应放在本仓库中混用。

## 免责声明

这不是自动交易，不是确定买卖指令。脚本不会连接券商 API，不会自动下单。所有操作都需要人工确认后执行。
