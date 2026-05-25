"""
probe_kim_80m.py -- 80m wind 의 t=0 값이 항상 0 인지 검증

가설:
  (H1) KIM 모델 초기조건(analysis)에는 surface(10m) wind 만 있고,
       80m wind 는 모델이 PBL scheme 으로 spin-up 후 채우는 진단변수.
       → t=0(TMEF=TMFC) 의 80m 은 모든 발표/지점에서 항상 0.
  (H2) 특정 발표/지점/시즌에서만 0 -- 일부 데이터 문제.

(H1) 이 맞으면: 80m 은 t>0 에서 정상이므로 t=0 만 스킵하면 사용 가능.
(H2) 면 보다 복잡한 처리 필요.

테스트:
  - 두 지점(Gosan, Seongsan) × 4 발표(00/06/12/18 UTC) × 3 일치 (총 24 케이스)
  - 각 케이스에서 t=0 의 80mu / 80mv 값을 추출해 모두 0 인지 확인
  - 그리고 t+1h ~ t+3h 의 80m 값을 함께 출력 (t=0 만 0 이고 그 뒤로 정상인지)
"""

import os
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

AUTH_KEY = os.getenv("KMA_API_KEY")
BASE_URL = "https://apihub.kma.go.kr/api/typ06/url/kim_grib_pt_tmfc.php"

POINTS = [
    {"name": "West(Gosan)",    "x": 529, "y": 253},
    {"name": "East(Seongsan)", "x": 548, "y": 259},
]

# 발표 시각 (UTC) -- 안정적으로 데이터가 있는 -2 ~ -4 일 사이 사용
ISSUE_HOURS = [0, 6, 12, 18]
DAYS_BACK = [2, 3, 4]   # 최근 ~3일치


def parse_rows(body: str) -> list[tuple[str, int, int, float]]:
    """body -> [(TMEF, VARN, LEVEL, VALUE)] 리스트. 데이터 라인만."""
    out = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 5:
            continue
        try:
            tmef = parts[1]
            varn = int(parts[2])
            level = int(parts[3])
            value = float(parts[4])
            out.append((tmef, varn, level, value))
        except ValueError:
            continue
    return out


def call(tmfc: str, x: int, y: int) -> list[tuple[str, int, int, float]]:
    params = {
        "group": "KIMR", "nwp": "r030", "data": "U",
        "varn": "2002,2003",   # 80mu / 80mv 만 (10m 도 같이 옴)
        "tmfc": tmfc,
        "ef": "0,3,1",         # t=0,1,2,3 -- 짧게
        "X": x, "Y": y,
        "authKey": AUTH_KEY,
    }
    r = requests.get(BASE_URL, params=params, timeout=30)
    return parse_rows(r.text)


def main():
    if not AUTH_KEY:
        raise SystemExit("KMA_API_KEY not set")

    today = datetime(2026, 5, 24, tzinfo=timezone.utc)
    cases: list[tuple[str, dict]] = []
    for d in DAYS_BACK:
        for h in ISSUE_HOURS:
            base = today - timedelta(days=d)
            tmfc = base.strftime("%Y%m%d") + f"{h:02d}"
            for pt in POINTS:
                cases.append((tmfc, pt))

    print(f"{'tmfc':<12} {'point':<18} "
          f"{'80mu_t0':>10} {'80mu_t1':>10} {'80mu_t2':>10} {'80mu_t3':>10}  "
          f"{'10m_t0':>10}")
    print("-" * 100)

    t0_80m_zero_count = 0
    t0_80m_total = 0
    t1_80m_zero_count = 0

    for tmfc, pt in cases:
        rows = call(tmfc, pt["x"], pt["y"])
        # 인덱스화: (TMEF offset from TMFC) -> 80m u value
        # TMFC 와 TMEF 를 비교해 offset hour 계산
        u_by_offset_80 = {}
        u_by_offset_10 = {}
        for tmef, varn, level, value in rows:
            if varn != 2002:
                continue
            tmef_dt = datetime.strptime(tmef, "%Y%m%d%H")
            tmfc_dt = datetime.strptime(tmfc, "%Y%m%d%H")
            offset = int((tmef_dt - tmfc_dt).total_seconds() // 3600)
            if level == 80:
                u_by_offset_80[offset] = value
            elif level == 10:
                u_by_offset_10[offset] = value

        def fmt(v):
            return "n/a" if v is None else f"{v:+.3e}"

        v_80_0 = u_by_offset_80.get(0)
        v_80_1 = u_by_offset_80.get(1)
        v_80_2 = u_by_offset_80.get(2)
        v_80_3 = u_by_offset_80.get(3)
        v_10_0 = u_by_offset_10.get(0)

        print(f"{tmfc:<12} {pt['name']:<18} "
              f"{fmt(v_80_0):>10} {fmt(v_80_1):>10} {fmt(v_80_2):>10} {fmt(v_80_3):>10}  "
              f"{fmt(v_10_0):>10}")

        if v_80_0 is not None:
            t0_80m_total += 1
            if v_80_0 == 0.0:
                t0_80m_zero_count += 1
        if v_80_1 is not None and v_80_1 == 0.0:
            t1_80m_zero_count += 1

    print("\nSummary:")
    print(f"  t=0  80m_u == 0.0 : {t0_80m_zero_count} / {t0_80m_total} cases")
    print(f"  t=1h 80m_u == 0.0 : {t1_80m_zero_count} cases (should be ~0)")
    if t0_80m_total > 0 and t0_80m_zero_count == t0_80m_total:
        print("  -> H1 confirmed: 80m at t=0 is ALWAYS 0 (model spin-up artifact)")
        print("     Recommendation: keep 80m for t>0, store from offset=1 onward")


if __name__ == "__main__":
    main()
