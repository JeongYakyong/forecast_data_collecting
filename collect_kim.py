"""
collect_kim.py -- 제주 풍력 예보 수집기 (KMA KIM 지역, 단일면 data=U)

KMA API Hub 의 KIM(Korea Integrated Model) 지역 모델 단일면 격자점 자료를
받아 두 제주 지점(West(Gosan) / East(Seongsan))의 행을 SQLite 에 적재한다.

핵심 규칙
- 발표 시각(UTC): 00, 06, 12, 18  → 1일 4회 (KST 로는 09, 15, 21, 03)
- 발표 직후 데이터 가용까지 지연 ~10분 -- 안전마진 3시간
- 수집 윈도우 (day-aligned, KST 기준): [D+1 00 KST, D+3 00 KST), 2일치 hourly
- 응답에서 (varn, level) -> human label 로 매핑해 저장 (CATEGORY_MAP)
- 80m wind: t=0 에서 KIM 의 spin-up artifact 로 0.0 이 나오나, day-aligned
  윈도우가 항상 base+3h 이후부터 시작하므로 t=0 row 는 자연히 제외된다 (안심)
- INSERT OR IGNORE -- 동일 발표 재실행은 no-op (cron 재시도 안전)
- 발표가 다르면 같은 fcst_datetime 도 모두 별도 행으로 보관 (lead-time EDA)

KIM API 의 특별한 점:
- 응답이 JSON 이 아니라 plaintext (EUC-KR header + ASCII data lines).
  데이터 라인은 '# ' 로 시작하지 않으므로 그 필터만 거치면 됨.
- 멀티 varn 은 콤마(',')로만 동작. '+' / 공백은 빈 응답.
  -> 8 변수를 단일 호출에 묶어 가져옴 (호출 비용 최소화)
- varn=2002/2003 은 LEVEL=10(10m) / LEVEL=80(80m) 두 행이 함께 와서 4개
  카테고리(WIND_U_10M / WIND_U_80M / WIND_V_10M / WIND_V_80M) 가 됨.
  나머지 변수는 LEVEL=0 단일.
- Retention 이 매우 길다 -- 최소 180 일 전 발표도 응답함 (Village 의 ~1일 대비).
  -> 초기 backfill 로 수개월치 한 번에 적재 가능 (--backfill N).

사용 예
    python collect_kim.py                          # 가장 최근 2 발표 (safety 재수집)
    python collect_kim.py --base 20260523 12       # 특정 UTC 발표
    python collect_kim.py --backfill 150           # 최근 150 일치 일괄 backfill
    python collect_kim.py --db ./data/kim.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

# ── 설정 ────────────────────────────────────────────────────────────────
KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc

AUTH_KEY = os.getenv("KMA_API_KEY")

BASE_URL = "https://apihub.kma.go.kr/api/typ06/url/kim_grib_pt_tmfc.php"

# KIM 지역 모델 격자 X/Y (사용자 제공, ny/nx -> Y/X)
# - 33.4427N / 126.1713E (Gosan 인근)  -> ny=253, nx=529
# - 33.5913N / 126.7930E (Seongsan 인근) -> ny=259, nx=548
POINTS = [
    {"name": "West(Gosan)",    "x": 529, "y": 253},  # 서쪽(고산)
    {"name": "East(Seongsan)", "x": 548, "y": 259},  # 동쪽(성산)
]

# 발표 시각 (UTC). KST 로는 09 / 15 / 21 / 03(다음날).
ISSUE_HOURS_UTC = (0, 6, 12, 18)

# 발표 후 데이터 가용까지의 안전 마진. 관찰상 ~10분이면 충분하나 여유롭게 3h.
PUBLISH_DELAY_HOURS = 3

# day-aligned 수집 윈도우 길이 (D+1 00 KST 부터 N 일).
FORECAST_DAYS = 2

# 멀티 varn (콤마만 동작) -- 한 번의 HTTP 호출에 8개 변수를 모두 받음.
# (10 카테고리: 8 + WIND_U/V 의 LEVEL=10/80 분기 2개)
VARNS_PARAM = "2002,2003,2022,7006,7007,3018,1074,1072"

# (varn, level) -> human label.
# 80m wind 의 LEVEL=80 은 t=0 에서 spin-up 으로 0.0 이지만 day-aligned 윈도우가
# 항상 base+3h 이후부터 시작하므로 저장 단계에서 자연히 제외된다.
# 매핑되지 않은 (varn, level) 조합은 무시(다중 level 응답 변경 대비 안전망).
CATEGORY_MAP: dict[tuple[int, int], str] = {
    (2002, 10): "WIND_U_10M",
    (2002, 80): "WIND_U_80M",
    (2003, 10): "WIND_V_10M",
    (2003, 80): "WIND_V_80M",
    (2022, 0):  "GUST",
    (7006, 0):  "CAPE",
    (7007, 0):  "CINN",
    (3018, 0):  "HPBL",
    (1074, 0):  "TCOG",
    (1072, 0):  "TCOH",
}

# ef=시작,종료,간격 (h). 윈도우 최장 케이스(18 UTC 발표)가 +69h 까지 필요.
# 단순화를 위해 0,87,1 로 넉넉히 받고 KST 윈도우 필터로 잘라낸다.
EF_PARAM = "0,87,1"

DEFAULT_DB = Path(__file__).parent / "data" / "kim.db"


# ── 스키마 ──────────────────────────────────────────────────────────────
# Village/VSRT 와 동일한 컬럼 shape (nx/ny 만 x/y 로 명명). category 가 height 까지
# 인코딩하므로(WIND_U_10M / 80M) 별도 level 컬럼은 두지 않음.
SCHEMA = """
CREATE TABLE IF NOT EXISTS kim_forecast (
    base_datetime  TEXT NOT NULL,
    fcst_datetime  TEXT NOT NULL,
    point_name     TEXT NOT NULL,
    x              INTEGER NOT NULL,
    y              INTEGER NOT NULL,
    category       TEXT NOT NULL,
    fcst_value     TEXT NOT NULL,
    collected_at   TEXT NOT NULL,
    PRIMARY KEY (base_datetime, fcst_datetime, point_name, category)
);
CREATE INDEX IF NOT EXISTS idx_kim_fcst_dt_cat ON kim_forecast(fcst_datetime, category);
CREATE INDEX IF NOT EXISTS idx_kim_base_dt     ON kim_forecast(base_datetime);
"""


# ── 시간 계산 ──────────────────────────────────────────────────────────
def latest_published_base(now_kst: datetime) -> datetime:
    """공개 지연(PUBLISH_DELAY_HOURS)을 감안한 가장 최근 가용 발표 (UTC datetime)."""
    now_utc = now_kst.astimezone(UTC)
    cutoff = now_utc - timedelta(hours=PUBLISH_DELAY_HOURS)
    candidates = []
    for day_offset in (0, -1):
        day = (cutoff + timedelta(days=day_offset)).date()
        for h in ISSUE_HOURS_UTC:
            issue = datetime(day.year, day.month, day.day, h, tzinfo=UTC)
            if issue <= cutoff:
                candidates.append(issue)
    return max(candidates)


def previous_issue(base_utc: datetime) -> datetime:
    """주어진 발표 직전 발표 (6h 전, UTC)."""
    return base_utc - timedelta(hours=6)


def backfill_bases(days: int, now_kst: datetime) -> list[datetime]:
    """가장 최근 가용 발표부터 N 일치 거꾸로. 가장 오래된 것부터 적재하도록 reverse."""
    latest = latest_published_base(now_kst)
    cutoff = latest - timedelta(days=days)
    out: list[datetime] = []
    cur = latest
    while cur >= cutoff:
        out.append(cur)
        cur -= timedelta(hours=6)
    out.reverse()  # 오래된 것부터 최신 순 -> 진행상황 직관적
    return out


def collection_window(base_utc: datetime) -> tuple[datetime, datetime]:
    """day-aligned 수집 윈도우 [start, end) KST 기준.
    base 시각의 KST 다음 자정부터 FORECAST_DAYS 일.
    """
    base_kst = base_utc.astimezone(KST)
    next_midnight = (base_kst + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return next_midnight, next_midnight + timedelta(days=FORECAST_DAYS)


# ── DB ─────────────────────────────────────────────────────────────────
def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def insert_rows(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    cur = conn.executemany(
        """
        INSERT OR IGNORE INTO kim_forecast
            (base_datetime, fcst_datetime, point_name, x, y, category, fcst_value, collected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return cur.rowcount


# ── KMA API ────────────────────────────────────────────────────────────
def parse_response(body: str) -> list[tuple[str, int, int, str]]:
    """plaintext body -> [(TMEF, VARN, LEVEL, VALUE_STR)] 데이터 라인만 추출.
    헤더는 '#' 로 시작하는 행. VALUE 는 원문 그대로 (TEXT 저장).
    """
    out: list[tuple[str, int, int, str]] = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 5:
            continue
        try:
            varn = int(parts[2])
            level = int(parts[3])
        except ValueError:
            continue
        out.append((parts[1], varn, level, parts[4]))
    return out


def fetch_one(base_utc: datetime, x: int, y: int) -> list[tuple[str, int, int, str]]:
    """단일 (publish, point) 호출. 멀티 varn 으로 8 변수 한 번에 가져옴."""
    params = {
        "group": "KIMR",
        "nwp":   "r030",
        "data":  "U",
        "varn":  VARNS_PARAM,
        "tmfc":  base_utc.strftime("%Y%m%d%H"),
        "ef":    EF_PARAM,
        "X":     x,
        "Y":     y,
        "authKey": AUTH_KEY,
    }
    r = requests.get(BASE_URL, params=params, timeout=60)
    r.raise_for_status()
    return parse_response(r.text)


# ── 수집 본체 ─────────────────────────────────────────────────────────
def fetch_and_prepare(
    base_utc: datetime, point: dict, collected_at: str,
) -> tuple[list[tuple], int, int, int]:
    """단일 (publish, point) 호출 + 윈도우 필터 + insert-ready 행 생성.
    리턴: (rows, fetched_count, dropped_unknown, dropped_window).
    네트워크 I/O 만 하므로 워커 스레드에서 호출해도 안전 (DB 접근 없음).
    """
    items = fetch_one(base_utc, point["x"], point["y"])
    base_dt_str = base_utc.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    window_start, window_end = collection_window(base_utc)

    rows: list[tuple] = []
    dropped_unknown = 0
    dropped_window = 0
    for tmef, varn, level, value in items:
        cat = CATEGORY_MAP.get((varn, level))
        if cat is None:
            dropped_unknown += 1
            continue
        fcst_kst = datetime.strptime(tmef, "%Y%m%d%H").replace(tzinfo=UTC).astimezone(KST)
        if not (window_start <= fcst_kst < window_end):
            dropped_window += 1
            continue
        rows.append((
            base_dt_str,
            fcst_kst.strftime("%Y-%m-%d %H:%M"),
            point["name"],
            point["x"],
            point["y"],
            cat,
            value,
            collected_at,
        ))
    return rows, len(items), dropped_unknown, dropped_window


def collect(base_utc: datetime, db_path: Path) -> int:
    """단일 발표를 두 지점에서 수집해 SQLite 에 적재 (순차 처리)."""
    if not AUTH_KEY:
        sys.exit("KMA_API_KEY is not set (check .env)")

    base_kst = base_utc.astimezone(KST)
    base_dt_str = base_kst.strftime("%Y-%m-%d %H:%M")
    collected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_start, window_end = collection_window(base_utc)
    window_str = (
        f"{window_start.strftime('%Y-%m-%d %H:%M')} ~ "
        f"{window_end.strftime('%Y-%m-%d %H:%M')}"
    )

    print(
        f"[{collected_at}] base={base_dt_str} KST "
        f"(={base_utc.strftime('%Y%m%d%H')} UTC)  "
        f"window=[{window_str})  db={db_path}"
    )

    total_inserted = 0
    conn = open_db(db_path)
    try:
        for pt in POINTS:
            try:
                rows, n_fetched, n_unknown, n_window = fetch_and_prepare(
                    base_utc, pt, collected_at,
                )
            except Exception as e:
                print(f"  [ERROR] {pt['name']} fetch failed: {e}")
                continue
            inserted = insert_rows(conn, rows)
            total_inserted += inserted
            cats = sorted({r[5] for r in rows})
            print(
                f"  {pt['name']:<18}  fetched={n_fetched:4d}  kept={len(rows):4d}  "
                f"dropped(unknown)={n_unknown:3d}  "
                f"dropped(out-of-window)={n_window:4d}  "
                f"inserted={inserted:4d}  duplicates={len(rows) - inserted:4d}  "
                f"categories={','.join(cats)}"
            )
    finally:
        conn.close()
    return total_inserted


# ── Backfill (parallel + skip-existing) ────────────────────────────────
def existing_base_point_pairs(db_path: Path) -> set[tuple[str, str]]:
    """DB 에 이미 있는 (base_datetime, point_name) 쌍을 모두 수집.
    부분 적재된 발표는 (해당 지점만) 누락된 쪽이 다시 fetch 된다.
    """
    if not db_path.exists():
        return set()
    out: set[tuple[str, str]] = set()
    conn = sqlite3.connect(db_path)
    try:
        for row in conn.execute(
            "SELECT base_datetime, point_name FROM kim_forecast "
            "GROUP BY base_datetime, point_name"
        ):
            out.add((row[0], row[1]))
    finally:
        conn.close()
    return out


def run_backfill(
    bases: list[datetime], db_path: Path, workers: int,
) -> tuple[int, int, int]:
    """병렬 backfill. 이미 적재된 (base, point) 는 건너뛴다.
    리턴: (total_inserted, failed_pairs, skipped_pairs).
    """
    if not AUTH_KEY:
        sys.exit("KMA_API_KEY is not set (check .env)")

    existing = existing_base_point_pairs(db_path)
    all_tasks = [(b, pt) for b in bases for pt in POINTS]
    tasks = [
        (b, pt) for b, pt in all_tasks
        if (b.astimezone(KST).strftime("%Y-%m-%d %H:%M"), pt["name"]) not in existing
    ]
    skipped = len(all_tasks) - len(tasks)

    print(
        f"[backfill] {len(bases)} publishes -> {len(all_tasks)} (publish,point) pairs total\n"
        f"           {skipped} pairs already in DB, {len(tasks)} pairs to fetch with "
        f"{workers} worker(s)"
    )
    if not tasks:
        print("[backfill] nothing to do.")
        return 0, 0, skipped

    collected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = open_db(db_path)
    total_inserted = 0
    failed = 0
    done = 0
    started_at = time.time()
    PROGRESS_EVERY = max(1, len(tasks) // 40)  # 진행 라인 ~40 줄

    try:
        if workers <= 1:
            for b, pt in tasks:
                done += 1
                try:
                    rows, *_ = fetch_and_prepare(b, pt, collected_at)
                    total_inserted += insert_rows(conn, rows)
                except Exception as e:
                    failed += 1
                    print(f"  [ERROR] {b.strftime('%Y%m%d%H')} UTC {pt['name']}: {e}")
                if done % PROGRESS_EVERY == 0 or done == len(tasks):
                    elapsed = time.time() - started_at
                    rate = done / elapsed if elapsed else 0
                    eta = (len(tasks) - done) / rate if rate else 0
                    print(
                        f"  [{done:5d}/{len(tasks)}] inserted_so_far={total_inserted}  "
                        f"failed={failed}  rate={rate:.2f}/s  eta={eta/60:.1f}m"
                    )
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {
                    ex.submit(fetch_and_prepare, b, pt, collected_at): (b, pt)
                    for b, pt in tasks
                }
                for fut in as_completed(futures):
                    b, pt = futures[fut]
                    done += 1
                    try:
                        rows, *_ = fut.result()
                        total_inserted += insert_rows(conn, rows)
                    except Exception as e:
                        failed += 1
                        print(f"  [ERROR] {b.strftime('%Y%m%d%H')} UTC {pt['name']}: {e}")
                    if done % PROGRESS_EVERY == 0 or done == len(tasks):
                        elapsed = time.time() - started_at
                        rate = done / elapsed if elapsed else 0
                        eta = (len(tasks) - done) / rate if rate else 0
                        print(
                            f"  [{done:5d}/{len(tasks)}] inserted_so_far={total_inserted}  "
                            f"failed={failed}  rate={rate:.2f}/s  eta={eta/60:.1f}m"
                        )
    finally:
        conn.close()

    elapsed = time.time() - started_at
    print(
        f"\n[backfill] done in {elapsed/60:.1f}m. "
        f"inserted={total_inserted}  failed_pairs={failed}  skipped_pairs={skipped}"
    )
    return total_inserted, failed, skipped


# ── CLI ────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Collect KIM single-level (data=U) gridded-point forecast for two "
            "Jeju points into SQLite."
        )
    )
    p.add_argument(
        "--base",
        nargs=2,
        metavar=("YYYYMMDD", "HH_UTC"),
        help=(
            "Specific UTC publish (HH in 00/06/12/18). "
            "Default: latest 2 publishes (safety re-fetch)."
        ),
    )
    p.add_argument(
        "--backfill",
        type=int,
        metavar="N_DAYS",
        help=(
            "Backfill last N days of publishes (one-time bulk fetch). "
            "Mutually exclusive with --base. Skips (publish, point) pairs "
            "already in DB -- safe to re-run after interruption."
        ),
    )
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Parallel workers for --backfill (default 1 = sequential). "
            "4-8 recommended; KMA apihub seems to tolerate this without 429. "
            "Ignored without --backfill."
        ),
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
    if args.base and args.backfill is not None:
        sys.exit("--base and --backfill are mutually exclusive")

    now_kst = datetime.now(tz=KST)

    if args.base:
        base_utc = datetime.strptime(
            args.base[0] + args.base[1], "%Y%m%d%H",
        ).replace(tzinfo=UTC)
        if base_utc.hour not in ISSUE_HOURS_UTC:
            print(
                f"[WARN] base hour {base_utc.hour:02d} UTC not in "
                f"ISSUE_HOURS_UTC {ISSUE_HOURS_UTC}"
            )
        collect(base_utc, args.db)
        return

    if args.backfill is not None:
        bases = backfill_bases(args.backfill, now_kst)
        print(
            f"[backfill] {len(bases)} publishes "
            f"({bases[0].strftime('%Y%m%d%H')} UTC ~ "
            f"{bases[-1].strftime('%Y%m%d%H')} UTC, span ~{args.backfill} days)"
        )
        run_backfill(bases, args.db, max(1, args.workers))
        return

    # Default: latest 2 publishes (safety re-fetch of previous + latest).
    # 동일 발표 재수집은 INSERT OR IGNORE 로 무해. cron 누락시 자동 복구.
    latest = latest_published_base(now_kst)
    prev = previous_issue(latest)
    for b in (prev, latest):
        try:
            collect(b, args.db)
        except Exception as e:
            print(f"  [ERROR] {b.strftime('%Y%m%d%H')} UTC: {e}")


if __name__ == "__main__":
    main()
