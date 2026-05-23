# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Collect KMA (Korean Meteorological Administration) forecast data on a recurring schedule, for renewable-generation (initially wind) forecasting at two Jeju points: **West(Gosan)** = 서쪽/고산 (nx=46, ny=35) and **East(Seongsan)** = 동쪽/성산 (nx=59, ny=38). Data is consumed downstream by a Streamlit app running on the same Ubuntu server.

## Layout

- `collect_vilage.py` — **production collector #1 (Village Forecast, 단기예보).** Fetches one KMA Village Forecast issue (both Jeju points) and inserts every returned row into SQLite. Idempotent, dynamic `base_date`/`base_time`, supports `--base YYYYMMDD HHMM` for backfill. Runs from cron 4×/day. 3-day horizon, hourly.
- `collect_vsrt.py` — **production collector #2 (Ultra-Short-Range Forecast, 초단기예보).** Same shape as the Village collector but: single point (Gosan only), endpoint `getUltraSrtFcst`, base time is every hour at HH:30, 6-hour forecast horizon, no windowing. Writes to a **separate DB** at `data/vsrt.db` (table `ultra_srt_forecast`). Runs from cron hourly, 24×/day.
- `probe_vilage_wind.py` — original interactive probe. Kept as a reference for inspecting raw API payloads; not used in production.
- `변수명 GRIB 변수 변호 설명 단위.txt` — reference table of single-level GRIB variable codes for a **planned** third collector (KMA NWP/GRIB endpoint, not yet written). Neither Village nor VSRT exposes 80m hub-height wind, pressure, PBL height, or gust — those are the motivating variables for the GRIB follow-up.
- `등압면(pres).txt` — reference table of pressure-level (1000~50 hPa) GRIB variables. The user's primary target from this set is **`frcc` (Fraction of Cloud Cover)** at all 24 pressure levels — to be added as a fourth collector covering vertical cloud structure (complements Village/VSRT's surface-only SKY).
- `requirements.txt` — `requests`, `python-dotenv`. Everything else is stdlib (Python 3.9+ for `zoneinfo`).
- `.env.example` — template for the required `KMA_API_KEY` (shared by both collectors).
- `data/forecast.db` — Village Forecast SQLite output.
- `data/vsrt.db` — Ultra-Short-Range Forecast SQLite output (separate file by design — different temporal cadence, different table shape would mix poorly).

## Storage model

**Two SQLite files, one per collector.**

`data/forecast.db` — Village Forecast (schema in `collect_vilage.py::SCHEMA`):

```
village_forecast(base_datetime, fcst_datetime, point_name, nx, ny,
                 category, fcst_value, collected_at)
PRIMARY KEY (base_datetime, fcst_datetime, point_name, category)
```

`data/vsrt.db` — Ultra-Short-Range Forecast (schema in `collect_vsrt.py::SCHEMA`):

```
ultra_srt_forecast(base_datetime, fcst_datetime, point_name, nx, ny,
                   category, fcst_value, collected_at)
PRIMARY KEY (base_datetime, fcst_datetime, point_name, category)
```

Both share the same column shape and PK design — only the table name and DB file differ. Kept separate because the cadences differ ~24× (VSRT is hourly, Village is 4×/day) and the per-row meaning differs (VSRT is nowcast/0~6h, Village is short-range/3-day).

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

Both collectors share `KMA_API_KEY` and the same `apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/` base. Only the endpoint name and timing differ.

### Village Forecast (`getVilageFcst`, 단기예보)
- KMA publishes 8 issue slots (KST): 02, 05, 08, 11, 14, 17, 20, 23. **This project only collects 4 of them: 05, 14, 20, 23** (see `ISSUE_HOURS` in `collect_vilage.py`). The other 4 are intentionally skipped — irregular but acceptable spacing for EDA purposes.
- **Publish delay ≈ 30 min** — cron at `HH:30`. `collect_vilage.py::latest_published_base` enforces this when no `--base` is given.
- Response mixes 1h and 3h time-step rows; the schema stores them uniformly by `fcst_datetime` — downstream can detect the gap by sorting.
- **Historical retention is ~1 day.** Verified empirically: `--base` for yesterday returns data, 2-days-ago typically returns `error 03 NO_DATA`, 3-days-ago returns `error 10 최근 3일 간의 자료만 제공합니다` ("only last 3 days available" — but the practical floor for a given base hour is closer to D-1). Treat the backfill window as "yesterday only." Missing a cron run = data permanently lost after ~24h.
- Pagination is required when totalCount > 1000 (one issue × one point can exceed this); `fetch_all_items` walks pages until totalCount.

### Ultra-Short-Range Forecast (`getUltraSrtFcst`, 초단기예보)
- Single issue cadence: **every hour at HH:30**. 24 issues/day (no skipping).
- **Publish delay ≈ 10–15 min** — cron at `HH:45` (HH+15 after the HH:30 publish). `collect_vsrt.py::latest_published_base` enforces this when no `--base` is given.
- Categories: **T1H, UUU, VVV, VEC, WSD, SKY, LGT, PTY, RN1, REH** (10 total). Overlaps with Village on UUU/VVV/VEC/WSD/SKY/PTY/REH; new categories vs Village are **T1H (1h temperature, °C)**, **LGT (lightning)**, **RN1 (1h rainfall amount)** — all nowcast-flavored.
- Forecast horizon: base + 0:00 ~ base + 6:00, **all hourly, 6 valid times**. No 1h/3h mixing → **no windowing needed**, the collector keeps every returned row.
- Response shape: identical JSON layout to Village (`response.body.items.item[]` with `baseDate/baseTime/fcstDate/fcstTime/category/fcstValue/nx/ny`). Re-uses the same parsing scaffold.
- Per-issue row count: 60 rows (10 categories × 6 valid times × 1 point). Daily: ~1,440 rows. Annual: ~525k rows — still tiny for SQLite.
- Retention assumed similar to Village (~1 day); not yet stress-tested. Treat backfill window as "today and yesterday only."

## Common commands

```powershell
# Village Forecast — latest issue → data/forecast.db
python collect_vilage.py
python collect_vilage.py --base 20260522 0500   # backfill / test

# Ultra-Short-Range Forecast — latest issue → data/vsrt.db
python collect_vsrt.py
python collect_vsrt.py --base 20260522 2330     # backfill / test (HHMM must end with 30)

# inspect raw API payload for one Village issue (no DB write)
python probe_vilage_wind.py
```

Install deps in a venv:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Server / deployment notes

**Status: live since 2026-05-22.** Running on user's Linux Mint host (KST system timezone), reachable via Tailscale. Repo lives at `github.com/JeongYakyong/forecast_data_collecting`; the server keeps a clone at `~/forecast_data_collecting` with a venv at `~/forecast_data_collecting/.venv`.

Active crontab entries (one line per collector — both append to the same `cron.log`):
```
30 5,14,20,23 * * * cd /home/kimjourvanne/forecast_data_collecting && /home/kimjourvanne/forecast_data_collecting/.venv/bin/python collect_vilage.py >> /home/kimjourvanne/forecast_data_collecting/data/cron.log 2>&1
45 * * * *          cd /home/kimjourvanne/forecast_data_collecting && /home/kimjourvanne/forecast_data_collecting/.venv/bin/python collect_vsrt.py   >> /home/kimjourvanne/forecast_data_collecting/data/cron.log 2>&1
```

Operational notes:
- Server timezone is already KST → no `TZ=` env var needed in crontab.
- Calls the venv's Python by absolute path (no `source activate` needed in cron).
- Both stdout and stderr are appended to `data/cron.log` (gitignored). Tail this to see run history.
- Updates: edit locally → `git push` → `ssh` in → `git pull` → done. Only re-run `pip install -r requirements.txt` when deps change.
- The user has SSH access but limited Linux/venv/crontab experience. When giving server-side instructions, be explicit and step-by-step, paste full commands, and prefer simple file-based flows over systemd/supervisor unless asked.

## Conventions

- **Language split**: Korean for source comments / docstrings (the user is Korean and reads code in Korean). **ASCII English only for**: (a) values written to the DB, (b) `print()` output that goes to cron logs. This split avoids Windows codepage / CP949 mojibake in stored data and in log files.
- `POINTS` in `collect_vilage.py` is the single source of truth for collection locations. Don't duplicate this list in other modules — import it.
- The probe script (`probe_vilage_wind.py`) is a reference only; do not delete it, but do not extend it for production — extend `collect_vilage.py` instead. Its internal Korean `POINTS` values are fine because the probe only prints to stdout, never writes to the DB.
