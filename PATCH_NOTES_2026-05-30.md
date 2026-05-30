# Patch notes 2026-05-30

## Fixed

1. Weekly report no longer includes fares whose departure date is already in the past.
2. Weekly report deduplicates repeated cached prices for the same route/date/return/price/stops.
3. `prune_state()` now removes expired `latest_prices`, expired `weekly_drops`, and expired `manual_check_links`.
4. Expired configured date-window trips are skipped when generating candidates.
5. Travelpayouts flexible-cache search now starts from today instead of the first day of the month.
6. Travelpayouts flexible-cache results are ignored when their actual cached departure date is already expired.

## Verified locally

```bash
python -m py_compile flight_price_monitor.py
python flight_price_monitor.py --config flight_price_config.yaml --weekly-report --dry-run
python flight_price_monitor.py --config flight_price_config.yaml --link-only --core-only --dry-run --force
```

After the patch, the weekly report output contained no `2026-05-19` / `2026-05-22` / `2026-05-28` expired fares and no repeated Tokyo-Xian 2026-05-28 rows.
