"""Recheck existing offers; search absence never means sold out.

State and request timestamps must travel with the repository between workers.
Only an exact product/offer match advances the price observation timestamp.
"""
from __future__ import annotations

import json
import copy
import os
import re
import sys
import time
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import fetch_cafe_deals as core
import refresh_metrics as metrics

STATE_PATH = core.ROOT / "data" / "refresh_state.json"
STALE_HOURS = 24
# Current /products/search docs (verified 2026-11-26): 50 requests/minute.
# Deliberately much lower; old discovery commands retain their legacy limit.
MINUTE_CAP = 8
DAY_CAP = 900  # rolling 24 hours, including failed/manual requests
RUN_CAP = 600


def retained_usage(usage, now):
    # Keep future timestamps too: clock skew must not erase reservations.
    return [stamp for stamp in usage if now - stamp < timedelta(hours=24)]


def budget_wait(usage, now):
    recent = [stamp for stamp in usage if now - stamp < timedelta(seconds=61)]
    if len(retained_usage(usage, now)) >= DAY_CAP:
        return None
    pace = core.pace_wait(usage, now)
    if len(recent) >= MINUTE_CAP:
        pace = max(pace, (sorted(recent)[-MINUTE_CAP] + timedelta(seconds=61) - now).total_seconds())
    return max(0, pace)


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
    confirmed_today = metrics.observed_ids(deals, metrics.day_key(now))
    for deal in deals:
        if not deal.get("productId") or not deal.get("productName"):
            continue
        record = state["products"].get(str(deal["productId"]), {})
        if str(deal["productId"]) in confirmed_today and deal.get("refreshStatus", "ok") == "ok":
            continue
        checked = parse_at(last_checked(deal))
        attempted = parse_at(record.get("lastAttemptAt"))
        # Missing offers remain eligible for a later sweep, including next day.
        delay = 4
        if attempted and now - attempted < timedelta(hours=delay):
            continue
        eligible.append((attempted or checked or floor, checked or floor, str(deal["productId"]), deal))
    eligible.sort(key=lambda x: x[:3])
    return [x[3] for x in eligible[:limit]]


def choose_search(deals, state, now, used_queries, group_turn):
    """Alternate shared original queries and exact names; never repeat a run query."""
    candidates = plan(deals, state, len(deals), now)
    today = metrics.day_key(now)
    used_queries = used_queries | {r["query"] for r in state.get("metrics", {}).get("requests", [])
                                   if r["at"].startswith(today) and r["status"] != "api_error"}
    group_used = {r["query"] for r in state.get("metrics", {}).get("requests", [])
                  if r["at"].startswith(today) and r["strategy"] == "group"}
    if group_turn:
        # Reuse queries that actually found exact offers, not just their target.
        # The latest normal response replaces older evidence (ranking can change).
        learned = {}
        for event in state.get("metrics", {}).get("requests", []):
            if event["status"] not in ("api_error", "interrupted"):
                learned[event["query"]] = set(event["observedIds"])
        ranked = []
        for q, ids in learned.items():
            members = [d for d in candidates if str(d["productId"]) in ids]
            if members and q not in used_queries:
                ranked.append((-len(members), candidates.index(members[0]), q, members[0]))
        if ranked:
            _, _, q, target = min(ranked, key=lambda row: row[:3])
            return target, q, "learned"
        groups = {}
        for d in candidates:
            q = str(d.get("query") or "").strip()[:50].strip()
            if q:
                groups.setdefault(q, []).append(d)
        for q, members in groups.items():
            if len(members) >= 2 and q not in used_queries and q not in group_used:
                return members[0], q, "group"
    for d in candidates:
        attempts = state["products"].get(str(d["productId"]), {}).get("attempts", 0)
        for q in (search_query(d, attempts), search_query(d, 0)):
            if q and q not in used_queries:
                return d, q, "individual"
    return None


def exact_offer(stored, item):
    if str(stored.get("productId")) != str(item.get("productId")):
        return False
    a, b = core.vendor_item_id(stored), core.vendor_item_id(item)
    if a:
        return bool(b and a == b)
    name = core.option_name_key(stored)
    return bool(name and name == core.option_name_key(item))


def apply_result(deal, items, at, hist, source="기존 상품 재확인"):
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
    # Price observation and quantity parsing are separate facts. A matching
    # offer's valid price must not be discarded because its title omits units.
    # Never carry the old unit price forward alongside a newly observed price.
    deal.update(productName=name, productPrice=price, unitPrice=round(unit) if unit else None,
                unitBasis=basis, packNote=pack, isRocket=True,
                lastCheckedAt=at, refreshStatus="ok", unitStatus="ok" if unit else "unconfirmed")
    for field in ("productUrl", "productImage", "vendorItemId", "isFreeShipping"):
        if item.get(field) is not None and item.get(field) != "":
            deal[field] = item[field]
    core.with_vendor_item_id(deal)
    core.add_observation(hist, deal, at, source)
    return "ok"


def freshness_stats(deals, now):
    fresh = sum(1 for d in deals if parse_at(last_checked(d))
                and timedelta(0) <= now - parse_at(last_checked(d)) < timedelta(hours=STALE_HOURS)
                and d.get("refreshStatus", "ok") == "ok")
    return {"within24Hours": fresh, "needsRecheck": len(deals) - fresh, "total": len(deals)}


def checkpoint(previous, deals, hist, state, requests, attempted, refreshed, had_error):
    """Keep observations as well as reservations if a long runner is interrupted."""
    core.finalize_history(hist)
    discounts = core.apply_price_stats(deals, hist)
    for deal in deals:
        deal["lastCheckedAt"] = last_checked(deal)
    core.save_history(hist)
    now = datetime.now(timezone.utc)
    previous.update(deals=deals, count=len(deals), lastRunAt=core.kst_str(now),
                    freshnessStats=freshness_stats(deals, now),
                    refreshRun={"requests": requests, "attempted": attempted,
                                "refreshed": refreshed, "error": had_error},
                    priceStats={"products": len(hist["products"]), "discounts": discounts})
    report = metrics.coverage_report(deals, state, now)
    previous["coverage"] = {k: report[k] for k in ("generatedAt", "measurementStartedAt", "daily")}
    previous["coverage"]["missing72HoursOrRepeated"] = len(report["missing72HoursOrRepeated"])
    if refreshed:
        previous["updatedAt"] = core.kst_str(now)
    core.write_json(previous)


def main(argv):
    now = datetime.now(timezone.utc)
    previous = core.load_previous()
    deals = previous.get("deals") or []
    state = load_state()
    limit = max(0, min(RUN_CAP, int(core._opt(argv, "--max-requests") or 600)))
    assume_used = max(0, int(core._opt(argv, "--assume-used") or 0))
    if "--dry-run" in argv:
        simulated, used = copy.deepcopy(state), set()
        count = 0
        for slot in range(limit):
            choice = choose_search(deals, simulated, now, used, slot % 2 == 0)
            if not choice:
                break
            deal, query, strategy = choice
            used.add(query)
            simulated["products"].setdefault(str(deal["productId"]), {})["lastAttemptAt"] = core.kst_str(now)
            count += 1
            print(f"[plan] {strategy} | {deal['productId']} | {last_checked(deal)} | {query}")
        print(f"[dry-run] 기존 상품 {len(deals)}개 · 최대 검색 {count}회 · 실제 결과에 따라 다음 대상 재선정 · API/파일 변경 없음")
        return 0
    access = os.environ.get("COUPANG_ACCESS_KEY", "").strip()
    secret = os.environ.get("COUPANG_SECRET_KEY", "").strip()
    if not access or not secret:
        print("[error] COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY 가 없습니다.", file=sys.stderr)
        return 2
    blocked_until = parse_at(state.get("blockedUntil"))
    if state.get("apiPaused"):
        print("[skip] API restriction recorded; review required before resuming")
        return 1
    if core.cooldown_active(now) or (blocked_until and now < blocked_until):
        print("[skip] API 쿨다운 중")
        return 0
    usage = core.load_usage()
    hist = core.load_history()
    core.reconcile_from_previous(hist, previous)
    attempted = refreshed = requests = 0
    used_queries = set()
    observed_run = set()
    had_error = False
    started = time.monotonic()
    for slot in range(limit):
        if time.monotonic() - started >= 90 * 60:
            print("[stop] Runtime budget reached; remaining offers stay queued")
            break
        now = datetime.now(timezone.utc)
        choice = choose_search(deals, state, now, used_queries, slot % 2 == 0)
        if not choice:
            break
        target, query, strategy = choice
        # Outside calls conservatively consume both minute and daily headroom.
        budget_usage = retained_usage(usage, now) + [now] * assume_used
        wait = budget_wait(budget_usage, now)
        if wait is None or assume_used >= MINUTE_CAP:
            print("[skip] Safe request budget exhausted")
            break
        if wait:
            time.sleep(wait)
        now = datetime.now(timezone.utc)
        pid = str(target["productId"])
        record = state["products"].setdefault(pid, {})
        used_queries.add(query)
        confirmed_before = metrics.observed_ids(deals, metrics.day_key(now))
        observed = set()
        at = core.kst_str(now)
        record.update(lastAttemptAt=at, attempts=record.get("attempts", 0) + 1, query=query)
        target["lastAttemptAt"] = at
        attempted += 1
        # Reserve the request BEFORE network I/O, including failed requests.
        usage = retained_usage(usage, datetime.now(timezone.utc))
        usage.append(datetime.now(timezone.utc))
        core.save_usage(usage)
        event = metrics.record_request(state, now, query, strategy, pid, "interrupted", set(), set())
        save_state(state)
        requests += 1
        event["apiStatus"] = "interrupted"
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
            event.update(apiStatus="ok", returnedCount=len(items))
            result = apply_result(target, items, at, hist)
            # One search can refresh other EXISTING exact offers too, but never add unrelated products.
            for other in deals:
                if other is target or not any(exact_offer(other, x) for x in items):
                    continue
                status = apply_result(other, items, at, hist)
                other["refreshStatus"] = status
                if status == "ok":
                    observed.add(str(other["productId"]))
                    state["products"].setdefault(str(other["productId"]), {}).update(
                        lastAttemptAt=at, consecutiveMisses=0, status="ok")
        except Exception as exc:
            result = "api_error"
            had_error = True
            event["apiStatus"] = "error"
            print(f"[error] {type(exc).__name__}; preserving last observed price")
            # Never repeatedly probe a restricted account on the next schedule.
            state["blockedUntil"] = core.kst_str(datetime.now(timezone.utc) + timedelta(hours=24, minutes=1))
            if isinstance(exc, urllib.error.HTTPError):
                event["httpStatus"] = exc.code
                if exc.code in (401, 403, 429):
                    state.update(apiPaused=True, pauseReason=f"HTTP {exc.code}")
            elif isinstance(exc, ValueError):
                state.update(apiPaused=True, pauseReason="Invalid or error API response")
        target["refreshStatus"] = result
        record.update(status=result, consecutiveMisses=0 if result == "ok" else record.get("consecutiveMisses", 0) + 1)
        if result == "ok":
            observed.add(pid)
        observed_run.update(observed)
        refreshed = len(observed_run)
        event.update(status=result, observedIds=sorted(observed), newTodayIds=sorted(observed - confirmed_before))
        print(f"[refresh] {pid}: {result}; API={event['apiStatus']}; exact observations={len(observed)}")
        save_state(state)
        if requests % 25 == 0:
            checkpoint(previous, deals, hist, state, requests, attempted, refreshed, had_error)
        if had_error:
            break
    if attempted:
        checkpoint(previous, deals, hist, state, requests, attempted, refreshed, had_error)
    print(f"[done] API {requests}회 · 재확인 대상 {attempted}개 · 가격 관측 {refreshed}개")
    return 1 if had_error else 0


if __name__ == "__main__":
    with collection_lock():
        sys.exit(main(sys.argv[1:]))
