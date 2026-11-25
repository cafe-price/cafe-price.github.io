"""Measured search yield and calendar-day coverage, without invented observations."""
from collections import Counter
from datetime import datetime, timedelta

import fetch_cafe_deals as core


def day_key(now):
    return now.astimezone(core.KST).date().isoformat()


def observed_ids(deals, day):
    result = set()
    for deal in deals:
        times = [deal.get("lastCheckedAt", "")]
        times += [x[0] for x in deal.get("priceHistory", []) if len(x) >= 2 and x[1]]
        days = [x[0] for x in deal.get("dailyHistory", []) if len(x) >= 6 and x[5] > 0]
        if any(str(t).startswith(day + " ") for t in times) or day in days:
            result.add(str(deal["productId"]))
    return result


def record_request(state, now, query, strategy, target, status, observed, new_today):
    metrics = state.setdefault("metrics", {"startedAt": core.kst_str(now), "requests": []})
    cutoff = (now.astimezone(core.KST).date() - timedelta(days=30)).isoformat()
    metrics["requests"] = [r for r in metrics["requests"] if r["at"][:10] >= cutoff]
    metrics["requests"].append({"at": core.kst_str(now), "query": query,
        "strategy": strategy, "targetId": str(target), "status": status,
        "observedIds": sorted(observed), "newTodayIds": sorted(new_today)})
    return metrics["requests"][-1]


def coverage_report(deals, state, now):
    today = day_key(now)
    metrics = state.get("metrics", {})
    started = metrics.get("startedAt")
    requests = metrics.get("requests", [])
    total = len(deals)
    daily = []
    start_day = started[:10] if started else today
    for offset in range(2, -1, -1):
        day = (now.astimezone(core.KST).date() - timedelta(days=offset)).isoformat()
        if day < start_day:
            continue
        calls = [r for r in requests if r["at"].startswith(day)]
        observed = observed_ids(deals, day)
        measured = set().union(*(set(r["observedIds"]) for r in calls)) if calls else set()
        daily.append({"date": day, "confirmed": len(observed), "total": total,
            "percent": round(100 * len(observed) / total, 1) if total else 0,
            "requests": len(calls), "measuredUnique": len(measured),
            "uniquePerRequest": round(len(measured) / len(calls), 2) if calls else None,
            "partial": day == today or day == start_day})
    recent_cutoff = (now.astimezone(core.KST).date() - timedelta(days=2)).isoformat()
    recent = [r for r in requests if r["at"][:10] >= recent_cutoff]
    successful = set().union(*(set(r["observedIds"]) for r in recent)) if recent else set()
    failures = {}
    for r in recent:
        if r["status"] != "ok":
            failures.setdefault(r["targetId"], set()).add(r["at"][:10])
    missing = []
    for d in deals:
        pid = str(d["productId"])
        at = d.get("lastCheckedAt")
        try:
            age = (now - datetime.strptime(at, "%Y-%m-%d %H:%M KST").replace(tzinfo=core.KST)).total_seconds() / 3600
        except (TypeError, ValueError):
            age = None
        failure_days = len(failures.get(pid, set()))
        repeated = failure_days >= 2 and pid not in successful
        if age is None or age >= 72 or repeated:
            missing.append({"productId": pid, "productName": d["productName"],
                "lastCheckedAt": at, "hoursSinceCheck": round(age, 1) if age is not None else None,
                "status": d.get("refreshStatus", "not_attempted"),
                "failedDays": failure_days, "repeatedlyUnconfirmed": repeated})
    queries = {}
    for r in recent:
        key = (r["query"], r["strategy"])
        q = queries.setdefault(key, {"query": key[0], "strategy": key[1], "requests": 0,
            "observations": 0, "newToday": 0, "statuses": Counter()})
        q["requests"] += 1
        q["observations"] += len(r["observedIds"])
        q["newToday"] += len(r["newTodayIds"])
        q["statuses"][r["status"]] += 1
    return {"generatedAt": core.kst_str(now), "measurementStartedAt": started,
        "note": "한국시간 기준. 첫날·오늘은 부분 집계. API 횟수는 측정 시작 이후만 포함. 미발견은 품절이 아닙니다.",
        "daily": daily, "missing72HoursOrRepeated": missing,
        "queries": sorted(queries.values(), key=lambda q: (-q["newToday"], q["query"]))}
