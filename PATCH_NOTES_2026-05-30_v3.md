# Patch notes 2026-05-30 v3

## Summary clarity fix

The previous v2 code behaved correctly, but the GitHub summary could still look confusing in dry-run/link-only mode:

- `Priced results: 0`
- `travelpayouts:manual_check_required`

This is expected when the command includes `--link-only`, because the script intentionally generates manual-check links and does not call price APIs.

## Changes

- Added `Run type` to the summary:
  - `link-only/manual links only`
  - `price-api enabled`
- Added a specific no-alert explanation when `--link-only` is used.
- Left alert/weekly/pricing logic unchanged from v2.

## Correct tests

Manual-link test, no pricing expected:

```bash
python flight_price_monitor.py --config flight_price_config.yaml --core-only --link-only --dry-run --force
```

Price API test, pricing may appear if token/API/date data works:

```bash
export TRAVELPAYOUTS_TOKEN="your_token"
python flight_price_monitor.py --config flight_price_config.yaml --core-only --dry-run --force
```

Do not add `--link-only` when testing actual pricing.
