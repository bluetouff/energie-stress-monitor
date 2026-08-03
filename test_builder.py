import datetime
import json
import os
import tempfile
import unittest
import urllib.parse
from unittest import mock

import builder


class EiaOilFetchTest(unittest.TestCase):
    def setUp(self):
        self.cache_dir = tempfile.TemporaryDirectory()
        self.previous_key = builder.EIA_KEY
        self.previous_cache_dir = builder.EIA_CACHE_DIR
        self.previous_refresh = builder.EIA_MIN_REFRESH_SECONDS
        self.previous_meta = dict(builder.EIA_FETCH_META)
        builder.EIA_KEY = "test-eia-secret"
        builder.EIA_CACHE_DIR = self.cache_dir.name
        builder.EIA_MIN_REFRESH_SECONDS = 14400
        builder.EIA_FETCH_META.clear()

    def tearDown(self):
        builder.EIA_KEY = self.previous_key
        builder.EIA_CACHE_DIR = self.previous_cache_dir
        builder.EIA_MIN_REFRESH_SECONDS = self.previous_refresh
        builder.EIA_FETCH_META.clear()
        builder.EIA_FETCH_META.update(self.previous_meta)
        self.cache_dir.cleanup()

    @staticmethod
    def payload(series="RBRTE", frequency="daily"):
        return {
            "response": {
                "frequency": frequency,
                "data": [
                    {
                        "period": "2026-07-27",
                        "series": series,
                        "units": "$/BBL",
                        "value": "91.82",
                    },
                    {
                        "period": "2026-07-24",
                        "series": series,
                        "units": "$/BBL",
                        "value": "100.31",
                    },
                    {
                        "period": "2026-07-28",
                        "series": "AUTRE",
                        "units": "$/BBL",
                        "value": "999",
                    },
                    {
                        "period": "2026-07-28",
                        "series": series,
                        "units": "EUR/BBL",
                        "value": "999",
                    },
                ],
            },
        }

    def test_native_route_validates_series_frequency_and_units(self):
        with mock.patch.object(builder, "http_json", return_value=self.payload()) as get:
            pairs = builder.fetch_eia_oil_spot("RBRTE", length=2)

        self.assertEqual(
            pairs,
            [
                (datetime.date(2026, 7, 24), 100.31),
                (datetime.date(2026, 7, 27), 91.82),
            ],
        )
        url = get.call_args.args[0]
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/v2/petroleum/pri/spt/data/")
        self.assertEqual(query["frequency"], ["daily"])
        self.assertEqual(query["facets[series][]"], ["RBRTE"])
        self.assertEqual(query["data[0]"], ["value"])
        self.assertEqual(query["length"], ["2"])

    def test_cache_caps_network_calls_and_contains_no_key(self):
        with mock.patch.object(builder, "http_json", return_value=self.payload()) as get:
            first = builder.fetch_eia_oil_spot("RBRTE")
            self.assertEqual(builder.EIA_FETCH_META["RBRTE"]["refresh_mode"], "network")
            second = builder.fetch_eia_oil_spot("RBRTE")

        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(builder.EIA_FETCH_META["RBRTE"]["refresh_mode"], "cache")
        cache_path = os.path.join(self.cache_dir.name, "RBRTE.json")
        with open(cache_path, "r", encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn(builder.EIA_KEY, raw)
        self.assertNotIn("api_key", raw)
        self.assertEqual(json.loads(raw)["schema"], builder.EIA_CACHE_SCHEMA)

    def test_failed_request_is_not_retried(self):
        with mock.patch.object(
                builder, "http_json", side_effect=RuntimeError("EIA unavailable")) as get:
            with self.assertRaisesRegex(RuntimeError, "EIA unavailable"):
                builder.fetch_eia_oil_spot("RWTC")
        self.assertEqual(get.call_count, 1)

    def test_rejects_non_daily_payload(self):
        with mock.patch.object(
                builder, "http_json", return_value=self.payload(frequency="weekly")):
            with self.assertRaisesRegex(RuntimeError, "fréquence"):
                builder.fetch_eia_oil_spot("RBRTE")


class OilProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.previous_meta = dict(builder.EIA_FETCH_META)
        builder.EIA_FETCH_META.clear()

    def tearDown(self):
        builder.EIA_FETCH_META.clear()
        builder.EIA_FETCH_META.update(self.previous_meta)

    def test_eia_delayed_point_is_visible_in_snapshot(self):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        point = (today - datetime.timedelta(days=4)).isoformat()
        builder.EIA_FETCH_META["RBRTE"] = {
            "checked_at": "2026-08-03T08:00:00Z",
            "refresh_mode": "network",
        }
        series = {
            "brent": {"date": point, "stale": False},
            "wti": {"date": point, "stale": False},
        }
        notes = []
        builder.annotate_oil_provenance(series, notes)

        self.assertEqual(series["brent"]["quality_status"], "official-delayed")
        self.assertIn("source EIA quotidienne officielle différée", series["wti"]["source_warning"])
        self.assertEqual(series["brent"]["tip_source"], "eia")
        self.assertEqual(series["brent"]["source_series"], "RBRTE")
        self.assertEqual(series["brent"]["source_refresh_mode"], "network")
        self.assertFalse(series["brent"]["stale"])
        self.assertEqual(len(notes), 2)

    def test_recent_eia_point_is_nominal(self):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        point = (today - datetime.timedelta(days=2)).isoformat()
        series = {"brent": {"date": point, "stale": False}}
        builder.annotate_oil_provenance(series, [])

        self.assertEqual(series["brent"]["quality_status"], "nominal")
        self.assertIsNone(series["brent"]["source_warning"])

    def test_cached_current_error_is_not_masked(self):
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        warning = "brent indisponible: timeout"
        series = {
            "brent": {
                "date": today,
                "stale": False,
                "quality_status": "cached-current",
                "source_warning": warning,
            },
        }
        builder.annotate_oil_provenance(series, [])

        self.assertEqual(series["brent"]["quality_status"], "cached-current")
        self.assertEqual(series["brent"]["source_warning"], warning)
        self.assertEqual(series["brent"]["tip_source"], "eia")


class CachedFallbackTest(unittest.TestCase):
    def test_current_cached_value_remains_eligible(self):
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        fallback = builder.cached_fallback(
            {"date": today, "score": 74.0, "stale": False},
            "elec_fr indisponible: timeout",
            max_age_days=1,
        )

        self.assertFalse(fallback["stale"])
        self.assertEqual(fallback["quality_status"], "cached-current")
        self.assertEqual(fallback["age_days"], 0)
        composite, subindices = builder.composite({
            "elec_fr": fallback,
            "elec_de": {"score": 80.0, "stale": False},
        })
        self.assertEqual(subindices["electricite"]["score"], 77.0)
        self.assertEqual(composite["score"], 77.0)

    def test_expired_cached_value_is_excluded(self):
        old = (
            datetime.datetime.now(datetime.timezone.utc).date()
            - datetime.timedelta(days=3)
        ).isoformat()
        fallback = builder.cached_fallback(
            {"date": old, "score": 74.0, "stale": False},
            "elec_fr indisponible: timeout",
            max_age_days=1,
        )

        self.assertTrue(fallback["stale"])
        self.assertEqual(fallback["quality_status"], "stale")
        composite, subindices = builder.composite({
            "elec_fr": fallback,
            "elec_de": {"score": 80.0, "stale": False},
        })
        self.assertEqual(subindices["electricite"]["score"], 80.0)
        self.assertEqual(composite["score"], 80.0)


class SourcePolicyTest(unittest.TestCase):
    def test_builder_contains_no_unofficial_oil_provider(self):
        with open(builder.__file__, "r", encoding="utf-8") as handle:
            source = handle.read().lower()
        forbidden = (
            "oilprice" + "api.com",
            "finance." + "yahoo.com",
            "api." + "twelvedata.com",
            "oilprice_key",
            "twelvedata_key",
            "energie_yahoo_oil",
        )
        for marker in forbidden:
            self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
