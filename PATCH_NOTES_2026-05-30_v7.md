# Patch Notes 2026-05-30 v7

## Core fallback threshold normalization

- Fixed a config/logic mismatch where some Core China Fallback holiday windows still used 75,000 JPY or 80,000 JPY as the alert threshold.
- Core China Fallback now consistently uses one alert threshold across all windows:
  - <= 70,000 JPY: alert
  - 70,001-84,000 JPY: watch
  - > 84,000 JPY: no threshold/watch alert unless other drop rules apply
- `threshold_for_route()` now deliberately ignores Golden Week / year-end / Spring Festival threshold overrides for Core China Fallback routes, because those routes are only China gateway prices, not Tokyo-Xian full-itinerary prices.

## Why this matters

The v6 run showed fallback results using 75,000 JPY and 80,000 JPY thresholds even though the intended rule was 70,000 JPY. This patch makes the printed summary, email behavior, and YAML configuration consistent.
