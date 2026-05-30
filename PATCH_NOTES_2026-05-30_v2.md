# Patch notes 2026-05-30 v2

This patch follows the first expired-date cleanup patch and addresses issues observed in the latest dry-run and weekly report output.

## Changes

1. Weekly report now hides same-day departures by default.
   - New setting: `settings.weekly_min_departure_days: 1`.
   - On 2026-05-30, weekly report displays departures from 2026-05-31 onward.

2. Weekly manual-confirmation links are now deduplicated more aggressively.
   - One representative link per route/date/return-date group is shown.
   - Preferred source order: Google Flights, Skyscanner, Trip.com, Travelpayouts, Ctrip, Fliggy.

3. Weekly manual-confirmation link count is configurable.
   - New setting: `settings.weekly_manual_link_limit: 20`.

4. Manual Travelpayouts links in the manual-confirmation section are labeled as manual links, not cached prices.

## Notes about core dry-run

If a core dry-run shows `Priced results: 0` and every source status is `manual_check_required`, the run is link-only. It generated links but did not call a pricing API.

To test real Travelpayouts pricing, run without `--link-only` and make sure `TRAVELPAYOUTS_TOKEN` is set.
