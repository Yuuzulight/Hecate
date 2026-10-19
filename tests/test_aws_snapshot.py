"""Verify the public snapshot's SVG and JSON, and that a snapshot failure never fails the batch."""

import json
import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from xml.etree import ElementTree

from infra.aws import snapshot

GENERATED = datetime(2026, 10, 20, 8, tzinfo=timezone.utc)
DATA = {
    "repositories": 2358, "snapshot_days": 60, "mentions": 5625, "as_of": "2026-10-20",
    "stars_gained_7d": 1234, "stars_gained_30d": 5678,
    "gainers": [("a<b>&c", 900), ("vite", 300)], "momentum": [("vite", 61.5)],
}


class SnapshotTests(unittest.TestCase):
    def test_svg_is_valid_xml_and_escapes_names(self):
        svg = snapshot.render_svg(DATA, GENERATED)
        ElementTree.fromstring(svg)
        self.assertIn("a&lt;b&gt;&amp;c", svg)
        self.assertIn("+1,234", svg)

    def test_svg_renders_with_no_history(self):
        ElementTree.fromstring(snapshot.render_svg({**DATA, "gainers": [], "momentum": [], "as_of": None}, GENERATED))

    def test_summary_has_counts_and_date_only(self):
        self.assertEqual(snapshot.summary(DATA, GENERATED), {
            "repositories": 2358, "snapshot_days": 60, "mentions": 5625, "stars_gained_7d": 1234,
            "stars_gained_30d": 5678, "as_of": "2026-10-20", "generated": "2026-10-20",
        })

    def test_publish_writes_both_files_with_content_types(self):
        s3 = MagicMock()
        snapshot.publish(s3, "bucket", "snapshot", DATA, GENERATED)
        puts = {call.kwargs["Key"]: call.kwargs for call in s3.put_object.call_args_list}
        self.assertEqual(puts["snapshot/weekly-growth.svg"]["ContentType"], "image/svg+xml")
        self.assertEqual(json.loads(puts["snapshot/summary.json"]["Body"])["repositories"], 2358)

    def test_fetch_shapes_query_results(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (2358, 60, 5625, date(2026, 10, 20), 12, 34)
        cursor.fetchall.side_effect = [[("vite", 9)], [("vite", 61.5)]]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        data = snapshot.fetch(connection)
        self.assertEqual((data["as_of"], data["gainers"], data["momentum"]), ("2026-10-20", [("vite", 9)], [("vite", 61.5)]))
