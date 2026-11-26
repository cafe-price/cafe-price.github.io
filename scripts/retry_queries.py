"""Queries derived from the actual listing, never from a different product."""
import re

# Short, option-specific alternatives for listings whose long title did not
# retrieve the stored offer. Matching still requires both exact IDs.
FOCUSED_QUERIES = {
    '4975075206': ['서울우유 멸균 1L 10개'],
    '8989596765': ['에이투밀크 1L 1개', '에이투 밀크 멸균우유'],
    '7060182058': ['서울우유 유기농 120ml 32개'],
    '9628259219': ['도심커피 로부스타 1kg 2개'],
    '9050227380': ['베트남 로부스타 싱글오리진 1kg'],
    '9618453633': ['브로일링커피 화로 1kg 홀빈'],
    '9434882675': ['그라도스커피 스페셜 블렌드 1kg 홀빈'],
    '7672649461': ['예가체프 시다모 네추럴 500g 홀빈'],
    '9463147601': ['노블빈 다크초코 1kg 6개 홀빈'],
    '9413440319': ['코에코에 프렌즈블랜드 1kg 홀빈'],
    '9417321086': ['코에코에 안녕블랜드 200g 홀빈'],
    '7689350970': ['네이쳐티 헤이즐넛 1L 6개'],
    '9265147894': ['네이쳐티 카라멜 1L 10개'],
    '6702262397': ['토스키 초콜렛 1L 6개'],
    '7278937307': ['토스키 피치', '토스키 복숭아 시럽'],
    '335762157': ['샷 헤이즐넛 시럽 1000ml'],
    '7677876919': ['카일스 초콜릿 1kg 2개'],
    '7958862797': ['더블데이 로얄 밀크티 800g 4개'],
    '7973180924': ['서울팩토리 딸기 1.2kg'],
    '7398481358': ['엔칸토 제주감귤 1.8kg'],
    '7418316890': ['베버시티 파인애플 1kg'],
    '7136952609': ['고려인삼 벌꿀 750g 2개'],
    '9717971496': ['선인 화이트 컴파운드 초코칩 1kg'],
    '7787581793': ['버블펄스 타피오카 70g 50개'],
    '6853093017': ['헬로우그린 시나몬 300g'],
    '8168828141': ['킹아일랜드 코코넛칩 500g'],
    '9192487825': ['글로리판 이스트 500g'],
    '9629750440': ['우리밀 발아 통밀가루 강력분 500g'],
    '8293262854': ['카일스 카쵸코 1kg'],
    '7973208392': ['투데이넛 아몬드 슬라이스 300g'],
    '8180109513': ['발로나 코코아 1kg'],
    '24702790': ['우리차 옥수수수염차 150티백'],
    '82802': ['녹차원 옥수수수염차 50티백'],
    '7851945409': ['대한다업 블랙 보리차 60티백'],
    '7778899675': ['립톤 복숭아 1.5kg 4개'],
    '8244145059': ['립톤 레몬 1.5kg 2개'],
    '9707225326': ['수팍 종이컵 185ml 1000개'],
    '8540986451': ['늘푸른 종이컵 180ml 500개입 10개'],
    '9724890424': ['일자빨대 블랙 개별포장 500개입'],
    '8900660404': ['제이디팩 빨대 7 280 검정 500개'],
    '9632198895': ['플라마트 컵홀더 32온스 800개'],
    '7279542281': ['네이처컨테이너 주황 빨대 21cm 500개'],
    '9767112517': ['파스텔 자바라 빨대 200개입 2개'],
    '1951259803': ['냅킨공장 칵테일 냅킨 Brown 3000개입 4개'],
    '9649725029': ['스카트 뽑아쓰는 키친타올 150매 10개'],
    '9637013331': ['OPP 쌈비닐 190X200 1000장'],
    '9481002366': ['ProTap 니트릴 장갑 L 검은색 100매 5개'],
    '9558872810': ['부직포 위생모 화이트 100개입'],
    '7927510981': ['러버랩 니트릴 블랙 M 100p'],
    '9557536554': ['친환경 원형 접시 18cm 50개입'],
}

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
    candidates.extend(FOCUSED_QUERIES.get(str(deal['productId']), []))
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
