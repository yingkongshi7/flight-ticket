# 2026-08-16 v3 — multi-airport alert deduplication

## Fixed

- Multi-airport routes no longer generate duplicate emails for the same configured route and travel dates.
- The monitor still queries every configured airport (for example PVG/SHA and PEK/PKX), but alert grouping is now by route/date rather than route/destination-airport/date.
- When several airport variants trigger at the same time, the cheapest eligible result wins.
- Persistent deduplication now also groups airport variants of the same route so a second airport does not resend the same city/date alert on a later run.
- Backward compatibility is retained with pre-v3 destination-specific alert keys already stored in `flight_price_state.json`.
- Travelpayouts `origin_airport` / `destination_airport` fields are recorded when the API supplies them, so the email can display the actual returned airport rather than only the query target.

## Routes affected

This applies generically to any route with more than one destination airport, including Shanghai, Beijing, Chengdu, Seoul, Taipei, Bangkok, London and New York.

## Validation

- `python -m py_compile flight_price_monitor.py`: PASS
- `python -m unittest discover -s tests -t . -v`: 9/9 PASS
