# Patch notes 2026-05-30 v4

## Summary display clarification

- The run summary now prints the no-email reason on the same summary block with an explicit `Reason:` prefix.
- In `--link-only` runs, the summary states that price APIs are intentionally not called and `Priced results: 0` is expected.
- No pricing/filtering logic changed from v3.

## Expected link-only result

For:

```bash
python flight_price_monitor.py --config flight_price_config.yaml --core-only --link-only --dry-run --force
```

`Priced results: 0` and `Alert emails prepared: 0` are expected.
