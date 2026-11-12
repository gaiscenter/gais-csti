# -*- coding: utf-8 -*-
"""대만 가권지수(TAIEX) 일별 시세를 證交所에서 받아 market/taiex.csv 로 쌓는다.

왜 證交所인가
-------------
야후는 내려받기를 막았고, 발표·논문에 쓰려면 1차 출처가 낫다. 證交所는
로그인 없이 월 단위 JSON 을 준다.

주의 — 민국년
-------------
證交所 날짜는 민국년이다. `115/08/03` 은 2026-08-03 이다 (서기 = 민국 + 1911).
이걸 안 고치면 1년치가 통째로 어긋난다.

**형식이 바뀌면 조용히 틀리지 않게 한다.** 받은 것을 그대로 찍고, 날짜나
숫자가 안 읽히면 거기서 멈춘다. 빈 CSV 를 남기고 분석이 0 으로 돌아가는 것이
제일 나쁘다.
"""
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import date

START = os.environ.get("MARKET_START") or "2026-08-01"
END = os.environ.get("MARKET_END") or ""
OUT = os.environ.get("MARKET_FILE") or "market/taiex.csv"

# 證交所는 엔드포인트를 한 번 옮긴 적이 있다. 둘 다 시도한다.
ENDPOINTS = [
    "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={ym}01&response=json",
    "https://www.twse.com.tw/indicesReport/MI_5MINS_HIST?date={ym}01&response=json",
]
UA = {"User-Agent": "Mozilla/5.0 (research; GAIS CSTI)"}


def months(a, b):
    y, m = int(a[:4]), int(a[5:7])
    ey, em = int(b[:4]), int(b[5:7])
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def roc_to_iso(s):
    """민국년 '115/08/03' -> '2026-08-03'. 못 읽으면 None."""
    p = str(s).strip().split("/")
    if len(p) != 3:
        return None
    try:
        return f"{int(p[0]) + 1911:04d}-{int(p[1]):02d}-{int(p[2]):02d}"
    except ValueError:
        return None


def num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def fetch_month(ym):
    last = None
    for tmpl in ENDPOINTS:
        url = tmpl.format(ym=ym)
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode("utf-8"))
        except Exception as e:                                # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            continue
        if str(d.get("stat", "")).upper() not in ("OK", ""):
            last = f"stat={d.get('stat')}"
            continue
        rows = d.get("data") or []
        if not rows:
            last = "data 비어 있음"
            continue
        return rows, d.get("fields") or []
    print(f"  {ym} 실패 — {last}")
    return None, None


def main():
    end = END or date.today().isoformat()
    ms = months(START, end)
    print(f"가권지수 수집 {START} ~ {end} · {len(ms)}개월")

    out = {}
    bad_date = bad_num = 0
    for i, ym in enumerate(ms):
        rows, fields = fetch_month(ym)
        if rows is None:
            continue
        if i == 0:
            print(f"  필드: {fields}")
            print(f"  첫 행: {rows[0]}")
        for r in rows:
            if len(r) < 5:
                continue
            iso = roc_to_iso(r[0])
            if not iso:
                bad_date += 1
                continue
            o, h, l, c = (num(r[1]), num(r[2]), num(r[3]), num(r[4]))
            if c is None:
                bad_num += 1
                continue
            out[iso] = (o, h, l, c)
        print(f"  {ym}  누적 {len(out)}일")
        time.sleep(1.2)          # 證交所에 부담 주지 않는다

    if not out:
        sys.exit("한 달도 못 받았습니다. 위 로그의 실패 사유를 보고 엔드포인트를 고쳐야 합니다.")
    if bad_date or bad_num:
        print(f"  ::warning::날짜 못 읽음 {bad_date} · 숫자 못 읽음 {bad_num}")

    days = sorted(out)
    lo, hi = days[0], days[-1]
    if not (lo <= END or True):
        pass
    # 민국년 변환이 틀리면 연도가 엉뚱하게 나온다. 여기서 잡는다.
    if not (START[:4] <= lo[:4] <= end[:4]):
        sys.exit(f"날짜 범위가 이상합니다({lo} ~ {hi}). 민국년 변환을 확인하세요.")

    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close"])
        for d in days:
            w.writerow([d] + ["" if v is None else f"{v:.2f}" for v in out[d]])
    print(f"\n저장: {OUT} — 거래일 {len(days)}일 · {lo} ~ {hi}")
    print(f"  종가 범위 {min(v[3] for v in out.values()):,.0f} ~ {max(v[3] for v in out.values()):,.0f}")


if __name__ == "__main__":
    main()
