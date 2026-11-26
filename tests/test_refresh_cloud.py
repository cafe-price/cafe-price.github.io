import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fetch_cafe_deals as core
import refresh_cloud as cloud
import refresh_existing as refresh


def stored():
    return dict(productId="9595365819", vendorItemId="96123294199",
        productUrl="https://link.coupang.com/re/AFFSDP?lptag=TEST&pageKey=9595365819&itemId=29203033571&vendorItemId=96123294199",
        productName="세일즈카페 에스프레소 분쇄, 1kg, 5개", productPrice=170000,
        unitPrice=3400, unitBasis="100g당", isRocket=True, category="원두·커피",
        lastCheckedAt="2026-11-16 12:00 KST")


def response():
    # Relevant fields from the successful public playground response, 2026-11-26.
    return dict(product_id="9595365819", item_id="29203033571", vendor_item_id="96123294199",
        title="세일즈카페 고소한 다크초콜릿 시그니쳐 블렌드 에스프레소 분쇄, 1kg, 5개",
        price={"currency": "KRW", "sale_price": 170000,
               "tiers": [{"type": "SALES", "price": 170000}]},
        is_rocket=True, delivery={"type": "ROCKET_DELIVERY"},
        availability={"is_sold_out": False, "is_in_stock": True})


class CloudTests(unittest.TestCase):
    def test_exact_offer_and_base_price_ignore_member_coupon_prices(self):
        raw = response()
        raw["price"].update(price=100000, coupon_price=100000)
        raw["price"]["tiers"].append({"type": "MEMBER", "price": 90000})
        self.assertEqual(cloud.normalize(stored(), raw)["productPrice"], 170000)

    def test_wrong_option_unknown_stock_and_conditional_only_price_are_rejected(self):
        samples = []
        r = response(); r["vendor_item_id"] = "95586820183"; samples.append(r)
        r = response(); r["product_id"] = "999"; samples.append(r)
        r = response(); r["availability"] = {}; samples.append(r)
        r = response(); r["availability"]["is_sold_out"] = True; samples.append(r)
        r = response(); r["price"] = {"currency": "KRW", "price": 500, "coupon_price": 500}; samples.append(r)
        r = response(); r["price"]["tiers"][0]["price"] = True; samples.append(r)
        r = response(); r["is_rocket"] = False; samples.append(r)
        r = response(); r["variants"] = [{"is_selected": True, "vendor_item_id": "wrong"}]; samples.append(r)
        for r in samples:
            with self.subTest(response=r), self.assertRaises(ValueError):
                cloud.normalize(stored(), r)

    def test_merchant_rocket_and_relocated_item_keep_exact_vendor_identity_and_pack(self):
        d = dict(stored(), productId="8887691390", vendorItemId="92269420301", unitBasis="개당",
            category="컵·뚜껑", productUrl="https://link.coupang.com/re/AFFSDP?itemId=25273753959&vendorItemId=92269420301")
        raw = response()
        raw.update(product_id="8887691390", item_id="29469067525", vendor_item_id="92269420301",
            title="KP 아이스컵 1박스", is_rocket=None, delivery={"type": "ROCKET_MERCHANT"},
            variants=[{"is_selected": True, "vendor_item_id": "92269420301", "item_id": "29469067525",
                       "item_name": "KP 92파이 16온스 490ml 아이스컵 1박스, 1000개입, 1개"}])
        raw["price"]["tiers"][0]["price"] = 48510
        offer = cloud.normalize(d, raw)
        self.assertEqual(offer["itemId"], "29469067525")
        self.assertEqual(refresh.apply_result(d, [offer], "2026-11-26 12:00 KST", {"products": {}}), "ok")
        self.assertEqual(d["unitPrice"], 49)

    def test_budget_is_reserved_and_counts_failures_across_runs(self):
        now = datetime.now(timezone.utc)
        state = {"products": {}}
        with patch.object(refresh, "save_state") as save:
            cloud.reserve(state, stored(), now)
            self.assertEqual(cloud.budget(state, now), 699)
            save.assert_called_once()
        state["cloud"]["months"][now.astimezone(core.KST).strftime("%Y-%m")] = cloud.MONTH_CAP
        with self.assertRaisesRegex(ValueError, "budget_exhausted"):
            cloud.reserve(state, stored(), now)

    def test_one_daily_success_retry_delay_and_limit(self):
        now = datetime.now(timezone.utc)
        d = stored(); state = {"products": {}}
        self.assertEqual(len(cloud.choose([d], state, now)), 1)
        with patch.object(refresh, "save_state"):
            cloud.reserve(state, d, now)
            self.assertEqual(cloud.choose([d], state, now), [])
            later = now + timedelta(hours=4)
            self.assertEqual(len(cloud.choose([d], state, later)), 1)
            cloud.reserve(state, d, later)
            if now.astimezone(core.KST).date() == later.astimezone(core.KST).date():
                self.assertEqual(cloud.choose([d], state, later + timedelta(minutes=1)), [])
        fresh = dict(d, lastCheckedAt=core.kst_str(now))
        self.assertEqual(cloud.choose([fresh], {"products": {}}, now), [])

    def test_mixed_run_persists_observation_and_failure_without_deleting_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "data").mkdir()
            d = stored(); failed = dict(stored(), productId="2", vendorItemId="22")
            previous = {"status": "ok", "deals": [d, failed], "count": 2}
            state, hist = {"products": {}}, {"products": {}}
            def fetch(deal, key):
                saved = json.loads((root / "data/state.json").read_text())
                self.assertGreater(saved["cloud"]["days"][core.kst_str(datetime.now(timezone.utc))[:10]], 0)
                if deal["productId"] == "2":
                    return None, "option_mismatch", False
                return cloud.normalize(deal, response()), "ok", False
            with patch.object(core, "OUT_PATH", root / "deals.json"), \
                 patch.object(core, "HISTORY_PATH", root / "data/history.json"), \
                 patch.object(refresh, "STATE_PATH", root / "data/state.json"), \
                 patch.object(cloud.time, "sleep"):
                self.assertEqual(cloud.run(previous, state, hist, "test", 2, fetch), 1)
            saved = json.loads((root / "deals.json").read_text())
            self.assertEqual(saved["count"], 2)
            self.assertEqual(saved["deals"][0]["productPrice"], 170000)
            self.assertEqual(saved["deals"][0]["refreshStatus"], "ok")
            self.assertIn("lptag=TEST", saved["deals"][0]["productUrl"])
            self.assertEqual(saved["deals"][1]["lastCheckedAt"], failed["lastCheckedAt"])
            self.assertEqual(saved["deals"][1]["refreshStatus"], "option_mismatch")
            self.assertEqual(len(hist["products"]["9595365819"]["observations"]), 1)

    def test_disabled_mode_never_requests_or_mutates(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(sys, "argv", ["refresh_cloud.py"]), \
             patch.object(core, "load_previous", return_value={"deals": [stored()]}), \
             patch.object(refresh, "load_state", return_value={"products": {}}), \
             patch.object(cloud, "run") as run:
            self.assertEqual(cloud.main(), 2)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
