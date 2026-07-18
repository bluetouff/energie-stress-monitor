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


if __name__ == "__main__":
    unittest.main()
