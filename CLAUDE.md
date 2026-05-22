# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Collect KMA (Korean Meteorological Administration) forecast data on a recurring schedule, for renewable-generation (initially wind) forecasting at two Jeju points: **West(Gosan)** = 서쪽/고산 (nx=46, ny=35) and **East(Seongsan)** = 동쪽/성산 (nx=59, ny=38). Data is consumed downstream by a Streamlit app running on the same Ubuntu server.

## Layout

- `collect_vilage.py` — **production collector.** Fetches one KMA Village Forecast issue (both Jeju points) and inserts every returned row into SQLite. Idempotent, dynamic `base_date`/`base_time`, supports `--base YYYYMMDD HHMM` for backfill. This is what cron runs.
- `probe_vilage_wind.py` — original interactive probe. Kept as a reference for inspecting raw API payloads; not used in production.
- `변수명 GRIB 변수 변호 설명 단위.txt` — reference table of single-level GRIB variable codes for a **planned** second collector (KMA NWP/GRIB endpoint, not yet written). The Village Forecast API does not expose 80m hub-height wind, pressure, PBL height, or gust — those are the motivating variables for the GRIB follow-up.
- `등압면(pres).txt` — reference table of pressure-level (1000~50 hPa) GRIB variables. The user's primary target from this set is **`frcc` (Fraction of Cloud Cover)** at all 24 pressure levels — to be added as a third collector covering vertical cloud structure (complements Village Forecast's surface-only SKY).
- `requirements.txt` — `requests`, `python-dotenv`. Everything else is stdlib (Python 3.9+ for `zoneinfo`).
- `.env.example` — template for the required `KMA_API_KEY`. The real `.env` is git-ignored / not committed.
- `data/forecast.db` — SQLite output (default path; auto-created on first run).

## Storage model

**SQLite, single file at `data/forecast.db`.** Schema lives in `collect_vilage.py::SCHEMA`. One table:

```
village_forecast(base_datetime, fcst_datetime, point_name, nx, ny,
                 category, fcst_value, collected_at)
PRIMARY KEY (base_datetime, fcst_datetime, point_name, category)
```

Design rules:
- **Every issue is kept**, not overwritten. The 05H, 14H, 20H, 23H forecasts targeting the same `fcst_datetime` all coexist as separate rows distinguished by `base_datetime`. This is for lead-time / skill-score EDA — do not change this without asking.
- **Day-aligned, base-time-dependent window length.** Configured in `collect_vilage.py::FORECAST_DAYS_BY_HOUR`:
  - **23:00 publish → 3 days** (`[D+1 00:00, D+4 00:00)`) — covers D+1/D+2/D+3, all hourly
  - **05:00 / 14:00 / 20:00 publishes → 2 days** (`[D+1 00:00, D+3 00:00)`) — covers D+1/D+2 only
  This produces uniformly hourly rows across all publishes. D+1 and D+2 each receive forecasts from all four publishes (rich lead-time coverage for EDA); D+3 is covered only by the previous-day 23:00 publish (acts as the long-lead reference forecast).
- **Why the hybrid window**: empirical — KMA's 1h-step → 3h-step transition is **day-aligned** (around D+3 00:00), not base-relative. For 23:00 publish the window ends at D+4 00:00 and stays entirely inside the hourly zone. For 05/14/20 publishes, extending into D+3 would pick up 3h-step rows from D+3 03:00 onward; capping at D+3 00:00 cleanly avoids them. A simpler "+72h from base" or uniform 3-day window were both tried and rejected (they leaked 3h-step rows into the dataset).
- **Expected row count per publish per point** (× 14 categories, but TMN/TMX are once-per-day):
  - 23:00 publish: `72h × 12 + 3d × 2 = 870` rows
  - 05/14/20 publishes: `48h × 12 + 2d × 2 = 580` rows
  - Daily total per point: `870 + 3 × 580 = 2,610` rows. × 2 points × ~60 days ≈ **313k rows** for the full collection.
- Inserts use `INSERT OR IGNORE` so re-running the same issue is a no-op (idempotent — safe for cron retries).
- `fcst_value` is `TEXT` because some KMA categories return non-numeric strings (e.g. `PCP='강수없음'`). Cast at query time in EDA.
- No category filtering at write time — store everything KMA returns, filter downstream. Storage is cheap; recollection is not.
- **`point_name` is stored as ASCII English** (`West(Gosan)` / `East(Seongsan)`), not Korean. Windows DB browsers and CSV exporters frequently decode the .db as CP949 and mangle Korean values; keeping stored values ASCII sidesteps the problem. Korean labels stay in the source comments only.

## KMA API specifics

- Endpoint: `https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst`
- Auth: `authKey` query param from `KMA_API_KEY` env var.
- KMA publishes 8 issue slots (KST): 02, 05, 08, 11, 14, 17, 20, 23. **This project only collects 4 of them: 05, 14, 20, 23** (see `ISSUE_HOURS` in `collect_vilage.py`). The other 4 are intentionally skipped — irregular but acceptable spacing for EDA purposes.
- **Publish delay ≈ 30 min** — cron at `HH:30`. `collect_vilage.py::latest_published_base` enforces this when no `--base` is given.
- Response mixes 1h and 3h time-step rows; the schema stores them uniformly by `fcst_datetime` — downstream can detect the gap by sorting.
- **Historical retention is ~1 day.** Verified empirically: `--base` for yesterday returns data, 2-days-ago typically returns `error 03 NO_DATA`, 3-days-ago returns `error 10 최근 3일 간의 자료만 제공합니다` ("only last 3 days available" — but the practical floor for a given base hour is closer to D-1). Treat the backfill window as "yesterday only." Missing a cron run = data permanently lost after ~24h.
- Pagination is required when totalCount > 1000 (one issue × one point can exceed this); `fetch_all_items` walks pages until totalCount.

## Common commands

```powershell
# latest published issue → data/forecast.db
python collect_vilage.py

# backfill / test a specific issue
python collect_vilage.py --base 20260522 0500

# inspect raw API payload for one issue (no DB write)
python probe_vilage_wind.py
```

Install deps in a venv:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Server / deployment notes

- Target: existing Ubuntu/Debian server reachable via OpenSSH; the same host already runs the Streamlit app.
- Cron line shape (set up on server, not yet provisioned):
  ```
  30 5,14,20,23 * * * cd /path/to/repo && /path/to/.venv/bin/python collect_vilage.py >> /path/to/repo/data/cron.log 2>&1
  ```
- Server runs in UTC by default; the collector uses `zoneinfo("Asia/Seoul")` internally so server timezone does not affect base_time computation. **However**, crontab fires according to the *system* clock — if the server is UTC, schedule the cron at the UTC equivalents of HH:30 KST, or set the user's crontab `TZ=Asia/Seoul`.
- The user has SSH access but limited Linux/venv/crontab experience. When giving deployment instructions, be explicit and step-by-step, paste full commands rather than fragments, and prefer simple file-based flows over systemd/supervisor unless asked.

## Conventions

- **Language split**: Korean for source comments / docstrings (the user is Korean and reads code in Korean). **ASCII English only for**: (a) values written to the DB, (b) `print()` output that goes to cron logs. This split avoids Windows codepage / CP949 mojibake in stored data and in log files.
- `POINTS` in `collect_vilage.py` is the single source of truth for collection locations. Don't duplicate this list in other modules — import it.
- The probe script (`probe_vilage_wind.py`) is a reference only; do not delete it, but do not extend it for production — extend `collect_vilage.py` instead. Its internal Korean `POINTS` values are fine because the probe only prints to stdout, never writes to the DB.
