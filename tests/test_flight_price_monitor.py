import copy
import datetime as dt
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("monitor", ROOT / "flight_price_monitor.py")
monitor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = monitor
assert spec.loader is not None
spec.loader.exec_module(monitor)


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = str(payload)

    def json(self):
        return self._payload


class FlightMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = monitor.load_config(ROOT / "flight_price_config.yaml")

    def setUp(self):
        monitor.TRAVELPAYOUTS_REQUEST_COUNT = 0
        monitor.TRAVELPAYOUTS_LAST_REQUEST_AT = 0.0

    def test_friend_domestic_candidates_use_tyo_roundtrip_and_doubled_threshold(self):
        candidates = monitor.generate_friend_domestic_candidate_searches(copy.deepcopy(self.config))
        self.assertEqual(len(candidates), 12 * 4)
        self.assertTrue(all(c.origin == "TYO" for c in candidates))
        self.assertTrue(all(c.trip_type == "roundtrip" and c.return_date for c in candidates))
        sapporo = next(c for c in candidates if c.route_name == "Tokyo-Sapporo")
        self.assertEqual(sapporo.threshold_jpy, 14400)
        self.assertTrue(monitor.is_friend_domestic_candidate(sapporo))

    def test_flexible_query_is_anchored_to_candidate_month(self):
        depart = dt.date.today() + dt.timedelta(days=120)
        ret = depart + dt.timedelta(days=5)
        c = monitor.SearchCandidate(
            route_name="Tokyo-Seoul",
            destination_category="Northeast Asia",
            origin="TYO",
            destination="ICN",
            depart_date=depart.isoformat(),
            return_date=ret.isoformat(),
            trip_type="roundtrip",
            threshold_jpy=20000,
        )
        params = monitor.travelpayouts_flexible_params(c, self.config)
        self.assertEqual(params["beginning_of_period"], depart.replace(day=1).isoformat())
        self.assertEqual(params["one_way"], "false")

    def test_roundtrip_stop_filter_checks_return_direction(self):
        item = {"transfers": 0, "return_transfers": 2}
        allowed, stops, status = monitor.stops_allowed_for_item(item, self.config)
        self.assertFalse(allowed)
        self.assertEqual(stops, 2)
        self.assertEqual(status, "confirmed")

    def test_friend_domestic_exact_no_price_falls_back_to_monthly_roundtrip(self):
        cfg = copy.deepcopy(self.config)
        cfg["sources"]["travelpayouts"]["min_request_interval_seconds"] = 0
        cfg["sources"]["travelpayouts"]["pause_every_requests"] = 0
        candidate = monitor.generate_friend_domestic_candidate_searches(cfg)[0]
        source_result = monitor.SourceResult(
            candidate=candidate,
            source_name="travelpayouts",
            query_link=monitor.build_travelpayouts_search_link(candidate, cfg),
        )

        exact_empty = FakeResponse({"success": True, "data": []})
        flexible_hit = FakeResponse({
            "success": True,
            "data": [
                {
                    "origin": "TYO",
                    "destination": candidate.destination,
                    "depart_date": candidate.depart_date,
                    "return_date": candidate.return_date,
                    "value": 13000,
                    "number_of_changes": 0,
                }
            ],
        })

        with patch.dict(os.environ, {"TRAVELPAYOUTS_TOKEN": "test-token"}, clear=False):
            with patch.object(monitor.requests, "get", side_effect=[exact_empty, flexible_hit]) as get_mock:
                result = monitor.fetch_travelpayouts_price(source_result, cfg)

        self.assertEqual(get_mock.call_count, 2)
        self.assertEqual(result.status, "success_friend_domestic_flexible")
        self.assertEqual(result.price_jpy, 13000)
        self.assertEqual(result.price_mode, "flexible_cached")

    def test_friend_domestic_fallback_rejects_irrelevant_long_stay(self):
        cfg = copy.deepcopy(self.config)
        cfg["sources"]["travelpayouts"]["min_request_interval_seconds"] = 0
        cfg["sources"]["travelpayouts"]["pause_every_requests"] = 0
        candidate = monitor.generate_friend_domestic_candidate_searches(cfg)[0]
        source_result = monitor.SourceResult(
            candidate=candidate,
            source_name="travelpayouts",
            query_link=monitor.build_travelpayouts_search_link(candidate, cfg),
        )
        exact_empty = FakeResponse({"success": True, "data": []})
        long_return = (dt.date.fromisoformat(candidate.depart_date) + dt.timedelta(days=30)).isoformat()
        flexible_long = FakeResponse({
            "success": True,
            "data": [{
                "depart_date": candidate.depart_date,
                "return_date": long_return,
                "value": 5000,
                "number_of_changes": 0,
            }],
        })
        with patch.dict(os.environ, {"TRAVELPAYOUTS_TOKEN": "test-token"}, clear=False):
            with patch.object(monitor.requests, "get", side_effect=[exact_empty, flexible_long]):
                result = monitor.fetch_travelpayouts_price(source_result, cfg)
        self.assertIsNone(result.price_jpy)
        self.assertEqual(result.status, "no_price")

    def test_domestic_recipients_are_split_between_self_and_friends(self):
        self_recipients = monitor.recipients_for_scope(self.config, "domestic")
        friends = monitor.friend_recipients(self.config)
        self.assertIn("hailuwang71@gmail.com", self_recipients)
        self.assertNotIn("tyuu7517@gmail.com", self_recipients)
        self.assertIn("tyuu7517@gmail.com", friends)
        self.assertIn("andyping1989@yahoo.co.jp", friends)

    def test_multi_airport_same_route_is_grouped_and_cheapest_wins(self):
        depart = (dt.date.today() + dt.timedelta(days=60)).isoformat()
        ret = (dt.date.today() + dt.timedelta(days=65)).isoformat()
        alerts = []
        for destination, price in (("PVG", 52000), ("SHA", 47000)):
            c = monitor.SearchCandidate(
                route_name="Tokyo-Xian-via-Shanghai",
                destination_category="Core China Fallback",
                origin="NRT",
                destination=destination,
                depart_date=depart,
                return_date=ret,
                trip_type="roundtrip",
                threshold_jpy=70000,
            )
            r = monitor.SourceResult(c, "travelpayouts", "https://example.invalid", price_jpy=price, price_mode="exact_date")
            alerts.append({"result": r, "alert_needed": True, "below_threshold": True, "watch_price": False, "obvious_drop": False, "abnormal": False, "focus": False})

        selected = monitor.select_best_alerts_by_group(alerts)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["result"].price_jpy, 47000)
        self.assertEqual(selected[0]["result"].candidate.destination, "SHA")
        self.assertEqual(
            monitor.cross_scope_route_date_key(alerts[0]),
            monitor.cross_scope_route_date_key(alerts[1]),
        )

    def test_travelpayouts_actual_airport_is_recorded(self):
        c = monitor.SearchCandidate(
            route_name="Tokyo-Xian-via-Shanghai",
            destination_category="Core China Fallback",
            origin="NRT",
            destination="SHA",
            depart_date=(dt.date.today() + dt.timedelta(days=60)).isoformat(),
            return_date=(dt.date.today() + dt.timedelta(days=65)).isoformat(),
            trip_type="roundtrip",
            threshold_jpy=70000,
        )
        r = monitor.SourceResult(c, "travelpayouts", "https://example.invalid")
        monitor.apply_travelpayouts_actual_airports(r, {"origin_airport": "NRT", "destination_airport": "PVG"})
        self.assertEqual(r.actual_origin_airport, "NRT")
        self.assertEqual(r.actual_destination_airport, "PVG")

    def test_watch_band_is_eight_percent_and_does_not_send_email(self):
        cfg = copy.deepcopy(self.config)
        depart = (dt.date.today() + dt.timedelta(days=60)).isoformat()
        c = monitor.SearchCandidate(
            route_name="Tokyo-Test",
            destination_category="Global",
            origin="TYO",
            destination="AAA",
            depart_date=depart,
            return_date=None,
            trip_type="oneway",
            threshold_jpy=70000,
        )
        self.assertEqual(monitor.watch_threshold_for_candidate(c, cfg), 75600)
        r = monitor.SourceResult(c, "travelpayouts", "https://example.invalid", price_jpy=75000, price_mode="exact_date")
        alert = monitor.evaluate_price_alert(r, {"prices": {}, "history_lows": {}}, cfg)
        self.assertTrue(alert["watch_price"])
        self.assertFalse(alert["below_threshold"])
        self.assertFalse(alert["alert_needed"])

    def test_large_drop_above_target_is_report_only(self):
        cfg = copy.deepcopy(self.config)
        depart = (dt.date.today() + dt.timedelta(days=60)).isoformat()
        c = monitor.SearchCandidate(
            route_name="Tokyo-Test-Drop",
            destination_category="Global",
            origin="TYO",
            destination="BBB",
            depart_date=depart,
            return_date=None,
            trip_type="oneway",
            threshold_jpy=70000,
        )
        r = monitor.SourceResult(c, "travelpayouts", "https://example.invalid", price_jpy=75000, price_mode="exact_date")
        state = {"prices": {r.key: {"price_jpy": 90000}}, "history_lows": {}}
        alert = monitor.evaluate_price_alert(r, state, cfg)
        self.assertGreaterEqual(alert["drop_pct"], 15)
        self.assertTrue(alert["obvious_drop"])
        self.assertTrue(alert["watch_price"])
        self.assertFalse(alert["alert_needed"])

    def test_at_target_price_still_sends_immediate_alert(self):
        cfg = copy.deepcopy(self.config)
        depart = (dt.date.today() + dt.timedelta(days=60)).isoformat()
        c = monitor.SearchCandidate(
            route_name="Tokyo-Test-Target",
            destination_category="Global",
            origin="TYO",
            destination="CCC",
            depart_date=depart,
            return_date=None,
            trip_type="oneway",
            threshold_jpy=70000,
        )
        r = monitor.SourceResult(c, "travelpayouts", "https://example.invalid", price_jpy=70000, price_mode="exact_date")
        alert = monitor.evaluate_price_alert(r, {"prices": {}, "history_lows": {}}, cfg)
        self.assertTrue(alert["below_threshold"])
        self.assertFalse(alert["watch_price"])
        self.assertTrue(alert["alert_needed"])

    def test_weekly_watch_status_ignores_legacy_state_without_target(self):
        legacy = {"price_jpy": 80000, "watch_threshold": 84000, "watch_price": True}
        self.assertFalse(monitor.weekly_watch_status(legacy))
        current = {"price_jpy": 75000, "threshold_jpy": 70000, "watch_threshold": 75600, "watch_price": True}
        self.assertTrue(monitor.weekly_watch_status(current))
        too_high = {"price_jpy": 80000, "threshold_jpy": 70000, "watch_threshold": 75600, "watch_price": False}
        self.assertFalse(monitor.weekly_watch_status(too_high))

    def test_core_fallback_watch_ceiling_is_75600(self):
        cfg = copy.deepcopy(self.config)
        candidate = next(c for c in monitor.generate_candidate_searches(cfg, core_only=True) if monitor.is_core_fallback_candidate(c))
        self.assertEqual(candidate.threshold_jpy, 70000)
        self.assertEqual(monitor.watch_threshold_for_candidate(candidate, cfg), 75600)

    def test_v3_dedup_reads_pre_v3_destination_specific_state_keys(self):
        depart = (dt.date.today() + dt.timedelta(days=60)).isoformat()
        ret = (dt.date.today() + dt.timedelta(days=65)).isoformat()
        c = monitor.SearchCandidate(
            route_name="Tokyo-Xian-via-Shanghai",
            destination_category="Core China Fallback",
            origin="NRT",
            destination="PVG",
            depart_date=depart,
            return_date=ret,
            trip_type="roundtrip",
            threshold_jpy=70000,
        )
        r = monitor.SourceResult(c, "travelpayouts", "https://example.invalid", price_jpy=47000, price_mode="exact_date")
        alert = {
            "result": r, "alert_needed": True, "below_threshold": True, "watch_price": False,
            "obvious_drop": False, "abnormal": False, "focus": False,
        }
        legacy_key = monitor.legacy_destination_cross_scope_key(c)
        state = {"alerts": {legacy_key: {"date": dt.date.today().isoformat(), "price_jpy": 47000}}}
        self.assertFalse(monitor.deduplicate_alert(alert, state, self.config))


if __name__ == "__main__":
    unittest.main()
