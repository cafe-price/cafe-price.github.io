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
import refresh_metrics as metrics


def offer(pid="1", vid="11", price=8300):
    return dict(productId=pid, vendorItemId=vid, productName="프레지덩 휘핑크림, 1L, 1개",
                productPrice=price, isRocket=True, category="크림·연유", unitPrice=830,
                unitBasis="100ml당", packNote="1,000ml", query="서강 라미스 휘핑크림",
                priceHistory=[["2026-11-19 07:03 KST", 8300]])


class RefreshTests(unittest.TestCase):
    def test_success_profile_remembers_exact_url_and_survives_metric_retention(self):
        deal = dict(offer(), productUrl="https://link.coupang.com/re/AFFSDP?pageKey=1&itemId=123&vendorItemId=11")
        state = {"products": {}}
        refresh.remember_success(deal, state, "proven query", "2026-11-25 12:00 KST")
        lookup = state["products"]["1"]["lookup"]
        self.assertEqual(lookup["canonicalUrl"], "https://www.coupang.com/vp/products/1?itemId=123&vendorItemId=11")
        self.assertEqual(refresh.saved_queries(deal, state), ["proven query"])
        now = datetime(2026, 12, 30, 3, tzinfo=timezone.utc)
        self.assertEqual(refresh.choose_search([deal], state, now, set(), False)[1], "proven query")
        self.assertEqual(refresh.saved_queries(offer(vid="22"), state), [])

    def test_full_sweep_rechecks_today_but_not_already_examined_run_targets(self):
        now = datetime(2026, 11, 26, 3, tzinfo=timezone.utc)
        deal = dict(offer(), lastCheckedAt=core.kst_str(now))
        state = {"products": {"1": {"lastAttemptAt": core.kst_str(now)}}}
        self.assertIsNone(refresh.choose_search([deal], state, now, set(), False))
        self.assertIsNotNone(refresh.choose_search([deal], state, now, set(), False, True, set()))
        self.assertIsNone(refresh.choose_search([deal], state, now, set(), False, True, {"1"}))

    def test_only_successful_observations_create_profiles_and_run_report(self):
        code, data, state, api = self.run_collector({"rCode":"0", "data":{"productData":[offer("2", "22")] }}, deals=[offer(), offer("2", "22")])
        self.assertNotIn("lookup", state["products"]["1"])
        self.assertEqual(state["products"]["2"]["lookup"]["vendorItemId"], "22")
        report=json.loads((core.ROOT / "data/latest_refresh_run.json").read_text(encoding="utf-8"))
        self.assertTrue(report["finished"])
        self.assertEqual((report["confirmed"], report["unconfirmed"], report["priceChanged"]), (1,1,0))

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
                                ([offer(price=0)], "invalid_price")]:
            with self.subTest(expected=expected):
                deal = offer()
                old = copy.deepcopy(deal)
                hist = {"products": {}}
                self.assertEqual(refresh.apply_result(deal, items, "2026-11-25 12:00 KST", hist), expected)
                self.assertEqual(deal, old)
                self.assertEqual(hist["products"], {})

    def test_exact_nonrocket_offer_updates_price_with_correct_shipping(self):
        deal, hist = offer(), {"products": {}}
        self.assertEqual(refresh.apply_result(deal, [dict(offer(price=9500), isRocket=False)], "2026-11-26 15:00 KST", hist), "ok")
        self.assertEqual(deal["productPrice"], 9500)
        self.assertFalse(deal["isRocket"])
        self.assertEqual(deal["refreshStatus"], "ok")

    def test_retry_queries_use_real_product_not_unrelated_old_keyword(self):
        from retry_queries import variants, choose
        now = datetime(2026,11,26,6,tzinfo=timezone.utc)
        deal = dict(offer(), query="다른 브랜드 전혀 다른 상품")
        self.assertTrue(all("다른 브랜드" not in q for q in variants(deal)))
        cohort = {"1": {"initialStatus":"not_found"}}
        state = {"products": {}}
        q = choose([deal], state, cohort, {}, set(), set(), metrics.day_key(now))[1]
        self.assertIn("프레지덩", q)
        self.assertIsNone(choose([deal], state, cohort, {"1":3}, set(), set(), metrics.day_key(now)))
        self.assertIsNone(choose([deal], state, cohort, {}, {"1"}, set(), metrics.day_key(now)))

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

    def test_long_query_keeps_pack_size_and_count(self):
        deal = dict(offer(), productName="브랜드 " + "스페셜 고소한 신선한 맛있는 원두커피 " * 4 + "1kg, 5개")
        q = refresh.search_query(deal, 0)
        self.assertLessEqual(len(q), 50)
        self.assertTrue(q.startswith("브랜드"))
        self.assertTrue(q.endswith("1kg 5개"))

    def test_missing_option_identity_cannot_be_accepted(self):
        self.assertFalse(refresh.exact_offer(offer(), {"productId": "1", "productPrice": 1000}))

    def test_same_title_without_vendor_id_is_not_an_exact_offer(self):
        item = offer()
        del item["vendorItemId"]
        self.assertFalse(refresh.exact_offer(offer(), item))

    def test_price_is_recorded_even_when_matching_offer_title_omits_quantity(self):
        deal, hist = offer(), {"products": {}}
        item = dict(offer(price=9000), productName="프레지덩 휘핑크림")
        self.assertEqual(refresh.apply_result(deal, [item], "2026-11-25 12:00 KST", hist), "ok")
        self.assertEqual(deal["productPrice"], 9000)
        self.assertIsNone(deal["unitPrice"])
        self.assertEqual(deal["unitStatus"], "unconfirmed")
        self.assertEqual(hist["products"]["1"]["observations"][0]["price"], 9000)
        self.assertEqual(refresh.apply_result(deal, [offer(price=9000)], "2026-11-25 13:00 KST", hist), "ok")
        self.assertEqual((deal["unitPrice"], deal["unitStatus"]), (900, "ok"))

    def test_freshness_boundary_and_failed_check(self):
        now = datetime(2026, 11, 25, 3, tzinfo=timezone.utc)
        a = dict(offer(), lastCheckedAt=core.kst_str(now - timedelta(hours=24)))
        b = dict(offer("2"), lastCheckedAt=core.kst_str(now - timedelta(hours=1)))
        c = dict(b, refreshStatus="not_found")
        self.assertEqual(refresh.freshness_stats([a, b, c], now)["within24Hours"], 1)

    def run_collector(self, response=None, error=None, usage=None, deals=None, limit=1, extra_args=None, cohort=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "data").mkdir()
        if cohort is not None:
            (root / "data/retry_targets.json").write_text(json.dumps({"products":cohort}), encoding="utf-8")
        previous = {"status": "ok", "updatedAt": "2026-11-21 20:04 KST", "deals": deals or [offer()], "count": len(deals or [offer()])}
        (root / "deals.json").write_text(json.dumps(previous), encoding="utf-8")
        patches = [patch.object(core, "ROOT", root), patch.object(core, "OUT_PATH", root / "deals.json"),
                   patch.object(core, "HISTORY_PATH", root / "data/history.json"),
                   patch.object(core, "USAGE_PATH", root / "data/api_usage.json"),
                   patch.object(refresh, "STATE_PATH", root / "data/refresh_state.json"),
                   patch.object(core, "pace_wait", return_value=0),
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
            return response(*args) if callable(response) else response
        with patch.object(core, "api_get", side_effect=api) as mock:
            code = refresh.main(["--max-requests", str(limit)] + (extra_args or []))
        return code, json.loads((root / "deals.json").read_text(encoding="utf-8")), refresh.load_state(), mock

    def test_targeted_retry_uses_new_query_and_counts_cohort_separately(self):
        replies = iter([[offer("2","22")], [offer(price=9100)]])
        code, data, state, api = self.run_collector(
            response=lambda *args: {"rCode":"0","data":{"productData":next(replies)}},
            deals=[offer(),offer("2","22")], limit=8, extra_args=["--retry-unconfirmed"],
            cohort={"1":{"initialStatus":"not_found"}})
        report=json.loads((core.ROOT / "data/latest_refresh_run.json").read_text(encoding="utf-8"))
        self.assertEqual(api.call_count,2)
        self.assertEqual((report["targetCount"],report["targetConfirmed"],report["targetUnconfirmed"],report["confirmed"]),(1,1,0,2))
        queries=[r["query"] for r in state["metrics"]["requests"]]
        self.assertEqual(len(set(queries)),2)

    def test_full_sweep_reports_every_offer_even_with_duplicate_names(self):
        now = core.kst_str(datetime.now(timezone.utc))
        deals = [dict(offer(str(i), str(i+10)), lastCheckedAt=now) for i in range(3)]
        code, data, state, api = self.run_collector({"rCode": "0", "data": {"productData": []}},
                                                   deals=deals, limit=10, extra_args=["--full-sweep"])
        report = json.loads((core.ROOT / "data/latest_refresh_run.json").read_text(encoding="utf-8"))
        self.assertEqual(api.call_count, 3)
        self.assertEqual(report["stopReason"], "queue_complete")
        self.assertEqual({r["status"] for r in report["products"]}, {"not_found"})
        self.assertEqual((report["total"], report["confirmed"], report["unconfirmed"]), (3, 0, 3))

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

    def test_current_search_limit_allows_more_than_legacy_nine_calls(self):
        deals = [dict(offer(str(i), str(100+i)), productName=f"커피 {i} 1kg", query=f"커피 {i}") for i in range(12)]
        with patch.object(refresh, "budget_wait", return_value=0):
            code, data, state, api = self.run_collector(
                {"rCode": "0", "data": {"productData": []}}, deals=deals, limit=12)
        self.assertEqual(api.call_count, 12)
        self.assertEqual(len(core.load_usage()), 12)
        self.assertTrue(all(r["apiStatus"] == "ok" and r["status"] == "not_found"
                            for r in state["metrics"]["requests"]))

    def test_persisted_daily_budget_blocks_new_run(self):
        code, data, state, mock = self.run_collector(usage=[datetime.now(timezone.utc) - timedelta(seconds=i) for i in range(refresh.DAY_CAP)])
        self.assertEqual(code, 0)
        mock.assert_not_called()

    def test_minute_limit_spacing_and_clock_skew(self):
        now = datetime.now(timezone.utc)
        usage = [now - timedelta(seconds=8*i) for i in range(8)]
        self.assertEqual(refresh.budget_wait(usage, now), 8)
        self.assertEqual(refresh.budget_wait([now] * 8, now), 61)
        self.assertEqual(refresh.budget_wait([now + timedelta(seconds=10)], now), 18)
        self.assertEqual(len(refresh.retained_usage([now - timedelta(hours=23), now - timedelta(hours=25)], now)), 1)

    def test_restricted_account_stays_paused_on_next_run(self):
        error = urllib.error.HTTPError("https://example.invalid", 429, "limited", {}, None)
        code, data, state, mock = self.run_collector(error=error)
        self.assertTrue(state["apiPaused"])
        with patch.object(core, "api_get") as api:
            self.assertEqual(refresh.main(["--max-requests", "600"]), 1)
            api.assert_not_called()

    def test_learned_query_uses_all_hits_and_replaces_stale_evidence(self):
        now = datetime(2026, 11, 26, 3, tzinfo=timezone.utc)
        deals = [offer(), offer("2", "22"), offer("3", "33")]
        state = {"products": {}}
        metrics.record_request(state, now - timedelta(days=1), "successful query", "group", "3", "not_found", {"1", "2"}, {"1", "2"})
        choice = refresh.choose_search(deals, state, now, set(), True)
        self.assertEqual(choice[1:], ("successful query", "learned"))
        # Fairness: individual slots still try the oldest unconfirmed offer.
        self.assertEqual(refresh.choose_search(deals, state, now, set(), False)[2], "individual")
        metrics.record_request(state, now, "successful query", "learned", "1", "not_found", set(), set())
        self.assertNotEqual(refresh.choose_search(deals, state, now + timedelta(days=1), set(), True)[2], "learned")

    def test_daily_priority_skips_success_even_after_six_hours(self):
        now = datetime(2026, 11, 25, 14, tzinfo=timezone.utc)  # KST 23:00
        fresh = dict(offer(), lastCheckedAt="2026-11-25 01:00 KST")
        stale = offer("2", "22")
        state = {"products": {}}
        self.assertEqual([d["productId"] for d in refresh.plan([fresh, stale], state, 8, now)], ["2"])
        self.assertEqual(len(refresh.plan([fresh, stale], state, 8, now + timedelta(hours=2))), 2)

    def test_group_search_once_per_day_then_fallback_and_next_day_reset(self):
        now = datetime(2026, 11, 25, 3, tzinfo=timezone.utc)
        a, b = offer(), dict(offer("2", "22"), productName="프레지덩 휘핑크림, 1L, 2개")
        state = {"products": {}}
        target, query, strategy = refresh.choose_search([a, b], state, now, set(), True)
        self.assertEqual((query, strategy), (a["query"], "group"))
        metrics.record_request(state, now, query, strategy, target["productId"], "not_found", set(), set())
        self.assertEqual(refresh.choose_search([a, b], state, now, set(), True)[2], "individual")
        self.assertEqual(refresh.choose_search([a, b], state, now + timedelta(days=1), set(), True)[2], "group")

    def test_daily_metrics_deduplicate_observations_and_keep_failures(self):
        now = datetime(2026, 11, 25, 15, tzinfo=timezone.utc)  # KST 26th
        state = {"products": {}}
        for hours in (25, 1, 0):
            metrics.record_request(state, now - timedelta(hours=hours), "query", "group", "2", "not_found", {"1"}, {"1"} if hours else set())
        a, b = offer(), offer("2", "22")
        a["priceHistory"] += [["2026-11-24 23:00 KST", 8300], ["2026-11-25 23:00 KST", 8300], ["2026-11-26 00:00 KST", 8300]]
        a["lastCheckedAt"] = "2026-11-26 00:00 KST"
        report = metrics.coverage_report([a, b], state, now)
        self.assertEqual([r["confirmed"] for r in report["daily"]], [1, 1, 1])
        self.assertEqual([r["partial"] for r in report["daily"]], [True, False, True])
        self.assertTrue(next(d for d in report["missing72HoursOrRepeated"] if d["productId"] == "2")["repeatedlyUnconfirmed"])
        metrics.record_request(state, now, "query", "group", "2", "not_found", {"1"}, set())
        row = metrics.coverage_report([a, b], state, now)["daily"][-1]
        self.assertEqual((row["requests"], row["measuredUnique"], row["uniquePerRequest"]), (2, 1, 0.5))

    def test_request_error_is_measured_but_never_a_confirmation(self):
        code, data, state, mock = self.run_collector(error=ValueError("bad payload"))
        event = state["metrics"]["requests"][0]
        self.assertEqual((event["status"], event["observedIds"]), ("api_error", []))
        self.assertEqual(data["coverage"]["daily"][-1]["measuredUnique"], 0)

    def test_group_hit_replans_remaining_calls_and_counts_distinct_products(self):
        deals = [offer(), offer("2", "22"), dict(offer("3", "33"), query="다른 검색", productName="프레지덩 휘핑크림, 1L, 3개")]
        responses = iter([deals[:2], deals])
        def api(*args):
            return {"rCode": "0", "data": {"productData": next(responses)}}
        code, data, state, mock = self.run_collector(response=api, deals=deals, limit=2)
        self.assertEqual(code, 0)
        self.assertEqual(data["refreshRun"]["requests"], 2)
        self.assertEqual(data["refreshRun"]["refreshed"], 3)
        events = state["metrics"]["requests"]
        self.assertEqual([r["strategy"] for r in events], ["group", "individual"])
        self.assertEqual(events[1]["targetId"], "3")
        self.assertEqual(events[1]["newTodayIds"], ["3"])
        self.assertEqual(data["coverage"]["daily"][-1]["measuredUnique"], 3)


if __name__ == "__main__":
    unittest.main()
