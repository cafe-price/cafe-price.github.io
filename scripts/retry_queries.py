"""Queries derived from the actual listing, never from a different product."""
import re

# These names are taken from the stored listing. Long marketing copy previously
# pushed the brand or the selected pack out of the 50-character query.
BASE_NAMES = {
    "9212679642": "그라도스커피 고소한 블렌드 브라질 콜롬비아 인도",
    "9224933318": "그라도스커피 달콤한 블렌드 콜롬비아 과테말라",
    "9434882675": "그라도스커피 스페셜 블렌드 브라질 콜롬비아 에티오피아",
    "9413440319": "코에코에 프렌즈블랜드 스페셜티 원두",
    "9417321086": "코에코에 안녕블랜드 원두",
    "9618453633": "브로일링커피 화로 블렌드 Brazier 원두",
    "9628259219": "도심커피 베트남 로부스타 G1 원두",
    "9561213281": "소이로커피공장 베트남 로부스타 G1 원두",
    "9595365819": "세일즈카페 다크초콜릿 시그니쳐 블렌드 에스프레소 분쇄",
    "9463147601": "노블빈 다크초코 에스프레소 블렌드 원두",
    "9632198895": "플라마트 컵홀더 32온스 107파이 800개입",
    "8801620464": "에코윌 PLA빨대 개별포장 7 21 커피주스용",
    "9637013331": "OPP 쌈비닐 포장 봉투 소 190X200+40 1000장",
    "7290063313": "씨엘 400ml 13온스 종이컵 1000개",
    "9264781647": "다예 건강한 하루차 티타임 4종 32티백",
}


def clean(text):
    text = re.sub(r"\[(?:로켓프레시|본사직영|선주문|8월여름한정이벤트)\]", " ", str(text))
    text = text.replace("(분쇄안함)", "").replace("(뚜껑별도)", "")
    text = re.sub(r"(?<!\d)1개입\b", " ", text)
    return re.sub(r"\s+", " ", text.replace(",", " ")).strip()


def variants(deal):
    raw = deal.get("productName", "")
    base = BASE_NAMES.get(str(deal["productId"]), clean(raw.split(",")[0]))
    # Options are supplied by the stored title, never guessed from its old query.
    options = clean(" ".join(raw.split(",")[1:]))
    sizes = re.findall(r"\d+(?:\.\d+)?\s*(?:kg|ml|g|L|l|개입|개|매|박스|세트)\b", options)
    spec = " ".join(dict.fromkeys(sizes))
    candidates = [f"{base} {spec}".strip(), clean(raw), base,
                  f"{' '.join(base.split()[:4])} {spec}".strip()]
    result = []
    for query in candidates:
        if len(query) > 50:
            # Preserve the known brand/product and pack, without cutting words.
            room = max(10, 49 - len(spec))
            query = base[:room].rsplit(" ", 1)[0] + (" " + spec if spec else "")
        query = clean(query)[:50].strip()
        if query and query not in result:
            result.append(query)
    return result


def choose(deals, state, cohort, counts, observed, used, today):
    used = used | {r["query"] for r in state.get("metrics", {}).get("requests", [])
                   if r["at"].startswith(today) and r["status"] not in ("api_error", "interrupted")}
    pending = [d for d in deals if str(d["productId"]) in cohort
               and str(d["productId"]) not in observed and counts.get(str(d["productId"]), 0) < 3]
    pending.sort(key=lambda d: (counts.get(str(d["productId"]), 0),
                               cohort[str(d["productId"])].get("initialStatus") != "non_rocket",
                               str(d["productId"])))
    for deal in pending:
        pid = str(deal["productId"])
        # User now accepts general shipping. Recheck the previously exact query
        # once under the new rule, still within all request-rate budgets.
        if cohort[pid].get("initialStatus") == "non_rocket" and counts.get(pid, 0) == 0:
            prior = state["products"].get(pid, {}).get("query")
            if prior:
                return deal, prior, "retry_shipping"
        for query in variants(deal):
            if query not in used:
                return deal, query, "retry_exact"
    return None
