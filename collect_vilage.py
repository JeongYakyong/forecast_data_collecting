"""
collect_vilage.py — 제주 풍력 예보 수집기 (KMA Village Forecast 2.0)

KMA Village Forecast 2.0 API에서 단일 발표(issue)를 받아 두 제주 지점
(West(Gosan) / East(Seongsan))의 행을 SQLite에 적재한다.

핵심 규칙
- 발표 시각(KST): 05, 14, 20, 23  → 1일 4회
- 발표 직후엔 데이터가 비어 있을 수 있어 30분 지연 후 수집 (cron 은 HH:30)
- 수집 윈도우 (day-aligned, base 시각별 다름):
    23시 발표 → [D+1 00:00, D+4 00:00)  = 3일치 (D+1·D+2·D+3, 모두 hourly)
    05/14/20  → [D+1 00:00, D+3 00:00)  = 2일치 (D+1·D+2 만, 모두 hourly)
  → 모든 row 가 1h step. D+1·D+2 는 다중 base 로 lead-time 비교 가능,
    D+3 는 전일자 23시 발표가 단독 커버 (긴 lead 의 reference forecast).
- 같은 발표를 다시 실행해도 PK 충돌로 INSERT OR IGNORE → 중복 없음
- 발표가 다르면 같은 fcst_datetime 도 모두 별도 행으로 보관
  (덮어쓰지 않음 — lead-time / skill EDA 용)

사용 예
    python collect_vilage.py                          # 가장 최근에 공개된 발표
    python collect_vilage.py --base 20260522 0500     # 특정 발표 (백필/테스트)
    python collect_vilage.py --db ./data/forecast.db  # SQLite 경로 지정
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

# ── 설정 ────────────────────────────────────────────────────────────────
KST = ZoneInfo("Asia/Seoul")

AUTH_KEY = os.getenv("KMA_API_KEY")

# 수집 지점. point_name 은 영문으로 저장한다.
# 사유: 윈도우 환경의 DB 브라우저 / CSV 내보내기 도구가 종종 CP949 로
# 디코딩하여 한글 값이 깨지는 문제가 있어, 저장값 자체를 ASCII 로 둔다.
# (소스 코드 주석은 UTF-8 그대로 한글 유지)
POINTS = [
    {"name": "West(Gosan)",    "nx": 46, "ny": 35},  # 서쪽(고산)
    {"name": "East(Seongsan)", "nx": 59, "ny": 38},  # 동쪽(성산)
]

# 수집 대상 발표 시각 (KST). 1일 4회.
# KMA 는 원래 02·05·08·11·14·17·20·23 (8회) 공개하지만, EDA 용도상
# 23/05/14/20 의 4 회만 사용한다 (대략 6h/9h/6h/3h 간격).
ISSUE_HOURS = (5, 14, 20, 23)

# 발표 직후 ~10–30 분간 응답이 비어 있을 수 있으므로 cutoff 를 둔다.
# cron 은 HH:30 에 실행한다.
PUBLISH_DELAY_MIN = 30

# 수집 윈도우 (day-aligned, base_time 별 다른 길이): [D+1 00:00, D+1+N 00:00)
#
# 규칙:
#   - D = base_date. 윈도우 길이 N은 발표 시각에 따라 다르게 한다.
#       23시 발표: N=3 → D+1, D+2, D+3 (72h)
#       05/14/20 발표: N=2 → D+1, D+2  (48h)
#   - 왜 발표마다 길이가 다른가:
#       KMA 의 1h→3h 전환이 day-aligned 으로 D+3 00:00 부근에서 일어나
#       05/14/20 발표의 D+3 부분은 3h step 이 섞인다. 23시 발표만 D+3 까지
#       완전 hourly 이므로, 23시만 D+3 를 포함하고 나머지는 D+2 까지로 끊는다.
#       → 모든 발표·모든 row 가 1h step 으로 통일된다.
#
# Lead-time 측면:
#   - D+1, D+2 의 각 시각은 4 종류 base 모두가 예측 → lead-time 비교 풍부.
#   - D+3 의 각 시각은 전일자 23시 발표 한 종류만 → lead 비교 없음.
#     (renewable 운영 측면에서 D+3 는 24~72h 의 긴 lead 영역으로 그대로 유용)
FORECAST_DAYS_BY_HOUR: dict[int, int] = {
    23: 3,
    5:  2,
    14: 2,
    20: 2,
}
# 알 수 없는 base 시각이 들어오면 보수적으로 2일.
FORECAST_DAYS_DEFAULT = 2

BASE_URL = (
    "https://apihub.kma.go.kr/api/typ02/openApi/"
    "VilageFcstInfoService_2.0/getVilageFcst"
)
PAGE_SIZE = 1000  # 한 페이지 최대 행수 (API 제한)
DEFAULT_DB = Path(__file__).parent / "data" / "forecast.db"


# ── 스키마 ──────────────────────────────────────────────────────────────
# PRIMARY KEY 조합 (base_datetime, fcst_datetime, point_name, category) 이
# 동일하면 1 행만 보존된다. 따라서:
#   - 같은 발표를 재실행해도 안전 (INSERT OR IGNORE)
#   - 서로 다른 발표(05H/14H/...)가 같은 fcst_datetime 을 예측해도
#     모두 별도 행으로 남아 lead-time 비교가 가능
SCHEMA = """
CREATE TABLE IF NOT EXISTS village_forecast (
    base_datetime  TEXT NOT NULL,   -- 'YYYY-MM-DD HH:MM' KST, 발표 시각
    fcst_datetime  TEXT NOT NULL,   -- 'YYYY-MM-DD HH:MM' KST, 예보 대상 시각
    point_name     TEXT NOT NULL,   -- 'West(Gosan)' / 'East(Seongsan)' (영문 고정)
    nx             INTEGER NOT NULL,
    ny             INTEGER NOT NULL,
    category       TEXT NOT NULL,   -- WSD/VEC/UUU/VVV/TMP/REH/PCP/SKY/PTY/...
    fcst_value     TEXT NOT NULL,   -- 비숫자도 있어 ('강수없음' 등) TEXT 로 둠
    collected_at   TEXT NOT NULL,   -- 적재 시점 (UTC ISO 8601)
    PRIMARY KEY (base_datetime, fcst_datetime, point_name, category)
);
CREATE INDEX IF NOT EXISTS idx_fcst_dt_cat ON village_forecast(fcst_datetime, category);
CREATE INDEX IF NOT EXISTS idx_base_dt     ON village_forecast(base_datetime);
"""


# ── 시간 계산 ──────────────────────────────────────────────────────────
def latest_published_base(now_kst: datetime) -> datetime:
    """공개 지연(PUBLISH_DELAY_MIN)을 감안해 가장 최근에 이용 가능한 발표 시각.

    어제·오늘 의 ISSUE_HOURS 후보를 모두 만들고, 그 중 'now - 30분' 이전인
    것들 중 최댓값을 고른다. (자정 직후 호출되어도 어제의 23시 발표가
    잡히도록 두 날짜를 모두 후보에 둠.)
    """
    cutoff = now_kst - timedelta(minutes=PUBLISH_DELAY_MIN)
    candidates = []
    for day_offset in (0, -1):
        day = (cutoff + timedelta(days=day_offset)).date()
        for h in ISSUE_HOURS:
            issue = datetime(day.year, day.month, day.day, h, tzinfo=KST)
            if issue <= cutoff:
                candidates.append(issue)
    return max(candidates)


def collection_window(base_dt_kst: datetime) -> tuple[datetime, datetime]:
    """day-aligned 수집 윈도우 [start, end) 반환.

    start = base_date 의 다음날 00:00 KST
    end   = start + N 일 (N 은 base 시각에 따라 다름)
    """
    next_midnight = (base_dt_kst + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    days = FORECAST_DAYS_BY_HOUR.get(base_dt_kst.hour, FORECAST_DAYS_DEFAULT)
    return next_midnight, next_midnight + timedelta(days=days)


# ── DB ─────────────────────────────────────────────────────────────────
def open_db(path: Path) -> sqlite3.Connection:
    """SQLite 파일 열기 — 디렉토리·테이블·인덱스가 없으면 자동 생성."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def insert_rows(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """행 일괄 적재. PK 충돌은 무시. 실제 추가된 행 수를 반환."""
    cur = conn.executemany(
        """
        INSERT OR IGNORE INTO village_forecast
            (base_datetime, fcst_datetime, point_name, nx, ny,
             category, fcst_value, collected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return cur.rowcount


# ── KMA API ────────────────────────────────────────────────────────────
def fetch_page(nx: int, ny: int, base_date: str, base_time: str, page: int) -> dict:
    """단일 페이지 요청 → JSON dict."""
    params = {
        "pageNo":    page,
        "numOfRows": PAGE_SIZE,
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


def fetch_all_items(nx: int, ny: int, base_date: str, base_time: str) -> list[dict]:
    """totalCount 만큼 페이지를 순회해 모든 item 을 모은다."""
    items: list[dict] = []
    page = 1
    while True:
        data = fetch_page(nx, ny, base_date, base_time, page)
        header = data.get("response", {}).get("header", {})
        body = data.get("response", {}).get("body", {})
        if header.get("resultCode") != "00":
            raise RuntimeError(
                f"KMA API error: {header.get('resultCode')} {header.get('resultMsg')}"
            )
        page_items = body.get("items", {}).get("item", []) or []
        items.extend(page_items)
        total = int(body.get("totalCount", 0) or 0)
        if len(items) >= total or not page_items:
            return items
        page += 1


# ── 수집 본체 ─────────────────────────────────────────────────────────
def collect(base_dt_kst: datetime, db_path: Path) -> int:
    """단일 발표(base_dt_kst)를 두 지점에서 수집해 SQLite 에 적재."""
    if not AUTH_KEY:
        sys.exit("KMA_API_KEY is not set (check .env)")

    base_date = base_dt_kst.strftime("%Y%m%d")
    base_time = base_dt_kst.strftime("%H%M")
    base_dt_str = base_dt_kst.strftime("%Y-%m-%d %H:%M")
    collected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_start, window_end = collection_window(base_dt_kst)
    window_str = (
        f"{window_start.strftime('%Y-%m-%d %H:%M')} ~ "
        f"{window_end.strftime('%Y-%m-%d %H:%M')}"
    )

    print(f"[{collected_at}] base={base_dt_str} KST  window=[{window_str})  db={db_path}")

    total_inserted = 0
    conn = open_db(db_path)
    try:
        for pt in POINTS:
            try:
                items = fetch_all_items(pt["nx"], pt["ny"], base_date, base_time)
            except Exception as e:
                print(f"  [ERROR] {pt['name']} fetch failed: {e}")
                continue

            rows = []
            dropped_outside = 0  # 윈도우 밖이라 버린 행 수 (디버깅용)
            for it in items:
                # KMA fcstDate/fcstTime 은 KST 기준 문자열
                fcst_dt_obj = datetime.strptime(
                    it["fcstDate"] + it["fcstTime"], "%Y%m%d%H%M"
                ).replace(tzinfo=KST)

                # day-aligned 윈도우 [D+1 00:00, D+4 00:00) 밖은 drop
                # → base_date 당일의 부분 데이터 (예: base=14시의 15~23시)
                #   및 +4일/+5일 의 3h step 구간이 모두 제거된다
                if not (window_start <= fcst_dt_obj < window_end):
                    dropped_outside += 1
                    continue

                rows.append((
                    base_dt_str,
                    fcst_dt_obj.strftime("%Y-%m-%d %H:%M"),
                    pt["name"],
                    pt["nx"],
                    pt["ny"],
                    it["category"],
                    str(it["fcstValue"]),
                    collected_at,
                ))
            inserted = insert_rows(conn, rows)
            total_inserted += inserted
            cats = sorted({r[5] for r in rows})
            print(
                f"  {pt['name']:<16}  fetched={len(items):4d}  "
                f"kept={len(rows):4d}  dropped(out-of-window)={dropped_outside:4d}  "
                f"inserted={inserted:4d}  duplicates={len(rows) - inserted:4d}  "
                f"categories={','.join(cats)}"
            )
    finally:
        conn.close()
    return total_inserted


# ── CLI ────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Collect one KMA Village Forecast issue (day-aligned 3-day window) into SQLite."
    )
    p.add_argument(
        "--base",
        nargs=2,
        metavar=("YYYYMMDD", "HHMM"),
        help="Specific base_date / base_time. Default: latest published issue.",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite path (default: {DEFAULT_DB})",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.base:
        base_dt = datetime.strptime(
            args.base[0] + args.base[1], "%Y%m%d%H%M"
        ).replace(tzinfo=KST)
        if base_dt.hour not in ISSUE_HOURS:
            # 본 프로젝트의 수집 대상이 아닌 발표 시각도 --base 로는 허용한다
            # (테스트/탐색 용도). 다만 경고는 남긴다.
            print(
                f"[WARN] base_time {base_dt.hour:02d} is not in target "
                f"ISSUE_HOURS {ISSUE_HOURS}"
            )
    else:
        base_dt = latest_published_base(datetime.now(tz=KST))
        
    
    collect(base_dt, args.db)


if __name__ == "__main__":
    main()
