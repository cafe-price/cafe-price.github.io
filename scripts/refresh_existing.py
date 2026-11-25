"""Recheck existing offers; search absence never means sold out.

State and request timestamps must travel with the repository between workers.
Only an exact product/offer match advances the price observation timestamp.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import fetch_cafe_deals as core

STATE_PATH = core.ROOT / "data" / "refresh_state.json"
STALE_HOURS = 24


def parse_at(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M KST").replace(tzinfo=core.KST)
    except (TypeError, ValueError):
        return None


def last_checked(deal):
    # Never use the file's updatedAt as a product observation.
    candidates = [deal.get("lastCheckedAt")]
    candidates += [p[0] for p in deal.get("priceHistory", []) if len(p) >= 2 and p[1]]
    valid = [x for x in candidates if parse_at(x)]
    return max(valid) if valid else None


def load_state():
    if not STATE_PATH.exists():
        return {"products": {}}
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    state.setdefault("products", {})
    return state


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


@contextmanager
def collection_lock():
    """OS lock releases even on process crash; serialize local manual runs too."""
    path = core.ROOT / "scripts" / ".collection.lock"
    with path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def search_query(deal, attempts):
    name = re.sub(r"\s+", " ", deal.get("productName", "").replace(",", " ")).strip()
    # Retry a different query, without permanently abandoning missing offers.
    original = str(deal.get("query") or "").strip()
    return (original if attempts % 2 and original else name)[:50].strip()


def plan(deals, state, limit, now):
    floor = datetime.min.replace(tzinfo=timezone.utc)
    eligible = []
    for deal in deals:
        if not deal.get("productId") or not deal.get("productName"):
            continue
        record = state["products"].get(str(deal["productId"]), {})
        checked = parse_at(last_checked(deal))
        attempted = parse_at(record.get("lastAttemptAt"))
        # Success: at least 6h between checks. Misses: retry after 6/12/24h.
        misses = record.get("consecutiveMisses", 0)
        delay = min(24, 6 * (2 ** min(misses - 1, 2))) if misses else 6
        if attempted and now - attempted < timedelta(hours=delay):
            continue
        eligible.append((attempted or checked or floor, checked or floor, str(deal["productId"]), deal))
    eligible.sort(key=lambda x: x[:3])
    return [x[3] for x in eligible[:limit]]


def exact_offer(stored, item):
    if str(stored.get("productId")) != str(item.get("productId")):
        return False
    a, b = core.vendor_item_id(stored), core.vendor_item_id(item)
    if a and b:
        return a == b
    name = core.option_name_key(stored)
    return bool(name and name == core.option_name_key(item))


def apply_result(deal, items, at, hist):
    matches = [item for item in items if exact_offer(deal, item)]
    if not matches:
        same_page = any(str(x.get("productId")) == str(deal["productId"]) for x in items)
        return "option_mismatch" if same_page else "not_found"
    item = matches[0]
    if item.get("isRocket") is not True:
        return "non_rocket"
    try:
        price = int(item.get("productPrice") or 0)
    except (TypeError, ValueError):
        return "invalid_price"
    if price <= 0:
        return "invalid_price"
    name = item.get("productName") or deal["productName"]
    bases = core.BASIS_BASES.get(deal.get("unitBasis"), ["100ml", "100g", "개당"])
    unit, basis, pack = core.calc_unit_price(name, price, bases, deal.get("category"))
    if not unit:
        return "invalid_unit"
    deal.update(productName=name, productPrice=price, unitPrice=round(unit),
                unitBasis=basis, packNote=pack, isRocket=True,
                lastCheckedAt=at, refreshStatus="ok")
    for field in ("productUrl", "productImage", "vendorItemId", "isFreeShipping"):
        if item.get(field) is not None and item.get(field) != "":
            deal[field] = item[field]
    core.with_vendor_item_id(deal)
    core.add_observation(hist, deal, at, "기존 상품 재확인")
    return "ok"


def freshness_stats(deals, now):
    fresh = sum(1 for d in deals if parse_at(last_checked(d))
                and timedelta(0) <= now - parse_at(last_checked(d)) < timedelta(hours=STALE_HOURS)
                and d.get("refreshStatus", "ok") == "ok")
    return {"within24Hours": fresh, "needsRecheck": len(deals) - fresh, "total": len(deals)}


def main(argv):
    now = datetime.now(timezone.utc)
    previous = core.load_previous()
    deals = previous.get("deals") or []
    state = load_state()
    limit = max(0, min(core.HOURLY_CAP, int(core._opt(argv, "--max-requests") or 8)))
    assume_used = max(0, int(core._opt(argv, "--assume-used") or 0))
    targets = plan(deals, state, limit, now)
    if "--dry-run" in argv:
        for deal in targets:
            record = state["products"].get(str(deal["productId"]), {})
            print(f"[plan] {deal['productId']} | {last_checked(deal)} | "
                  f"{search_query(deal, record.get('attempts', 0))}")
        print(f"[dry-run] 기존 상품 {len(deals)}개 중 재확인 {len(targets)}개 · API/파일 변경 없음")
        return 0
    access = os.environ.get("COUPANG_ACCESS_KEY", "").strip()
    secret = os.environ.get("COUPANG_SECRET_KEY", "").strip()
    if not access or not secret:
        print("[error] COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY 가 없습니다.", file=sys.stderr)
        return 2
    blocked_until = parse_at(state.get("blockedUntil"))
    if core.cooldown_active(now) or (blocked_until and now < blocked_until):
        print("[skip] API 쿨다운 중")
        return 0
    usage = core.load_usage()
    hist = core.load_history()
    core.reconcile_from_previous(hist, previous)
    attempted = refreshed = requests = 0
    had_error = False
    for target in targets:
        now = datetime.now(timezone.utc)
        # A preceding query may already have refreshed this target incidentally.
        latest = state["products"].get(str(target["productId"]), {})
        if parse_at(latest.get("lastAttemptAt")) and latest.get("status") == "ok" and now - parse_at(latest["lastAttemptAt"]) < timedelta(hours=6):
            continue
        if len(core.hour_window(usage, now)) + assume_used >= core.HOURLY_CAP:
            break
        wait = core.pace_wait(usage, now)
        if wait:
            time.sleep(wait)
        pid = str(target["productId"])
        record = state["products"].setdefault(pid, {})
        query = search_query(target, record.get("attempts", 0))
        at = core.kst_str(datetime.now(timezone.utc))
        record.update(lastAttemptAt=at, attempts=record.get("attempts", 0) + 1, query=query)
        target["lastAttemptAt"] = at
        attempted += 1
        # Reserve the request BEFORE network I/O, including failed requests.
        usage = core.hour_window(usage, datetime.now(timezone.utc))
        usage.append(datetime.now(timezone.utc))
        core.save_usage(usage)
        save_state(state)
        requests += 1
        params = {"keyword": query, "limit": core.LIMIT}
        if os.environ.get("COUPANG_SUB_ID"):
            params["subId"] = os.environ["COUPANG_SUB_ID"]
        try:
            payload = core.api_get(core.API_PATH, params, access, secret)
            if payload.get("rCode") not in (0, "0"):
                raise ValueError("API returned an error response")
            data = payload.get("data")
            items = data.get("productData", []) if isinstance(data, dict) else data
            if not isinstance(items, list):
                raise ValueError("Invalid productData")
            result = apply_result(target, items, at, hist)
            # One search can refresh other EXISTING exact offers too, but never add unrelated products.
            for other in deals:
                if other is target or not any(exact_offer(other, x) for x in items):
                    continue
                status = apply_result(other, items, at, hist)
                other["refreshStatus"] = status
                if status == "ok":
                    refreshed += 1
                    state["products"].setdefault(str(other["productId"]), {}).update(
                        lastAttemptAt=at, consecutiveMisses=0, status="ok")
        except Exception as exc:
            result = "api_error"
            had_error = True
            print(f"[error] {type(exc).__name__}; preserving last observed price")
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 429):
                state["blockedUntil"] = core.kst_str(datetime.now(timezone.utc) + timedelta(hours=24, minutes=1))
        target["refreshStatus"] = result
        record.update(status=result, consecutiveMisses=0 if result == "ok" else record.get("consecutiveMisses", 0) + 1)
        if result == "ok":
            refreshed += 1
        print(f"[refresh] {pid}: {result}")
        save_state(state)
        if had_error:
            break
    if attempted:
        core.finalize_history(hist)
        discounts = core.apply_price_stats(deals, hist)
        for deal in deals:
            deal["lastCheckedAt"] = last_checked(deal)
        core.save_history(hist)
        now = datetime.now(timezone.utc)
        previous.update(deals=deals, count=len(deals), lastRunAt=core.kst_str(now),
                        freshnessStats=freshness_stats(deals, now),
                        refreshRun={"requests": requests, "attempted": attempted, "refreshed": refreshed,
                                    "error": had_error},
                        priceStats={"products": len(hist["products"]), "discounts": discounts})
        if refreshed:
            previous["updatedAt"] = core.kst_str(now)
        core.write_json(previous)
    print(f"[done] API {requests}회 · 재확인 대상 {attempted}개 · 가격 관측 {refreshed}개")
    return 1 if had_error else 0


if __name__ == "__main__":
    with collection_lock():
        sys.exit(main(sys.argv[1:]))
