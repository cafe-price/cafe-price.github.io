"""Daily exact-offer refresh through Omkar's documented server API.

Opt-in only: CAFE_PRICE_CLOUD_ENABLED=true and OMKAR_API_KEY are required.
No local browser, Coupang credentials, search, or guessed prices are used.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import fetch_cafe_deals as core
import refresh_existing as refresh
import refresh_metrics as metrics

ENDPOINT = "https://coupang-scraper.omkar.cloud/products/details"
DAY_CAP = 700
MONTH_CAP = 24000
WORKERS = 4


def identity(deal):
    pid = str(deal.get("productId") or "")
    vid = core.vendor_item_id(deal)
    parsed = urllib.parse.urlparse(deal.get("productUrl") or "")
    args = urllib.parse.parse_qs(parsed.query)
    iid = str(deal.get("itemId") or (args.get("itemId") or [""])[0])
    if not all(x.isdigit() and int(x) > 0 for x in (pid, iid, vid)):
        raise ValueError("missing_identity")
    return pid, iid, vid


def direct_url(deal):
    pid, iid, vid = identity(deal)
    return f"https://www.coupang.com/vp/products/{pid}?itemId={iid}&vendorItemId={vid}"


def positive_price(value):
    return type(value) in (int, float) and value > 0 and value == int(value)


def normalize(deal, data):
    """Fail closed on wrong offers, conditional prices and uncertain availability."""
    pid, _, vid = identity(deal)
    if not isinstance(data, dict):
        raise ValueError("invalid_response")
    if str(data.get("product_id")) != pid or str(data.get("vendor_item_id")) != vid:
        raise ValueError("option_mismatch")
    iid = str(data.get("item_id") or "")
    if not iid.isdigit() or int(iid) <= 0:
        raise ValueError("missing_identity")
    # A page's itemId can change while its immutable vendorItemId remains the same.
    # Validate the actual selected offer, never echo the input URL as proof.
    selected = [v for v in (data.get("variants") or []) if v.get("is_selected") is True]
    if selected and (len(selected) != 1 or str(selected[0].get("vendor_item_id")) != vid
                     or str(selected[0].get("item_id")) != iid):
        raise ValueError("option_mismatch")
    availability = data.get("availability") or {}
    if availability.get("is_sold_out") is True or availability.get("is_in_stock") is False:
        raise ValueError("sold_out")
    if availability.get("is_in_stock") is not True:
        raise ValueError("availability_unknown")
    delivery = data.get("delivery") or {}
    rocket = data.get("is_rocket") is True or (
        data.get("is_rocket") is None and delivery.get("type") in {"ROCKET_DELIVERY", "ROCKET_MERCHANT"})
    if not rocket:
        raise ValueError("non_rocket")
    price = data.get("price") or {}
    if price.get("currency") != "KRW":
        raise ValueError("invalid_currency")
    tiers = [t.get("price") for t in price.get("tiers", []) if t.get("type") == "SALES"]
    # MEMBER/coupon/credit-card prices must never become an unconditional price.
    if tiers:
        if not all(positive_price(p) for p in tiers) or len(set(tiers)) != 1:
            raise ValueError("invalid_price")
        value = tiers[0]
    else:
        value = price.get("sale_price")
    if not positive_price(value):
        raise ValueError("invalid_price")
    name = selected[0].get("item_name") if selected else data.get("title")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("missing_name")
    return {"productId": pid, "vendorItemId": vid, "itemId": iid,
            "productName": name, "productPrice": int(value), "isRocket": True}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(deal, key):
    url = ENDPOINT + "?" + urllib.parse.urlencode({"product": direct_url(deal)})
    req = urllib.request.Request(url, headers={"API-Key": key, "Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=45) as response:
            data = json.load(response)
        return normalize(deal, data), "ok", False
    except urllib.error.HTTPError as exc:
        # No bodies or request headers in logs: they can contain provider secrets.
        return None, f"provider_http_{exc.code}", exc.code in (401, 402, 403, 429)
    except ValueError as exc:
        allowed = {"missing_identity", "invalid_response", "option_mismatch", "sold_out",
                   "availability_unknown", "non_rocket", "invalid_currency", "invalid_price", "missing_name"}
        return None, str(exc) if str(exc) in allowed else "invalid_response", False
    except Exception:
        return None, "provider_error", False


def budget(state, now):
    cloud = state.setdefault("cloud", {"days": {}, "months": {}, "products": {}})
    day, month = metrics.day_key(now), metrics.day_key(now)[:7]
    return max(0, min(DAY_CAP - cloud["days"].get(day, 0), MONTH_CAP - cloud["months"].get(month, 0)))


def choose(deals, state, now):
    cloud = state.setdefault("cloud", {"days": {}, "months": {}, "products": {}})
    day = metrics.day_key(now)
    checked = metrics.observed_ids(deals, day)
    result = []
    for d in deals:
        pid = str(d["productId"])
        if pid in checked:
            continue
        r = cloud["products"].get(pid, {})
        attempts = r.get("attempts", 0) if r.get("day") == day else 0
        at = refresh.parse_at(r.get("lastAttemptAt"))
        if attempts >= 2 or (at and now - at < timedelta(hours=4)):
            continue
        try:
            identity(d)
        except ValueError:
            continue
        result.append((attempts, refresh.last_checked(d) or "", pid, d))
    return [x[3] for x in sorted(result, key=lambda x: x[:3])]


def reserve(state, deal, now):
    if not budget(state, now):
        raise ValueError("budget_exhausted")
    cloud = state["cloud"]
    day, month, pid = metrics.day_key(now), metrics.day_key(now)[:7], str(deal["productId"])
    cloud["days"][day] = cloud["days"].get(day, 0) + 1
    cloud["months"][month] = cloud["months"].get(month, 0) + 1
    record = cloud["products"].setdefault(pid, {})
    attempts = record.get("attempts", 0) if record.get("day") == day else 0
    record.update(day=day, attempts=attempts + 1, lastAttemptAt=core.kst_str(now), status="interrupted")
    event = metrics.record_request(state, now, direct_url(deal), "cloud_direct", pid, "interrupted", set(), set())
    refresh.save_state(state)  # failures/crashes also consume the budget
    return event


def persist(previous, hist, state, requests, observed, errors, now):
    deals = previous["deals"]
    core.finalize_history(hist)
    discounts = core.apply_price_stats(deals, hist)
    core.save_history(hist)
    previous.update(count=len(deals), lastRunAt=core.kst_str(now),
        freshnessStats=refresh.freshness_stats(deals, now),
        refreshRun={"source": "omkar", "requests": requests, "attempted": requests,
                    "refreshed": len(observed), "error": bool(errors)},
        priceStats={"products": len(hist["products"]), "discounts": discounts})
    if observed:
        previous["updatedAt"] = core.kst_str(now)
    refresh.save_state(state)
    core.write_json(previous)


def run(previous, state, hist, key, limit=600, fetcher=fetch):
    deals = previous["deals"]
    requests, errors, observed = 0, 0, set()
    started = time.monotonic()
    blocked = refresh.parse_at(state.get("cloud", {}).get("blockedUntil"))
    if blocked and datetime.now(timezone.utc) < blocked:
        print("[cloud] provider cooldown")
        return 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while requests < limit and time.monotonic() - started < 36 * 60:
            now = datetime.now(timezone.utc)
            batch = choose(deals, state, now)[:min(WORKERS, budget(state, now), limit - requests)]
            if not batch:
                break
            jobs = []
            before = metrics.observed_ids(deals, metrics.day_key(now))
            for deal in batch:
                event = reserve(state, deal, now)
                requests += 1
                jobs.append((deal, event, pool.submit(fetcher, deal, key)))
            halt = False
            batch_statuses = []
            for deal, event, future in jobs:
                try:
                    item, status, fatal = future.result()
                except Exception:
                    item, status, fatal = None, "provider_error", False
                at = core.kst_str(datetime.now(timezone.utc))
                pid = str(deal["productId"])
                if status == "ok":
                    status = refresh.apply_result(deal, [item], at, hist, "Omkar 정확한 판매 옵션 조회")
                if status == "ok":
                    # Preserve affiliate link, but update its possibly relocated itemId.
                    deal["itemId"] = item["itemId"]
                    parsed = urllib.parse.urlparse(deal["productUrl"])
                    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                    query["itemId"] = [item["itemId"]]
                    deal["productUrl"] = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))
                    deal["priceSource"] = "omkar"
                    observed.add(pid)
                    event.update(observedIds=[pid], newTodayIds=[] if pid in before else [pid])
                    state["products"].setdefault(pid, {}).update(status="ok", consecutiveMisses=0, lastAttemptAt=at)
                else:
                    errors += 1
                deal.update(refreshStatus=status, lastAttemptAt=at)
                state["cloud"]["products"][pid]["status"] = status
                event["status"] = status
                batch_statuses.append(status)
                print(f"[cloud] {pid}: {status}")
                if fatal:
                    state["cloud"]["blockedUntil"] = core.kst_str(datetime.now(timezone.utc) + timedelta(hours=6))
                    halt = True
            if batch_statuses and all(s.startswith("provider_") or s == "invalid_response" for s in batch_statuses):
                state["cloud"]["blockedUntil"] = core.kst_str(datetime.now(timezone.utc) + timedelta(hours=6))
                halt = True
            persist(previous, hist, state, requests, observed, errors, datetime.now(timezone.utc))
            if halt:
                break
            time.sleep(1)
    print(f"[cloud] requests={requests}; confirmed={len(observed)}; failures={errors}")
    # CI must surface incomplete coverage even if some observations succeeded.
    missing = len(deals) - len(metrics.observed_ids(deals, metrics.day_key(datetime.now(timezone.utc))))
    print(f"[cloud] still unconfirmed today={missing}")
    return 1 if errors or missing else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-requests", type=int, default=600)
    args = parser.parse_args()
    previous, state = core.load_previous(), refresh.load_state()
    limit = max(0, min(600, args.max_requests))
    if args.dry_run:
        now = datetime.now(timezone.utc)
        print(json.dumps({"catalog": len(previous["deals"]), "eligible": len(choose(previous["deals"], state, now)),
          "maxRequests": min(limit, budget(state, now)), "dailyCap": DAY_CAP, "monthlyCap": MONTH_CAP,
          "sample": [direct_url(d) for d in choose(previous["deals"], state, now)[:3]]}, ensure_ascii=False))
        return 0
    if os.environ.get("CAFE_PRICE_CLOUD_ENABLED") != "true" or not os.environ.get("OMKAR_API_KEY"):
        print("[cloud] not enabled or missing OMKAR_API_KEY; no requests sent")
        return 2
    hist = core.load_history()
    core.reconcile_from_previous(hist, previous)
    return run(previous, state, hist, os.environ["OMKAR_API_KEY"], limit)


if __name__ == "__main__":
    with refresh.collection_lock():
        raise SystemExit(main())
