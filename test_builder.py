import datetime
import unittest

import builder


class OilProvenanceTest(unittest.TestCase):
    def test_eia_daily_fallback_is_visible_in_snapshot(self):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        point = (today - datetime.timedelta(days=4)).isoformat()
        previous_tip_source = dict(builder.TIP_SOURCE)
        try:
            builder.TIP_SOURCE.clear()
            builder.TIP_SOURCE.update({"brent": "eia", "wti": "eia"})
            series = {
                "brent": {"date": point, "stale": False},
                "wti": {"date": point, "stale": False},
            }
            notes = []
            builder.annotate_oil_provenance(series, notes)

            self.assertEqual(series["brent"]["quality_status"], "official-delayed")
            self.assertIn("source EIA quotidienne officielle différée", series["wti"]["source_warning"])
            self.assertFalse(series["brent"]["stale"])
            self.assertEqual(len(notes), 2)
        finally:
            builder.TIP_SOURCE.clear()
            builder.TIP_SOURCE.update(previous_tip_source)


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


if __name__ == "__main__":
    unittest.main()
