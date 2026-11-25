# 카페발주 단가표

카페 사장님이 발주 품목의 **상품 가격**과 **개당 · 100ml당 · 100g당 단가**를 12개 카테고리로 비교하는 모바일 웹 페이지입니다. 목록에는 **로켓배송이면서 상품명에 용량·수량이 적혀 단가를 계산할 수 있는 상품만** 싣습니다.

- 공개 주소: https://cafe-price.github.io/
- 정적 사이트: `index.html` + `deals.json` (별도 서버 없음)
- 모든 상품 카드에 실제 수집 기록 기반의 상태 배지(평균 대비 ↓/↑, 평균가 유지, 추적 1일차)를 표시하고, 카드를 누르면 **당일 시간대별 · 최근 30일 일별 가격 추이 모달**이 열립니다(구매 링크는 모달 안의 `쿠팡에서 최저가 확인 / 발주하기` 버튼).

> 이 페이지의 링크는 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.

## 운영 방식 (2026-11-25 수정)

GitHub Actions 워크플로 `.github/workflows/update-deals.yml`을 설치했습니다. 매시간 17분에 예약되며 GitHub 사정에 따라 지연될 수 있습니다. PC나 AI 실행은 필요하지 않습니다.

수집은 저장소 변수 `CAFE_PRICE_SCHEDULER_ENABLED`가 `true`일 때만 실행됩니다. 키 등록과 기존 피클 예약 중지를 확인하기 전에는 이 변수를 활성화하지 않습니다.

필수 저장소 Secrets: `COUPANG_ACCESS_KEY`, `COUPANG_SECRET_KEY`. 활성화 후 수동 실행에서 1회 조회로 연결을 먼저 확인합니다.

정기 작업은 최신 main을 받아 다음 명령을 실행합니다.

```bash
python scripts/fetch_cafe_deals.py --refresh-existing --max-requests 8
```

- 기본 실행도 기존 상품 재확인입니다. 신규 키워드 확장은 `--discover` 또는 `--wave`/`--only`로 명시한 경우에만 합니다.
- 성공적으로 확인한 가격 시각 `lastCheckedAt`과 검색 시도 시각 `lastAttemptAt`은 별개입니다. 과거 데이터는 실제 `priceHistory`에서만 확인 시각을 복원합니다.
- 화면은 24시간 미만의 관측가를 먼저 보여주며, 오래된 가격·재확인 실패 상품은 별도 영역으로 분리하고 할인 강조를 제거합니다. 최근 24시간 확인 상품만 보는 필터도 있습니다.
- 재확인은 오래 기다린 상품부터 진행하며 상품명/기존 검색어를 번갈아 사용합니다. 같은 productId와 판매 옵션이 확인된 경우만 가격·단가·이력을 갱신합니다. 다른 기존 상품이 함께 검색되면 그것도 갱신하지만, 무관한 신규 상품은 늘리지 않습니다.
- 검색 미발견, 다른 옵션, 비로켓, 가격/단가 확인 실패, API 오류를 별도 상태로 남깁니다. 미발견을 품절로 단정하지 않습니다. 실패 상품도 6/12/24시간 후 다시 대기열에 들어가며 영구 제외하지 않습니다.
- 호출 상한은 최근 60분 9회, 요청 간격은 최소 8초입니다. HTTP 오류도 호출로 계산합니다. 403/429 응답 시 24시간 이상 쿨다운을 기록합니다.
- `deals.json`, `data/price_history.json`, `data/refresh_state.json`, `data/api_usage.json`을 **함께 커밋**해야 다음 실행이 대기열과 사용량을 이어받습니다. 마지막 두 파일에는 비밀키가 없습니다.
- 여러 환경에서 같은 키로 호출하면 최신 사용기록을 공유하고 작업을 직렬화해야 합니다. 저장소 밖 검색 호출은 `--assume-used N`으로 합산합니다. 호출 수는 갱신 상품 수가 아니며 모든 상품의 일일 갱신을 보장하지 않습니다.
- `freshnessStats`와 `refreshRun`으로 최근 24시간 확인 수, 재확인 필요 수, 실제 관측 수를 확인합니다. 웹 화면의 신선도는 브라우저 시각 기준으로 다시 계산합니다.
- `automation/update-deals.yml`은 활성 워크플로의 참고 사본입니다. 피클 예약 작업과 동시에 수집하지 않습니다. 수집 결과 커밋 후 같은 워크플로에서 GitHub Pages를 직접 배포하며, 화면 코드 push도 배포합니다. GitHub Pages Source는 GitHub Actions를 사용합니다.

### 일일 확인률 개선 및 측정

- 상품 수를 유지하며 한국시간 오늘 이미 확인한 상품의 중복 목표 조회를 줄입니다. 공동 검색어와 개별 검색을 번갈아 사용하고, 검색 후 남은 대기열을 다시 계산합니다. 정상 응답을 받은 동일 검색어는 당일 반복하지 않습니다.
- 정확한 상품·판매 옵션이 일치할 때만 가격을 기록합니다. 가격이 같아도 확인 이력은 남기고, 검색 미발견은 품절로 단정하지 않습니다.
- `refresh_state.json`의 `metrics`는 검색별 전략·실제 확인 상품·오늘 최초 확인 상품·실패 사유를 기록합니다. 31일 보존하며 과거 호출 횟수를 만들어 채우지 않습니다.
- `python scripts/report_coverage.py`는 API 호출 없이 최근 3일의 일일 고유 확인 상품 수와 검색 효율, 72시간 이상 미확인 및 2개 이상 날짜에서 반복 실패한 상품을 `data/coverage_report.json`에 저장합니다. 전체 일일 확인 수는 실제 가격 이력 기준이고, 검색 횟수·효율은 측정 시작 이후만 계산합니다. 첫날과 오늘은 부분 집계입니다.
- 운영자는 Actions 실행 요약과 `coverage-report` 아티팩트에서 결과를 확인합니다. 고객용 사이트에는 확인률·호출 횟수·측정 보고서를 노출하지 않습니다. 오늘은 2026-11-25부터 부분 측정하며 11월 28일에는 26~27일의 두 종일 결과를 비교할 수 있습니다. 이 일정은 별도 알림을 예약한 것이 아닙니다.
- 사이트를 열어 두면 1분마다 배포된 데이터를 다시 읽습니다. 검색·필터는 유지하고, 연결 실패 시 마지막 목록을 보존합니다. 상세창을 보는 동안에는 목록 교체를 보류합니다. 쿠팡 가격의 실시간 변경 감지를 보장하지 않습니다.

검증: `python -m unittest discover -s tests -v`, `node tests/frontend.test.cjs`, `python scripts/fetch_cafe_deals.py --refresh-existing --dry-run`.

## 노출 필터 (로켓배송 + 단가 확인)

`deals.json` 에는 **`isRocket == true` 이고 `unitPrice` 가 계산된 상품만** 들어갑니다. 그래야 카드마다 비교 가능한 단가가 있고, 사이트 상단의 `🚀 로켓배송 · 단가 확인 품목만` 배지가 실제 내용과 일치합니다.

- 수집·병합(`merge_deals`)과 `--recompute` / `--prune-only` 모든 경로에서 같은 필터(`passes_listing_filter`)를 거칩니다.
- 비로켓 상품과 상품명에 용량·수량이 없어 단가를 낼 수 없는 상품은 `deals.json` 에서 빠지지만, `data/price_history.json` 의 **기존 관측 이력은 지우지 않고 보존**합니다.
- `index.html` 도 데이터를 한 번 더 확인해 조건 밖 품목은 그리지 않고, 숨긴 건수가 있으면 배지에 `조건 밖 N건 숨김`으로 표시합니다.
- 카테고리는 `당류·토핑`, `크림·연유`, `베이킹 재료` 3개가 추가되어 총 12개이며, 수집 키워드는 `TARGETS` 에 있습니다. `wave: 3` 키워드 묶음은 이 3개 카테고리와 `시럽·소스` 를 채우기 위한 규격형 키워드(`설탕 3kg` · `토스키 시럽` 등)입니다.

## 가격 이력 · 평균 대비 할인율

`data/price_history.json` 에 상품(`productId`)별 관측 가격을 쌓고, 그 평균과 비교해 할인율을 계산합니다.

- 관측 1건 = 그 수집 시각에 확인된 해당 상품의 `productPrice`. **보간·추정값은 넣지 않습니다.**
- `deals.json` 각 상품에 `avgPrice`(누적 평균가), `minPrice`(최저 관측가), `maxPrice`(최고 관측가), `priceCount`(관측 횟수), `discountRate`(평균 대비 하락률), `priceHistory`(관측 시각·가격 목록, 최근 120건), `dailyHistory`(최근 30일 일별 집계), `uniqueDays`(기록 일수)를 함께 저장합니다.
- `dailyHistory` 형식: `[날짜(YYYY-MM-DD), 최저, 최고, 평균, 마지막, 관측횟수]` — 오래된 순, 최대 30일.
- `discountRate` = 관측 2회 이상이고 현재가가 누적 평균보다 낮을 때 `round((avgPrice - productPrice) / avgPrice * 100)`, 그 외에는 `null`.
- 사이트는 **모든 상품**에 상태 배지를 붙입니다: `discountRate >= 1` → 빨강 `평균 대비 N%↓`, 평균보다 높음 → 주황 `평균 대비 +N%↑`, 평균과 같은 가격 → 회색 `평균가 유지 · N회 동일`, 관측 1회 → 파랑 `추적 1일차`. (0%·가짜 정가 표시 없음)
- 카드 하단에는 모든 상품에 `평균 X원 · 최저 Y원`을 같은 형식으로 표시합니다.
- 카드를 누르면 요약 타일(현재가·30일 평균·30일 최저·30일 최고) 아래에 **`당일 추이` / `30일 추이` 전환 탭**이 있고, 기본은 오늘 관측이 2회 이상이면 `당일 추이`, 1회뿐이면 `30일 추이`입니다.
  - `당일 추이`: 가장 최근 관측일의 `priceHistory` 관측 시각(`HH:MM`)·가격을 시간순으로 이은 SVG 라인 차트 + 관측 기록 목록(최근 순, 직전 관측 대비 ▲/▼). x축에 실제 관측 시각을 적고, 가로 위치는 관측 순서(보간 없음)입니다. 첫 관측 대비 변동(예: `▲ 2,670원 (+15.4%)`)을 차트 위에 표시합니다. 관측 1회뿐인 날은 가로 기준선+점+안내 문구로 그립니다.
  - `30일 추이`: 일별 평균가 SVG 라인 차트(막대 = 당일 최저~최고) + 일별 최저·평균·마지막 가격 목록. 하루치만 있어도 수평 기준선과 점, “매일 누적되어 최대 30일 추이가 그려집니다” 안내가 표시됩니다.
- 보관량: 원시 관측은 최근 120건(최근 35일치가 더 많으면 그만큼)까지, 일별 집계는 이력 파일에 최근 120일까지 남깁니다. → `deals.json` 에는 최근 30일 일별 집계만 실어 보냅니다.
- 관측 목록은 `deals.json` 에도 남기므로, 자동 갱신 워크플로가 `data/price_history.json` 을 커밋하지 못해도 다음 실행이 이력을 복원해 계속 누적합니다. (워크플로 `git add` 에 `data/price_history.json` 을 더하면 더 깔끔합니다 — 연결 권한상 워크플로 파일은 GitHub 웹에서 수정)
- 초기 이력은 저장소 커밋의 실제 `deals.json` 스냅샷에서 시드했습니다: `python scripts/seed_price_history.py`

### 쿨다운 안전장치

2026-11-14 15:02 KST 쿠팡 오픈 API `products/search` 가 403 경고(24시간 쿨다운, ~2026-11-15 23:58 KST)를 반환했습니다.
그래서 **2026-11-16 00:05 KST (`2026-11-15T15:05:00Z`) 이전에는 API를 호출하지 않습니다.**

1. 워크플로우 `쿨다운 안전장치` 스텝: 시각 이전이면 수집 스텝을 아예 실행하지 않음
2. `scripts/fetch_cafe_deals.py`: 동일 시각 이전이면 네트워크 요청 없이 `exit 0`

## 로컬 확인

```bash
python scripts/fetch_cafe_deals.py --dry-run      # 네트워크 없이 계획·안전장치만 확인
python scripts/fetch_cafe_deals.py --batch 1      # 1~4번째 카테고리만 수집 (1회 4호출)
python scripts/fetch_cafe_deals.py --wave 2 --max-requests 8  # 2차 보강 키워드 8개 수집
python scripts/fetch_cafe_deals.py --wave 3 --max-requests 8  # 3차(규격형) 키워드 — 0건 카테고리 보강용
python scripts/fetch_cafe_deals.py --only "설탕 3kg,토스키 시럽" --show-candidates  # 키워드별 필터 통과/탈락 상품을 한 줄씩 출력
python scripts/fetch_cafe_deals.py --only 원두,파우더  # 카테고리 지정 수집
python scripts/fetch_cafe_deals.py --prune-only    # API 없이 기존 데이터 정리(중복·무관 상품 제거)
python scripts/fetch_cafe_deals.py --recompute     # API 없이 가격 이력만으로 평균가·할인율 재계산
python scripts/seed_price_history.py               # 커밋 스냅샷에서 가격 이력 최초 시드(1회성)
python scripts/fetch_cafe_deals.py --assume-used 5   # 로그 밖에서 이미 쓴 호출 수 가산
python -m http.server 8000                        # 사이트 미리보기 (http://localhost:8000)
```

수집 결과는 `productId` 기준으로 기존 `deals.json` 과 병합됩니다(`--replace` 로 전체 교체).
호출 시각은 `data/api_usage.json` 에 기록되어 최근 1시간 9회를 넘으면 자동 중단합니다.
