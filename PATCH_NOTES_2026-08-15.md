# 2026-08-15 flight monitor audit / fixes

## Root cause of missing friend domestic alerts

The saved state from 2026-08-15 shows all 144 legacy friend domestic round-trip Travelpayouts exact-date queries returned `no_price`, while the user's domestic one-way queries still produced priced results. The friend recipient configuration itself is valid.

Travelpayouts `prices_for_dates` is cache-based and only reflects prices found recently. Exact round-trip date pairs have a much lower cache hit rate than one-way queries, so relying only on exact pairs causes long periods with no friend-priced results.

## Fixes

1. Friend domestic routes now query only `TYO` by default, reducing 144 friend candidates to 48 while still covering Tokyo airports.
2. Friend domestic exact round-trip `no_price` now falls back to `get_latest_prices` for the candidate departure month.
3. Friend flexible results are restricted to 3-8 day stays so unrelated long/short trips do not generate alerts.
4. Global flexible queries are anchored to each candidate departure month. The old code repeatedly queried the current month for +30/+45/+60/+90 candidates.
5. `max_requests_per_run` increased to 800 and a 0.22s minimum request interval was added. This prevents the 348-candidate global run from being cut off by the old 300-request total budget while keeping request pacing below the stricter flexible endpoint rate.
6. Round-trip stop filtering now considers both `transfers` and `return_transfers`.
7. Multiple recipients are hidden from one another by default.
8. Successful alert sends are persisted immediately; the workflow state commit runs with `always()` so partial successful sends can still be deduplicated after a later failure.
9. The missing `core-manual-weekly` workflow dispatch/schedule described in the README was added (Saturday 09:15 JST).
10. Offline unit tests were added and run in GitHub Actions before the monitor.

## Validation

- `python -m py_compile flight_price_monitor.py`: pass
- `python -m unittest discover -s tests -v`: 6/6 pass
- dry-run core/global/domestic/weekly/core-manual modes: pass
- current candidate counts: core 100; global 348; domestic self 144 + friend 48; all 592

## Important limitation

Travelpayouts is a cached discovery API, not a live booking-price API. A route can still have no cached price even when seats are actually on sale. Alerts therefore remain an opportunity detector; final fare, baggage, stop count, taxes and availability must be checked manually.
