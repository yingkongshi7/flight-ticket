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


if __name__ == "__main__":
    unittest.main()
