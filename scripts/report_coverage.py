"""Read-only coverage analysis; writes a report, never calls the price API."""
import json
from datetime import datetime, timezone

import fetch_cafe_deals as core
import refresh_existing as refresh
from refresh_metrics import coverage_report


def main():
    report = coverage_report(core.load_previous().get("deals", []), refresh.load_state(), datetime.now(timezone.utc))
    path = core.ROOT / "data" / "coverage_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("### 일일 가격 확인률\n")
    print(report["note"])
    print("\n측정 시작: " + str(report["measurementStartedAt"] or "다음 수집부터"))
    print("\n| 한국 날짜 | 확인 상품 | 확인률 | 측정 검색 | 검색당 고유 상품 | 집계 |")
    print("|---|---:|---:|---:|---:|---|")
    for row in report["daily"]:
        print(f'| {row["date"]} | {row["confirmed"]}/{row["total"]} | {row["percent"]}% | {row["requests"]} | {row["uniquePerRequest"]} | {"부분" if row["partial"] else "종일"} |')
    print(f'\n72시간 이상 미확인 또는 반복 누락: {len(report["missing72HoursOrRepeated"])}개')
    print("\n상세 상품·검색별 성과는 coverage-report 아티팩트의 JSON에서 확인합니다.")


if __name__ == "__main__":
    main()
