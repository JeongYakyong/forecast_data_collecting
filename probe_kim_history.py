"""
probe_kim_history.py -- 두 가지 확인 전용 스크립트

목적:
  (A) 사용자가 보내준 새 좌표 (Gosan / Seongsan)가 실제 데이터를 반환하는지
  (B) 과거 발표 시각(tmfc)을 거꾸로 훑어 KIM API 의 retention 한계를 찾기

운영용 아님. collect_kim.py 작성 직전에 한 번 돌려 확인 후 폐기 가능.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

AUTH_KEY = os.getenv("KMA_API_KEY")
BASE_URL = "https://apihub.kma.go.kr/api/typ06/url/kim_grib_pt_tmfc.php"

# 사용자가 준 새 좌표 ((ny, nx) -> Y=ny, X=nx)
POINTS = [
    {"name": "West(Gosan)",    "x": 529, "y": 253, "lat": 33.4427, "lon": 126.1713},
    {"name": "East(Seongsan)", "x": 548, "y": 259, "lat": 33.5913, "lon": 126.7930},
]

# retention 탐색 — 오늘이 2026-05-24, 모든 tmfc 는 UTC 00 발표 기준
# (관행적으로 00/06/12/18 발표 중 00 만 시험)
# 45d 까지는 OK 확인됨 -> 그 사이 (45~90) 구간을 촘촘히 본다
RETENTION_PROBE_DAYS = [180, 200, 220, 240, 260, 270]   # 6m ~ 9m 사이를 좁힘


def call(params: dict) -> tuple[int, str]:
    """KIM API 호출. (HTTP status, body text) 반환."""
    p = dict(params)
    p["authKey"] = AUTH_KEY
    try:
        r = requests.get(BASE_URL, params=p, timeout=30)
        return r.status_code, r.text
    except Exception as e:
        return -1, f"REQUEST ERROR: {e}"


def count_data_rows(body: str) -> int:
    """응답 body 에서 데이터 행 수만 카운트 (헤더 # 라인 제외)."""
    return sum(
        1 for ln in body.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    )


def first_data_row(body: str) -> Optional[str]:
    for ln in body.splitlines():
        if ln.strip() and not ln.lstrip().startswith("#"):
            return ln
    return None


# ── (A) 새 좌표 확인 ─────────────────────────────────────────────────
def probe_new_coords():
    print("\n" + "=" * 78)
    print("  (A) 새 좌표 검증 -- 두 점 모두 실 데이터가 오는지 / 첫 값 출력")
    print("=" * 78)
    # 안정적으로 데이터가 있을 24h+ 이전 발표
    test_tmfc = "2026052300"
    for pt in POINTS:
        params = {
            "group": "KIMR", "nwp": "r030", "data": "U",
            "varn": "2002,2003,2022,7006,7007,3018,1074,1072",
            "tmfc": test_tmfc,
            "ef": "0,6,3",
            "X": pt["x"], "Y": pt["y"],
        }
        status, body = call(params)
        n_data = count_data_rows(body)
        print(f"\n  {pt['name']:<18}  X={pt['x']}  Y={pt['y']}  "
              f"(lat={pt['lat']:.4f}, lon={pt['lon']:.4f})")
        print(f"    tmfc={test_tmfc}  HTTP {status}  data_rows={n_data}")
        first = first_data_row(body)
        if first:
            print(f"    first row: {first}")
        else:
            print(f"    NO DATA. body head: {body[:300]!r}")


# ── (B) 과거 retention 한계 탐색 ────────────────────────────────────
def probe_retention():
    print("\n\n" + "=" * 78)
    print("  (B) Retention 탐색 -- 며칠 전까지 데이터 가져올 수 있나")
    print("=" * 78)
    pt = POINTS[0]  # Gosan 하나만으로 충분
    today = datetime(2026, 5, 24, tzinfo=timezone.utc)

    def tmfc_n_days_ago(n: int) -> str:
        # 제대로 된 날짜 산술 (이전 버전의 정수 빼기 버그를 대체)
        return (today - timedelta(days=n)).strftime("%Y%m%d") + "00"

    rows = []
    for n in RETENTION_PROBE_DAYS:
        tmfc = tmfc_n_days_ago(n)
        params = {
            "group": "KIMR", "nwp": "r030", "data": "U",
            "varn": "2002",   # 단일 varn 만으로도 retention 판단 충분
            "tmfc": tmfc,
            "ef": "0,3,3",    # 가벼운 응답
            "X": pt["x"], "Y": pt["y"],
        }
        status, body = call(params)
        n_data = count_data_rows(body)
        ok = n_data > 0
        # 응답 본문에 에러 메시지가 있으면 추출
        head = body[:120].replace("\n", " | ")
        rows.append((n, tmfc, status, n_data, ok, head))
        print(f"  -{n:3d}d  tmfc={tmfc}  HTTP {status}  data_rows={n_data:3d}  "
              f"{'OK' if ok else 'EMPTY'}")

    print("\n  Summary:")
    last_ok = max((n for n, *_, ok, _ in rows if ok), default=None)
    first_fail = min((n for n, *_, ok, _ in rows if not ok), default=None)
    if last_ok is not None:
        print(f"    Last successful: -{last_ok} days ago")
    if first_fail is not None:
        print(f"    First failure  : -{first_fail} days ago")
    if last_ok is not None and first_fail is not None:
        print(f"    Retention floor lies between -{last_ok}d and -{first_fail}d")


# ── (C) 시간 step 확인 -- 1h 가능한가, 3h 가 native 인가 ───────────
def probe_time_step():
    print("\n\n" + "=" * 78)
    print("  (C) 시간 간격 확인 -- ef=...,1 로 1h step 가능한가")
    print("=" * 78)
    pt = POINTS[0]
    tmfc = "2026052300"
    # ef=시작,종료,간격 -> 0~12h 를 1h step 으로 요청
    for step in (1, 2, 3, 6):
        params = {
            "group": "KIMR", "nwp": "r030", "data": "U",
            "varn": "2002",
            "tmfc": tmfc,
            "ef": f"0,12,{step}",
            "X": pt["x"], "Y": pt["y"],
        }
        status, body = call(params)
        rows = [
            ln for ln in body.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        # TMEF (2번째 토큰) 들만 모아서 unique 시간 step 확인
        tmefs = sorted({ln.split()[1] for ln in rows})
        print(f"\n  ef=0,12,{step}  HTTP {status}  data_rows={len(rows)}  "
              f"unique_tmefs={len(tmefs)}")
        print(f"    TMEFs returned: {tmefs}")


def main():
    if not AUTH_KEY:
        raise SystemExit("KMA_API_KEY is not set (check .env)")
    probe_new_coords()
    probe_time_step()
    probe_retention()


if __name__ == "__main__":
    main()
