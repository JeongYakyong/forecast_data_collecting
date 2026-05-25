"""
probe_kim.py -- KMA API Hub KIM 단일면 격자점 자료 응답 탐색용 probe

엔드포인트:
    https://apihub.kma.go.kr/api/typ06/url/kim_grib_pt_tmfc.php

목적 (collect_kim.py 작성 전 사전 조사):
    1) 응답 포맷 확인 (JSON / CSV / 공백구분 텍스트 ?)
    2) 단일 varn 호출이 정상 동작하는지
    3) 다중 varn (varn=2002,2003,...) 한 번에 호출 가능한가
    4) varn 생략 시 전체 단일면 변수가 한 번에 오는가
    5) ef=0,87,3 87h horizon 응답 길이
    6) 사용자가 원하는 변수 8종(80mu/80mv/gust/cape/cinn/hpbl/tcog/tcoh)
       모두 실제로 존재하고 응답이 오는지

운영용 아님 -- 인스펙션 전용. (probe_vilage_wind.py 와 같은 위치)
좌표는 예시 URL 의 X=300, Y=200 그대로. (실제 제주 KIM 격자 X/Y 는 별도 확정)
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

AUTH_KEY = os.getenv("KMA_API_KEY", "YOUR_KEY_HERE")

BASE_URL = "https://apihub.kma.go.kr/api/typ06/url/kim_grib_pt_tmfc.php"

# 임시 좌표 (예시 URL 의 그것). 사용자가 제주 좌표 보내주면 교체 예정.
TEST_X = 300
TEST_Y = 200

# 24h+ 이전이라 안정적으로 데이터 존재 기대.
# 2026-05-23 12 UTC = 2026-05-23 21 KST  (오늘이 2026-05-24)
TEST_TMFC = "2026052312"

# 사용자가 수집 대상으로 지목한 단일면 변수 (data=U)
VARS_TO_TEST = [
    ("80mu", 2002),    # U-Component of Wind (80m hub-height)
    ("80mv", 2003),    # V-Component of Wind (80m hub-height)
    ("gust", 2022),    # Wind Speed (Gust)
    ("cape", 7006),    # Convective Available Potential Energy
    ("cinn", 7007),    # Convective Inhibition
    ("hpbl", 3018),    # Planetary Boundary Layer Height
    ("tcog", 1074),    # Total Column Integrated Graupel
    ("tcoh", 1072),    # Total Column Integrated Hail
]

# 다중 varn 시도 후보 (구분자 다양화)
MULTI_VARN_TRIES = [
    "2002,2003,2022",   # comma
    "2002+2003+2022",   # plus
    "2002 2003 2022",   # space
]


def call(params: dict, label: str, preview_lines: int = 80) -> str:
    print(f"\n{'=' * 78}")
    print(f"  {label}")
    print(f"  params={params}")
    print(f"{'=' * 78}")
    p = dict(params)
    p["authKey"] = AUTH_KEY
    try:
        r = requests.get(BASE_URL, params=p, timeout=30)
    except Exception as e:
        print(f"  [ERROR] request failed: {e}")
        return ""

    print(f"  HTTP {r.status_code}")
    print(f"  Content-Type: {r.headers.get('Content-Type')}")
    text = r.text
    print(f"  Body length: {len(text)} chars")
    lines = text.splitlines()
    print(f"  Body lines : {len(lines)}")
    print(f"  ---- HEAD (first {preview_lines} lines) ----")
    for ln in lines[:preview_lines]:
        print(f"  | {ln}")
    if len(lines) > preview_lines:
        print(f"  ... ({len(lines) - preview_lines} more lines)")
        print(f"  ---- TAIL (last 5 lines) ----")
        for ln in lines[-5:]:
            print(f"  | {ln}")
    return text


def main():
    if not AUTH_KEY or AUTH_KEY == "YOUR_KEY_HERE":
        raise SystemExit("KMA_API_KEY is not set (check .env)")

    common = {
        "group": "KIMR",
        "nwp": "r030",
        "data": "U",
        "tmfc": TEST_TMFC,
        "X": TEST_X,
        "Y": TEST_Y,
    }

    # 1) help=1 단독 호출 -- 응답에 변수 설명/단위 헤더가 붙는지 확인
    call(
        {**common, "varn": 2002, "ef": "0,12,3", "help": 1},
        "[1] single varn=2002 (80mu), help=1, ef=0,12,3 -- basic shape + header",
        preview_lines=120,
    )

    # 2) help=0 -- 순수 데이터 본문만
    call(
        {**common, "varn": 2002, "ef": "0,12,3"},
        "[2] same call, no help -- pure data body shape",
    )

    # 3) 다중 varn 시도들 -- 어떤 구분자가 통하나
    for sep in MULTI_VARN_TRIES:
        call(
            {**common, "varn": sep, "ef": "0,12,3", "help": 1},
            f"[3] multi-varn try: varn='{sep}'",
        )

    # 4) varn 생략 -- 전체 단일면 변수 한 번에?
    call(
        {**common, "ef": "0,12,3", "help": 1},
        "[4] varn omitted -- does it return ALL single-level vars at once?",
    )

    # 5) ef=0,87,3 -- 87h horizon 응답
    call(
        {**common, "varn": 2002, "ef": "0,87,3"},
        "[5] ef=0,87,3 -- does the 87h horizon work?",
        preview_lines=10,
    )

    # 6) 사용자가 원하는 모든 변수 -- 각각 단일 호출로 존재/값 확인
    print(f"\n\n{'#' * 78}")
    print("# [6] 변수별 단독 호출 -- 각 변수가 응답에 잡히는지 / 첫 값 확인")
    print(f"{'#' * 78}")
    for name, varn in VARS_TO_TEST:
        call(
            {**common, "varn": varn, "ef": "0,6,3", "help": 1},
            f"  variable '{name}' (varn={varn})",
            preview_lines=20,
        )


if __name__ == "__main__":
    main()
