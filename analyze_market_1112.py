# -*- coding: utf-8 -*-
"""CSTI 와 대만 가권지수의 관계를 **미리 정한 가설 하나로** 검정한다.

설계 (먼저 못 박는다 — 돌려보고 고르면 가짜 발견이 된다)
----------------------------------------------------------
주 가설 : CSTI 전체 CHN_threat 가 높은 날, 가권지수의 **변동성**(|일간 수익률|)이
          크다. 시차 0(같은 날)과 1(다음 거래일) 둘만 본다.
          방향(오르냐 내리냐)이 아니라 흔들림을 보는 이유는 두 가지다 —
          이론적으로 긴장은 불확실성을 키우지 방향을 정하지 않고,
          통계적으로도 수익률보다 |수익률|이 작은 표본에서 더 잘 잡힌다.

나머지(도메인 × 방향 × 시차)는 전부 **탐색적**이고, 본페로니 기준을 함께 찍는다.

표본이 작다
-----------
거래일 60여 일이면 r=0.30 을 80% 검정력으로 잡는 데 필요한 약 85 일에 못 미친다.
**r≈0.35 이상의 큰 효과만 보인다.** 그래서 이 스크립트는 p 값만 내지 않고
신뢰구간을 같이 낸다 — 구간이 0 을 넉넉히 품으면 "효과 없음"이 아니라
"이 표본으로는 모른다"이다.

교란
----
이 구간 가권지수의 큰 움직임은 AI·반도체 순환이 만든 것이고, 그 뉴스는
코드북이 '무관'으로 거른다. 상관이 없게 나오는 것이 자연스럽고, 그것은
실패가 아니라 **판별 타당도**의 증거로 읽어야 한다.
"""
import csv
import json
import math
import os
import sys
from datetime import datetime, timedelta

SERIES = os.environ.get("SERIES_FILE") or "llm_series.json"
MARKET = os.environ.get("MARKET_FILE") or "market/taiex.csv"
OUT = os.environ.get("ANALYSIS_FILE") or "market/csti_market_analysis.json"
PRIMARY = ("overall", "CHN_threat")


# ── 통계 ────────────────────────────────────────────────────────
def pearson(xs, ys):
    n = len(xs)
    if n < 4:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def ranks(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def fisher(r, n):
    """Fisher z 로 p 값과 95% 신뢰구간. scipy 없이 쓴다."""
    if r is None or n < 5 or abs(r) >= 1:
        return None, None, None
    z = 0.5 * math.log((1 + r) / (1 - r))
    se = 1.0 / math.sqrt(n - 3)
    p = math.erfc(abs(z / se) / math.sqrt(2))
    lo, hi = z - 1.96 * se, z + 1.96 * se
    return p, math.tanh(lo), math.tanh(hi)


def report(label, xs, ys):
    r = pearson(xs, ys)
    if r is None:
        return {"label": label, "n": len(xs), "note": "계산 불가"}
    p, lo, hi = fisher(r, len(xs))
    rs = pearson(ranks(xs), ranks(ys))
    return {"label": label, "n": len(xs), "r": round(r, 3),
            "p": None if p is None else round(p, 4),
            "ci95": [round(lo, 3), round(hi, 3)] if lo is not None else None,
            "spearman": None if rs is None else round(rs, 3)}


def line(d):
    if "r" not in d:
        return f"  {d['label']:34s} n={d['n']:3d}  {d.get('note','')}"
    ci = f"[{d['ci95'][0]:+.2f}, {d['ci95'][1]:+.2f}]" if d.get("ci95") else "—"
    return (f"  {d['label']:34s} n={d['n']:3d}  r={d['r']:+.3f}  p={d['p']:.3f}  "
            f"95%CI {ci:>16s}  ρ={d['spearman']:+.3f}")


# ── 자료 ────────────────────────────────────────────────────────
def load_market():
    if not os.path.exists(MARKET):
        sys.exit(f"{MARKET} 가 없습니다. fetch_taiex.py 를 먼저 돌리세요.")
    rows = {}
    with open(MARKET, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                rows[r["date"]] = {k: (float(r[k]) if r.get(k) else None)
                                   for k in ("open", "high", "low", "close")}
            except ValueError:
                continue
    days = sorted(rows)
    out = {}
    for i, d in enumerate(days):
        c, pc = rows[d]["close"], (rows[days[i - 1]]["close"] if i else None)
        if c is None or not pc:
            continue
        ret = math.log(c / pc)
        hi, lo = rows[d]["high"], rows[d]["low"]
        rng = (math.log(hi / lo) if hi and lo and lo > 0 else None)
        out[d] = {"close": c, "ret": ret, "absret": abs(ret), "range": rng}
    return out


def load_csti():
    with open(SERIES, encoding="utf-8") as f:
        s = json.load(f)
    out = {}
    for d, v in s.items():
        if d.startswith("_") or not isinstance(v, dict):
            continue
        rec = {"overall": {}, }
        for k, c in (v.get("cells") or {}).items():
            if isinstance(c, dict) and isinstance(c.get("value"), (int, float)):
                rec["overall"][k] = c["value"]
        for ch, cells in (v.get("channels") or {}).items():
            rec[ch] = {k: c["value"] for k, c in (cells or {}).items()
                       if isinstance(c, dict) and isinstance(c.get("value"), (int, float))}
        out[d] = rec
    return out


def pair(csti, mkt, domain, key, lag, ycol):
    """CSTI(domain,key) 를 lag 거래일 뒤의 시장 값과 맞춘다.

    **휴장일은 다음 거래일에 합친다 — 복제하지 않는다.** 금·토·일의 긴장도를
    월요일에 각각 붙이면 같은 수익률이 세 번 들어가 표본이 부풀고 유의성이
    과장된다(의사복제). 거래일 하나에 그 직전 비거래일까지의 **평균**을 쓴다.
    그래서 n 은 거래일 수를 넘지 않는다.

    코딩한 날인데 그 칸에 증거가 없으면 0 이다 — 대시보드와 같은 규약.
    """
    days = sorted(mkt)
    if not days:
        return [], []
    # 각 CSTI 날짜가 어느 거래일에 반영되는지
    bucket = {}
    for d, rec in csti.items():
        if domain not in rec:
            continue
        j = None
        for i, q in enumerate(days):
            if q >= d:
                j = i
                break
        if j is None:
            continue
        bucket.setdefault(j, []).append(rec[domain].get(key, 0.0))

    xs, ys = [], []
    for j in sorted(bucket):
        k = j + lag
        if k >= len(days):
            continue
        y = mkt[days[k]].get(ycol)
        if y is None:
            continue
        vals = bucket[j]
        xs.append(sum(vals) / len(vals))
        ys.append(y)
    return xs, ys


def main():
    mkt, csti = load_market(), load_csti()
    md, cd = sorted(mkt), sorted(csti)
    print(f"시장 {len(md)}거래일 {md[0]} ~ {md[-1]}")
    print(f"CSTI {len(cd)}일 {cd[0]} ~ {cd[-1]}")

    dom, key = PRIMARY
    print(f"\n━━ 주 가설 (미리 정함) — {dom}/{key} 대 |일간 수익률| ━━")
    primary = []
    for lag in (0, 1):
        xs, ys = pair(csti, mkt, dom, key, lag, "absret")
        d = report(f"lag {lag}일", xs, ys)
        primary.append(d); print(line(d))

    print("\n━━ 탐색적 — 전부 돌린 것이라 그대로 읽으면 안 된다 ━━")
    expl, keys = [], ["CHN_threat", "CHN_response", "USA_threat", "USA_response"]
    doms = ["overall", "military", "diplomatic", "economic"]
    for ycol, yname in (("absret", "|수익률|"), ("ret", "수익률")):
        for dm in doms:
            for k in keys:
                for lag in (0, 1):
                    xs, ys = pair(csti, mkt, dm, k, lag, ycol)
                    if len(xs) < 20:
                        continue
                    expl.append(report(f"{dm}/{k} L{lag} → {yname}", xs, ys))
    n_tests = len(expl)
    thr = 0.05 / max(1, n_tests)
    print(f"  검정 {n_tests}회 · 본페로니 기준 p < {thr:.5f}")
    hits = [d for d in expl if d.get("p") is not None and d["p"] < thr]
    for d in sorted(expl, key=lambda x: x.get("p") or 1)[:8]:
        mark = " ★" if d in hits else ""
        print(line(d) + mark)
    print(f"  기준을 넘은 것: {len(hits)}개" + ("" if hits else "  ← 없음이 정상이다"))

    print("\n━━ 사건연구 — 긴장이 크게 튄 날 ━━")
    rows = sorted(((v["overall"].get(key, 0.0), d) for d, v in csti.items()
                   if "overall" in v), reverse=True)[:6]
    days = sorted(mkt); idx = {d: i for i, d in enumerate(days)}
    ev = []
    for val, d in rows:
        j = idx.get(d) or next((idx[q] for q in days if q > d), None)
        if j is None:
            continue
        seq = []
        for off in (0, 1, 2):
            if j + off < len(days):
                seq.append(round(100 * mkt[days[j + off]]["ret"], 2))
        ev.append({"date": d, "csti": round(val, 2), "ret_pct": seq})
        print(f"  {d}  CSTI {val:+.2f}  →  당일/익일/익익일 수익률 "
              + " · ".join(f"{x:+.2f}%" for x in seq))

    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now().isoformat(timespec="seconds"),
                   "primary_hypothesis": {"domain": dom, "key": key,
                                          "outcome": "abs_daily_log_return",
                                          "results": primary},
                   "exploratory": expl, "n_tests": n_tests,
                   "bonferroni_threshold": thr,
                   "event_study": ev,
                   "_caveat": ("거래일 60여 일은 r≈0.35 미만을 가릴 검정력이 없다. "
                               "이 구간 가권지수의 큰 움직임은 AI·반도체 순환이 만든 "
                               "것이고 코드북은 그 뉴스를 '무관'으로 거른다. 상관이 "
                               "없는 것은 실패가 아니라 판별 타당도의 증거다.")},
                  f, ensure_ascii=False, indent=1)
    print(f"\n저장: {OUT}")


if __name__ == "__main__":
    main()
