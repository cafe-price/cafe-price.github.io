# 가격 갱신 작업

사용자의 2026-11-25 요청: 기존 상품 가격의 재확인을 우선하고, 상품별 마지막 확인 시각과 오래된 가격을 구분한다.

- 매시간 예약은 GitHub Actions로 이전한다. 저장소 변수 `CAFE_PRICE_SCHEDULER_ENABLED=true`가 설정되면 피클/수동 정기 수집은 실행하지 않는다. 키 등록과 피클 예약 중지를 확인한 뒤에만 이 변수를 활성화한다.
- 정기 수집은 최신 main을 가져온 뒤 `python scripts/fetch_cafe_deals.py --refresh-existing --max-requests 8`을 한 번 실행한다.
- 과거 notes.md의 신규 키워드 확장·미발견 상품 영구 제외 방침은 정기 수집에 적용하지 않는다. `--wave`, `--only`, `--discover`, `--replace`는 별도로 신규 수집을 요청받았을 때만 사용한다.
- `deals.json`, `data/price_history.json`, `data/refresh_state.json`, `data/api_usage.json` 변경분을 함께 커밋·푸시한다. 사용기록은 실패한 실행에서도 보존한다. 비밀키는 커밋하거나 출력하지 않는다.
- 정기·수동 작업은 동시에 실행하지 않는다. 동일 키를 쓰는 다른 작업의 검색 호출도 시간당 한도에 합산하며, 저장소 밖의 호출은 `--assume-used N`으로 반영한다. 다른 스케줄러를 중복 활성화하지 않는다.
- 검색 미발견·다른 옵션·로켓배송 이탈·API 오류를 품절로 단정하거나 가격을 추정하지 않는다. 실제 동일 옵션의 가격을 확인한 경우에만 `lastCheckedAt`과 가격 이력을 갱신한다.
- 파일 생성·재계산 시간을 상품 확인 시각으로 쓰지 않는다. 오래된 이력을 오늘 가격으로 복사하지 않는다.
- 보고는 API 호출 횟수, 기존 상품 가격 관측 수, 최근 24시간 확인 비율, 재확인 필요 수로 한다. 전 상품 실시간 갱신을 보장하지 않는다.
- 2026-11-25 일일 확인률 측정: 상품은 줄이지 않는다. 한국시간 오늘 확인한 상품은 중복 목표에서 제외하고, 공동 검색어 탐색과 개별 검색을 번갈아 실행한다. 같은 검색어는 정상 응답 후 당일 재사용하지 않는다. 정확한 판매 옵션 매칭은 유지한다.
- 검색별 호출·관측·오늘 최초 확인 ID는 `refresh_state.json`의 `metrics`에 31일간 보존한다. 첫날·오늘은 부분 집계이며, 측정 이전 호출 수는 추정하지 않는다. `python scripts/report_coverage.py`로 API 호출 없이 날짜별 확인률, 검색 효율, 72시간 이상 미확인 및 2개 이상 날짜에서 실패한 상품을 분석한다. `data/coverage_report.json`도 함께 커밋한다.
- 검증: `python -m unittest discover -s tests -v`, `node tests/frontend.test.cjs`, `python scripts/fetch_cafe_deals.py --refresh-existing --dry-run`.
