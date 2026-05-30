# Patch Notes 2026-05-30 v5

## Added

- Added `--core-manual-report`.
  - Generates a weekly manual-confirmation email for Tokyo-Xian.
  - Does not depend on Travelpayouts cached prices.
  - Includes Tokyo-Xian direct-search links and China transfer fallback links.
- Added `core_fallback_routes` in `flight_price_config.yaml`.
  - NRT -> PEK / PKX
  - NRT -> PVG / SHA
  - NRT -> CAN
  - NRT -> TFU / CTU
- Core mode now includes `core_fallback_routes`, so Travelpayouts also attempts to price denser China gateway routes when Tokyo-Xian has no cached offers.
- Weekly report core group now includes `Core China Fallback` prices if Travelpayouts returns any.
- GitHub Actions now includes:
  - Scheduled weekly core manual report: Saturday 09:15 JST.
  - Manual dispatch option: `core-manual-weekly`.

## Config

New settings:

```yaml
settings:
  core_manual_report_limit: 40
  core_manual_direct_limit: 18
  core_manual_fallback_limit: 22
  core_manual_report_sources:
    - google_flights
    - travelpayouts
    - skyscanner
```

## Test commands

```bash
python -m py_compile flight_price_monitor.py
python flight_price_monitor.py --config flight_price_config.yaml --core-manual-report --dry-run
python flight_price_monitor.py --config flight_price_config.yaml --core-only --link-only --dry-run --force
python flight_price_monitor.py --config flight_price_config.yaml --core-only --dry-run --force
```
