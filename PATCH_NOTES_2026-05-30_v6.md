# Patch notes 2026-05-30 v6

## Core departure date filter

- Added `settings.core_min_departure_days`, default `3`.
- Core price API runs now ignore too-soon departures.
- Travelpayouts flexible fallback also starts from the effective core minimum departure date, so same-day/tomorrow cached fares are skipped.

## Core fallback thresholds

Fallback routes are Tokyo -> China gateway cities, not Tokyo -> Xian full itineraries.

- Alert threshold: `settings.core_fallback_alert_jpy: 70000`
- Watch threshold: `settings.core_fallback_watch_jpy: 84000`
- Fallback route `normal_threshold_jpy` values were updated to `70000`.
- Fallback route `watch_threshold_jpy` values were added as `84000`.

Expected behavior:

- `<= 70,000 JPY`: alert / below threshold
- `70,001-84,000 JPY`: watch alert
- `> 84,000 JPY`: usually no alert unless another rule applies

## Fallback wording

Fallback alert subjects and bodies now clearly say:

- This is a Tokyo -> China gateway city price, not a Tokyo -> Xian full itinerary.
- The China domestic segment / high-speed rail / self-transfer risk / baggage must be checked manually.

## Suggested tests

```bash
python -m py_compile flight_price_monitor.py
python flight_price_monitor.py --config flight_price_config.yaml --core-only --link-only --dry-run --force
python flight_price_monitor.py --config flight_price_config.yaml --core-manual-report --dry-run
TRAVELPAYOUTS_TOKEN="your_token" python flight_price_monitor.py --config flight_price_config.yaml --core-only --dry-run --force
```
