"""
Probe script for KMA VilageFcst 2.0 wind data.
Run with:  python probe_vilage_wind.py
Edit NX, NY, BASE_DATE, BASE_TIME, AUTH_KEY before running.
"""

import requests
import json
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv

load_dotenv()

# ── 설정 (실행 전 확인) ─────────────────────────────────────────────────────
AUTH_KEY  = os.getenv("KMA_API_KEY", "YOUR_KEY_HERE")

POINTS = [
    {"name": "서쪽(고산)", "nx": 46, "ny": 35},
    {"name": "동쪽(성산)", "nx": 59, "ny": 38},
]

# base_date / base_time: 발표 시각 (02,05,08,11,14,17,20,23 중 하나)
BASE_DATE = "20260501"
BASE_TIME = "0500"
NUM_ROWS  = 1000   # 한 페이지 최대
# ────────────────────────────────────────────────────────────────────────────

BASE_URL = (
    "https://apihub.kma.go.kr/api/typ02/openApi/"
    "VilageFcstInfoService_2.0/getVilageFcst"
)

WIND_CATEGORIES = {"WSD", "VEC", "UUU", "VVV"}


def fetch_vilage(nx: int, ny: int, base_date: str, base_time: str) -> dict:
    params = {
        "pageNo":    1,
        "numOfRows": NUM_ROWS,
        "dataType":  "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx":        nx,
        "ny":        ny,
        "authKey":   AUTH_KEY,
    }
    resp = requests.get(BASE_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def probe_point(name: str, nx: int, ny: int):
    print(f"\n{'='*60}")
    print(f"  지점: {name}  (nx={nx}, ny={ny})")
    print(f"  base_date={BASE_DATE}  base_time={BASE_TIME}")
    print(f"{'='*60}")

    try:
        data = fetch_vilage(nx, ny, BASE_DATE, BASE_TIME)
    except Exception as e:
        print(f"  [ERROR] 요청 실패: {e}")
        return

    # ── header 확인
    header = data.get("response", {}).get("header", {})
    print(f"  resultCode : {header.get('resultCode')}")
    print(f"  resultMsg  : {header.get('resultMsg')}")

    body = data.get("response", {}).get("body", {})
    total = body.get("totalCount", "?")
    print(f"  totalCount : {total}")

    items = body.get("items", {}).get("item", [])
    if not items:
        print("  [WARN] items 없음 — 응답 전체 출력:")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
        return

    # ── 전체 카테고리 목록
    categories = sorted({it["category"] for it in items})
    print(f"\n  [카테고리 목록]  총 {len(categories)}개")
    print("  " + ", ".join(categories))

    # ── 바람 관련 rows
    wind_rows = [it for it in items if it["category"] in WIND_CATEGORIES]
    print(f"\n  [바람 카테고리 행수]  {len(wind_rows)}개")
    if not wind_rows:
        print("  [WARN] WSD/VEC/UUU/VVV 없음 — 실제 바람 카테고리 확인 필요")
        return

    # 시간 범위
    times = sorted({it["fcstDate"] + it["fcstTime"] for it in wind_rows})
    print(f"  예측 시간 범위: {times[0]} ~ {times[-1]}  (총 {len(times)} 스텝)")

    # ── 처음 8개 wind row 출력 (WSD 기준)
    wsd_rows = [it for it in wind_rows if it["category"] == "WSD"]
    print(f"\n  [WSD 풍속 샘플 — 첫 8개]")
    print(f"  {'fcstDate':<10} {'fcstTime':<8} {'category':<6} {'fcstValue'}")
    for row in wsd_rows[:8]:
        print(f"  {row['fcstDate']:<10} {row['fcstTime']:<8} {row['category']:<6} {row['fcstValue']}")

    # ── UUU/VVV 존재 여부
    has_uuu = any(it["category"] == "UUU" for it in wind_rows)
    has_vvv = any(it["category"] == "VVV" for it in wind_rows)
    has_vec = any(it["category"] == "VEC" for it in wind_rows)
    print(f"\n  UUU(동서 성분) : {'있음' if has_uuu else '없음'}")
    print(f"  VVV(남북 성분) : {'있음' if has_vvv else '없음'}")
    print(f"  VEC(풍향 도수) : {'있음' if has_vec else '없음'}")
    print(f"  WSD(풍속 m/s)  : {'있음' if wsd_rows else '없음'}")

    # ── 3시간 간격 전환 지점 감지
    if len(times) >= 2:
        gaps = []
        for i in range(1, len(times)):
            t0 = datetime.strptime(times[i-1], "%Y%m%d%H%M")
            t1 = datetime.strptime(times[i],   "%Y%m%d%H%M")
            gap = int((t1 - t0).total_seconds() / 3600)
            gaps.append(gap)
        unique_gaps = sorted(set(gaps))
        print(f"\n  시간 간격: {unique_gaps}시간")
        if 3 in unique_gaps:
            switch_idx = next(i for i, g in enumerate(gaps) if g == 3)
            switch_time = times[switch_idx + 1]
            print(f"  3시간 간격 전환 시점: {switch_time}")


if __name__ == "__main__":
    for pt in POINTS:
        probe_point(pt["name"], pt["nx"], pt["ny"])
    print("\n[완료]")
