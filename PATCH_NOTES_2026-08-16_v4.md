# 2026-08-16 v4 - Target-only immediate email policy

## Policy change

- Immediate alert emails are now sent only when `price_jpy <= threshold_jpy`.
- A price exactly equal to the target is considered a target hit and sends.
- Watch-band prices no longer send immediate email.
- Large-drop / focus flags above target remain recorded for history and reports, but do not send immediate email.

## Watch band

- Global watch margin changed from the old wide band to `8%`.
- Watch band is now `target < price <= target * 1.08`.
- Watch tracking remains enabled for state, GitHub Actions Summary and weekly reports.
- Core China fallback target `70,000 JPY` now uses a `75,600 JPY` watch ceiling instead of `84,000 JPY`.

## Weekly report

- Added a dedicated `接近目标价（仅观察，不发即时邮件）` section.
- Existing large-drop history remains available in the weekly report.
- Legacy pre-v4 watch flags are ignored until refreshed by a new monitor run, preventing old 20/25% watch-band entries from appearing as current watch items.

## Configuration

```yaml
settings:
  watch_price_tracking_enabled: true
  email_below_target_only: true
  watch_price_margin_pct: 8
  core_fallback_alert_jpy: 70000
  core_fallback_watch_jpy: 75600
```

Route-level China fallback `watch_threshold_jpy` values are also set to `75600`.

## Compatibility

The evaluator still understands the legacy `watch_price_alert_enabled` setting as a fallback for older configs, but the shipped configs use `watch_price_tracking_enabled`.

## Validation

- Python compile: PASS
- Unit tests: 14/14 PASS
- YAML parsing: PASS
- Link-only dry-runs for all/core/global/domestic: PASS
- Weekly report dry-run: PASS
