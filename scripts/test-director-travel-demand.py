#!/usr/bin/env python3
"""Unit tests for admin_panel.parse_director_travel_demand.

Covers the demand-detection patterns the autoscaler applies to director logs:
- ClassicalInstancing "Processing travel queue" lines (num > 0)
- Dimension "Processing travel queue" lines (num > 0)
- "Received travel request ... (instancingMode=Dimension)" lines
- "Received travel request ... (instancingMode=SingleServer)" lines (added for
  SingleServer-mode maps such as CB_Overland_S_06 / CB_Story_BanditFortress01)
- The "Failed to process travel queue for partition N" warning must NOT feed
  demand (it fires for serverless Dimension/SingleServer partitions regardless
  of queue contents).
"""
import importlib.util
import os
import pathlib
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

try:
    import psycopg2  # noqa: F401
except ModuleNotFoundError:
    psycopg2_stub = types.ModuleType("psycopg2")
    psycopg2_extras_stub = types.ModuleType("psycopg2.extras")
    psycopg2_extras_stub.RealDictCursor = object
    psycopg2_stub.extras = psycopg2_extras_stub
    psycopg2_stub.connect = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("database access is not available in unit tests")
    )
    sys.modules["psycopg2"] = psycopg2_stub
    sys.modules["psycopg2.extras"] = psycopg2_extras_stub


def load_admin_panel(workspace):
    os.environ["ADMIN_WORKSPACE"] = str(workspace)
    spec = importlib.util.spec_from_file_location(
        "admin_panel_under_test", ROOT / "admin" / "admin_panel.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLASSICAL_LINE = (
    "[14:07:21 20 INF Database] Processing travel queue for ClassicalInstancing group "
    "CB_Arrakis_Generic_Sietch_Room (servers: [], num: 1)"
)
DIMENSION_PROCESSING_LINE = (
    "[14:07:21 20 INF Database] Processing travel queue for DeepDesert_1 "
    "(01 Recommended PVE Casual, id=abc123, dimension=0, partition=8, num=2)"
)
DIMENSION_RECEIVED_LINE = (
    "[13:43:54 9 INF Director] Received travel request for 1 player(s) to "
    "DeepDesert_1 (instancingMode=Dimension)"
)
SINGLESERVER_RECEIVED_LINE = (
    "[13:43:54 9 INF Director] Received travel request for 1 player(s) to "
    "CB_Overland_S_06 (instancingMode=SingleServer)"
)
SINGLESERVER_RECEIVED_MULTI_LINE = (
    "[13:43:55 9 INF Director] Received travel request for 2 player(s) to "
    "CB_Story_BanditFortress01 (instancingMode=SingleServer)"
)
FAILURE_LINE = (
    "[14:02:16 14 WRN Database] Failed to process travel queue for partition 8."
)
ZERO_NUM_LINE = (
    "[13:42:58 12 INF Database] Processing travel queue for ClassicalInstancing group "
    "Story_ArtOfKanly (servers: [], num: 0)"
)


class ParseDirectorTravelDemandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        workspace = pathlib.Path(cls.tmp.name)
        (workspace / "config").mkdir(parents=True)
        (workspace / ".env").write_text("", encoding="utf-8")
        cls.panel = load_admin_panel(workspace)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def parse(self, text):
        return self.panel.parse_director_travel_demand(text)

    def maps_counts(self, events):
        return sorted((event["map"], event["count"]) for event in events)

    def test_classical_processing_line_with_demand(self):
        events = self.parse(CLASSICAL_LINE)
        self.assertEqual(self.maps_counts(events), [("CB_Arrakis_Generic_Sietch_Room", 1)])

    def test_dimension_processing_line_with_demand(self):
        events = self.parse(DIMENSION_PROCESSING_LINE)
        self.assertEqual(self.maps_counts(events), [("DeepDesert_1", 2)])

    def test_dimension_received_request(self):
        events = self.parse(DIMENSION_RECEIVED_LINE)
        self.assertEqual(self.maps_counts(events), [("DeepDesert_1", 1)])

    def test_singleserver_received_request(self):
        events = self.parse(SINGLESERVER_RECEIVED_LINE)
        self.assertEqual(self.maps_counts(events), [("CB_Overland_S_06", 1)])

    def test_singleserver_received_request_multi_player(self):
        events = self.parse(SINGLESERVER_RECEIVED_MULTI_LINE)
        self.assertEqual(self.maps_counts(events), [("CB_Story_BanditFortress01", 2)])

    def test_failure_line_does_not_feed_demand(self):
        self.assertEqual(self.parse(FAILURE_LINE), [])

    def test_zero_num_line_does_not_feed_demand(self):
        self.assertEqual(self.parse(ZERO_NUM_LINE), [])

    def test_duplicate_lines_dedup(self):
        events = self.parse(SINGLESERVER_RECEIVED_LINE + "\n" + SINGLESERVER_RECEIVED_LINE)
        self.assertEqual(len(events), 1)

    def test_mixed_log(self):
        log = "\n".join([
            CLASSICAL_LINE,
            ZERO_NUM_LINE,
            FAILURE_LINE,
            DIMENSION_RECEIVED_LINE,
            SINGLESERVER_RECEIVED_LINE,
            "unrelated noise",
        ])
        self.assertEqual(
            self.maps_counts(self.parse(log)),
            [
                ("CB_Arrakis_Generic_Sietch_Room", 1),
                ("CB_Overland_S_06", 1),
                ("DeepDesert_1", 1),
            ],
        )


if __name__ == "__main__":
    unittest.main()
