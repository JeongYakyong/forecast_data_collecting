"""
collect_vsrt.py — 초단기예보 수집기 (KMA Ultra-Short-Range Forecast)

KMA UltraSrtFcst API 에서 단일 발표(issue)를 받아 Gosan(고산) 지점의 행을
SQLite 에 적재한다. collect_vilage.py 의 형제 스크립트로, 동일한 KMA_API_KEY
와 동일한 응답 형식(JSON)을 사용하지만 다음 점이 다르다:

- 엔드포인트: getUltraSrtFcst (단기예보가 아닌 초단기예보)
- 발표: 매 시 HH:30 (24회/일, day & night)
- 예보 horizon: base + 0:30 ~ base + 6:00, 1h step 6개
- 지점: West(Gosan) 1곳만 (사용자 요청)
- DB 파일: data/vsrt.db (Village 와 별도)

핵심 규칙
- 발표 직후 ~10~15 분간 응답이 비어 있을 수 있어 15분 지연 → cron HH:45
- 예보 6시간 모두 보관 (윈도우 필터 없음 — Village 와 달리 1h/3h 혼재가 없음)
- 같은 발표를 재실행해도 PK 충돌 → INSERT OR IGNORE 로 중복 방지
- 서로 다른 발표가 같은 fcst_datetime 을 예측해도 별도 행으로 보관
  (lead-time / nowcast skill EDA 용)

사용 예
    python collect_vsrt.py                          # 가장 최근에 공개된 발표
    python collect_vsrt.py --base 20260523 1030     # 특정 발표 (백필/테스트)
    python collect_vsrt.py --db ./data/vsrt.db      # SQLite 경로 지정
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

# 수집 지점: 고산만. (사용자 요청 — 초단기예보는 Gosan 단일 지점.)
# point_name 은 영문 ASCII 로 저장 (Village 와 동일 정책 — CP949 mojibake 회피).
POINTS = [
    {"name": "West(Gosan)", "nx": 46, "ny": 35},  # 서쪽(고산)
]

# 초단기예보 발표는 매 시 HH:30. 1일 24회.
ISSUE_MINUTE = 30

# 발표 후 응답 가용까지 약 10~15분. 안전을 위해 15분.
# cron 은 HH:45 에 실행한다.
PUBLISH_DELAY_MIN = 15

BASE_URL = (
    "https://apihub.kma.go.kr/api/typ02/openApi/"
    "VilageFcstInfoService_2.0/getUltraSrtFcst"
)
PAGE_SIZE = 1000  # 단일 지점·단일 발표는 60행 → 1페이지로 충분
DEFAULT_DB = Path(__file__).parent / "data" / "vsrt.db"


# ── 스키마 ──────────────────────────────────────────────────────────────
# Village 와 같은 PK 조합. 테이블만 별도(ultra_srt_forecast).
SCHEMA = """
CREATE TABLE IF NOT EXISTS ultra_srt_forecast (
    base_datetime  TEXT NOT NULL,   -- 'YYYY-MM-DD HH:MM' KST, 발표 시각 (분단위: 30)
    fcst_datetime  TEXT NOT NULL,   -- 'YYYY-MM-DD HH:MM' KST, 예보 대상 시각
    point_name     TEXT NOT NULL,   -- 'West(Gosan)' (영문 고정)
    nx             INTEGER NOT NULL,
    ny             INTEGER NOT NULL,
    category       TEXT NOT NULL,   -- T1H/UUU/VVV/VEC/WSD/SKY/LGT/PTY/RN1/REH
    fcst_value     TEXT NOT NULL,   -- 비숫자도 있어 ('강수없음' 등) TEXT 로 둠
    collected_at   TEXT NOT NULL,   -- 적재 시점 (UTC ISO 8601)
    PRIMARY KEY (base_datetime, fcst_datetime, point_name, category)
);
CREATE INDEX IF NOT EXISTS idx_vsrt_fcst_dt_cat ON ultra_srt_forecast(fcst_datetime, category);
CREATE INDEX IF NOT EXISTS idx_vsrt_base_dt     ON ultra_srt_forecast(base_datetime);
"""


# ── 시간 계산 ──────────────────────────────────────────────────────────
def latest_published_base(now_kst: datetime) -> datetime:
    """공개 지연(PUBLISH_DELAY_MIN)을 감안한, 가장 최근 이용 가능 발표 시각.

    매 시 HH:30 발표 중에서 'now - 15분' 이하의 가장 큰 시각.
    """
    cutoff = now_kst - timedelta(minutes=PUBLISH_DELAY_MIN)
    candidate = cutoff.replace(minute=ISSUE_MINUTE, second=0, microsecond=0)
    if candidate > cutoff:
        candidate -= timedelta(hours=1)
    return candidate


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
        INSERT OR IGNORE INTO ultra_srt_forecast
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
    """단일 발표(base_dt_kst)를 Gosan 지점에서 수집해 SQLite 에 적재."""
    if not AUTH_KEY:
        sys.exit("KMA_API_KEY is not set (check .env)")

    base_date = base_dt_kst.strftime("%Y%m%d")
    base_time = base_dt_kst.strftime("%H%M")
    base_dt_str = base_dt_kst.strftime("%Y-%m-%d %H:%M")
    collected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"[{collected_at}] base={base_dt_str} KST  db={db_path}")

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
            for it in items:
                # KMA fcstDate/fcstTime 은 KST 기준 문자열
                fcst_dt_obj = datetime.strptime(
                    it["fcstDate"] + it["fcstTime"], "%Y%m%d%H%M"
                ).replace(tzinfo=KST)
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
                f"inserted={inserted:4d}  duplicates={len(rows) - inserted:4d}  "
                f"categories={','.join(cats)}"
            )
    finally:
        conn.close()
    return total_inserted


# ── CLI ────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Collect one KMA Ultra-Short-Range Forecast issue (Gosan, 6h horizon) into SQLite."
    )
    p.add_argument(
        "--base",
        nargs=2,
        metavar=("YYYYMMDD", "HHMM"),
        help="Specific base_date / base_time (HHMM must end with 30). Default: latest published issue.",
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
        if base_dt.minute != ISSUE_MINUTE:
            print(
                f"[WARN] base_time minute {base_dt.minute:02d} is not {ISSUE_MINUTE:02d} "
                f"(UltraSrtFcst publishes only at HH:30)"
            )
    else:
        base_dt = latest_published_base(datetime.now(tz=KST))

    collect(base_dt, args.db)


if __name__ == "__main__":
    main()
