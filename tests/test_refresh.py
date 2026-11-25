import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fetch_cafe_deals as core
import refresh_existing as refresh


def offer(pid="1", vid="11", price=8300):
    return dict(productId=pid, vendorItemId=vid, productName="프레지덩 휘핑크림, 1L, 1개",
                productPrice=price, isRocket=True, category="크림·연유", unitPrice=830,
                unitBasis="100ml당", packNote="1,000ml", query="서강 라미스 휘핑크림",
                priceHistory=[["2026-11-19 07:03 KST", 8300]])


class RefreshTests(unittest.TestCase):
    def test_exact_offer_updates_price_units_history_even_if_wrong_option_first(self):
        deal = offer()
        hist = {"products": {}}
        status = refresh.apply_result(deal, [offer(vid="22"), offer(price=9000)], "2026-11-25 12:00 KST", hist)
        self.assertEqual(status, "ok")
        self.assertEqual((deal["productPrice"], deal["unitPrice"]), (9000, 900))
        self.assertEqual(deal["lastCheckedAt"], "2026-11-25 12:00 KST")
        self.assertEqual(hist["products"]["1"]["observations"][0]["price"], 9000)

    def test_missing_wrong_option_nonrocket_do_not_advance_price(self):
        for items, expected in [([], "not_found"), ([offer(vid="22")], "option_mismatch"),
                                ([dict(offer(), isRocket=False)], "non_rocket"),
                                ([offer(price=0)], "invalid_price")]:
            with self.subTest(expected=expected):
                deal = offer()
                old = copy.deepcopy(deal)
                hist = {"products": {}}
                self.assertEqual(refresh.apply_result(deal, items, "2026-11-25 12:00 KST", hist), expected)
                self.assertEqual(deal, old)
                self.assertEqual(hist["products"], {})

    def test_scheduler_rotates_after_miss_and_retries_later(self):
        now = datetime(2026, 11, 25, 3, tzinfo=timezone.utc)
        a, b = offer(), offer("2", "22")
        state = {"products": {"1": {"lastAttemptAt": core.kst_str(now), "consecutiveMisses": 1}}}
        self.assertEqual([d["productId"] for d in refresh.plan([a, b], state, 8, now)], ["2"])
        self.assertEqual(len(refresh.plan([a, b], state, 8, now + timedelta(days=1))), 2)
        self.assertIn("프레지덩", refresh.search_query(a, 0))
        self.assertEqual(refresh.search_query(a, 1), a["query"])

    def test_file_timestamp_cannot_create_observation(self):
        hist = {"products": {}}
        deal = offer()
        del deal["priceHistory"]
        self.assertIsNone(refresh.last_checked(deal))
        self.assertEqual(core.reconcile_from_previous(hist, {"updatedAt": "2026-11-25 12:00 KST", "deals": [deal]}), 0)

    def test_missing_option_identity_cannot_be_accepted(self):
        self.assertFalse(refresh.exact_offer(offer(), {"productId": "1", "productPrice": 1000}))

    def test_freshness_boundary_and_failed_check(self):
        now = datetime(2026, 11, 25, 3, tzinfo=timezone.utc)
        a = dict(offer(), lastCheckedAt=core.kst_str(now - timedelta(hours=24)))
        b = dict(offer("2"), lastCheckedAt=core.kst_str(now - timedelta(hours=1)))
        c = dict(b, refreshStatus="not_found")
        self.assertEqual(refresh.freshness_stats([a, b, c], now)["within24Hours"], 1)

    def run_collector(self, response=None, error=None, usage=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "data").mkdir()
        previous = {"status": "ok", "updatedAt": "2026-11-21 20:04 KST", "deals": [offer()], "count": 1}
        (root / "deals.json").write_text(json.dumps(previous), encoding="utf-8")
        patches = [patch.object(core, "ROOT", root), patch.object(core, "OUT_PATH", root / "deals.json"),
                   patch.object(core, "HISTORY_PATH", root / "data/history.json"),
                   patch.object(core, "USAGE_PATH", root / "data/api_usage.json"),
                   patch.object(refresh, "STATE_PATH", root / "data/refresh_state.json"),
                   patch.dict("os.environ", {"COUPANG_ACCESS_KEY": "test", "COUPANG_SECRET_KEY": "test"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        if usage:
            core.save_usage(usage)
        def api(*args):
            self.assertGreaterEqual(len(core.load_usage()), 1)  # reservation precedes HTTP
            if error:
                raise error
            return response
        with patch.object(core, "api_get", side_effect=api) as mock:
            code = refresh.main(["--max-requests", "1"])
        return code, json.loads((root / "deals.json").read_text(encoding="utf-8")), refresh.load_state(), mock

    def test_empty_response_is_persisted_without_faking_price_update(self):
        code, data, state, mock = self.run_collector({"rCode": "0", "data": {"productData": []}})
        self.assertEqual(code, 0)
        self.assertEqual(data["deals"][0]["refreshStatus"], "not_found")
        self.assertEqual(data["deals"][0]["lastCheckedAt"], "2026-11-19 07:03 KST")
        self.assertEqual(data["updatedAt"], "2026-11-21 20:04 KST")
        self.assertEqual(state["products"]["1"]["consecutiveMisses"], 1)

    def test_rate_limit_failure_counts_request_and_persists_cooldown(self):
        error = urllib.error.HTTPError("https://example.invalid", 429, "limited", {}, None)
        code, data, state, mock = self.run_collector(error=error)
        self.assertEqual(code, 1)
        self.assertEqual(data["deals"][0]["refreshStatus"], "api_error")
        self.assertIn("blockedUntil", state)
        self.assertEqual(len(core.load_usage()), 1)

    def test_success_updates_existing_only(self):
        code, data, state, mock = self.run_collector({"rCode": "0", "data": {"productData": [offer(price=9900), offer("999", "999")]}})
        self.assertEqual(code, 0)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["deals"][0]["productPrice"], 9900)
        self.assertEqual(data["deals"][0]["refreshStatus"], "ok")
        self.assertEqual(data["freshnessStats"]["within24Hours"], 1)

    def test_persisted_hourly_budget_blocks_new_run(self):
        code, data, state, mock = self.run_collector(usage=[datetime.now(timezone.utc) - timedelta(seconds=i) for i in range(9)])
        self.assertEqual(code, 0)
        mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
