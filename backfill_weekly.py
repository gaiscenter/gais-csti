# -*- coding: utf-8 -*-
"""장기 소급 백필 — GDELT 사건 레코드만으로 **주간** 계열을 만든다.

왜 따로 있나
------------
전수코딩(`backfill_code.py`)은 기사 본문이 있어야 하고, 본문은 수집 시점에
받아 둔 것이 전부다. 3주만 지나도 링크가 죽기 시작하므로 그 계열은 사실상
최근 30일에 묶인다.

반면 **사건 레코드는 BigQuery 에 몇 년치가 있다.** 본문도 LLM 도 필요 없다.
그래서 이 작업이 대시보드의 3개월·6개월·1년·5년·10년 버튼을 채운다.
비용은 BigQuery 스캔뿐이고 LLM 호출은 0건이다.

주의
----
KBTI 저장소의 `backfill_history.py` 는 이름과 달리 `pipeline.py` 의 낡은
사본이어서 주간 파일을 만들지 않는다. 그 저장소의 `kbti_history.json` 은
그보다 앞선 판본이 남긴 것이다. 이 스크립트가 그 자리를 대신한다.

산출물
------
`csti_history.json` — {weeks, history, partner_names, ...}
history 의 키는 `<상대국>_threat` / `<상대국>_response` 이고
값은 weeks 와 길이가 같은 배열이다(값이 없는 주는 null).
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from google.cloud import bigquery

PROJECT_ID = os.environ.get("GCP_PROJECT", "ancient-voltage-508302-v3")
START = os.environ.get("BACKFILL_START") or "2026-08-01"
END = os.environ.get("BACKFILL_END") or ""          # 비우면 오늘
COST_GUARD_GB = float(os.environ.get("COST_GUARD_GB") or "800")
OUT = os.environ.get("HISTORY_FILE") or "csti_history.json"

# pipeline.py 와 **같은 값이어야 한다.** 다르면 두 계열이 다른 척도가 된다.
ANCHOR = "TWN"
PARTNERS = ["CHN", "USA"]
NAMES = {"CHN": "대만-중국", "USA": "대만-미국"}
CONFIDENCE_K = 5

QUERY = f"""
SELECT
  SQLDATE,
  Actor1CountryCode AS a1,
  Actor2CountryCode AS a2,
  GoldsteinScale AS g,
  NumMentions AS m
FROM `gdelt-bq.gdeltv2.events_partitioned`
WHERE
  _PARTITIONTIME >= TIMESTAMP(@start)
  AND _PARTITIONTIME < TIMESTAMP(@end)
  AND (
    (Actor1CountryCode = '{ANCHOR}' AND Actor2CountryCode IN UNNEST(@partners))
    OR (Actor2CountryCode = '{ANCHOR}' AND Actor1CountryCode IN UNNEST(@partners))
  )
  AND GoldsteinScale IS NOT NULL
  AND NumMentions > 0
"""


def week_start(d):
    """그 날짜가 속한 주의 월요일. kbti_history 와 같은 규약이다."""
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def run(client, start, end, dry=False):
    cfg = bigquery.QueryJobConfig(
        dry_run=dry, use_query_cache=not dry,
        query_parameters=[
            bigquery.ScalarQueryParameter("start", "TIMESTAMP", start + " 00:00:00"),
            bigquery.ScalarQueryParameter("end", "TIMESTAMP", end + " 00:00:00"),
            bigquery.ArrayQueryParameter("partners", "STRING", PARTNERS),
        ])
    job = client.query(QUERY, job_config=cfg)
    if dry:
        return job.total_bytes_processed
    return list(job.result())


def main():
    end = END or (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] CSTI 장기 소급 백필")
    print(f"  구간 {START} ~ {end} · 쌍 {ANCHOR}↔{PARTNERS}")

    client = bigquery.Client(project=PROJECT_ID)

    # ── 비용 가드 — 먼저 dry run 으로 스캔량을 잰다 ──────────────────
    gb = run(client, START, end, dry=True) / 1024**3
    print(f"  예상 스캔량 {gb:,.1f} GB (한도 {COST_GUARD_GB:,.0f} GB)")
    if gb > COST_GUARD_GB:
        sys.exit(f"한도 초과 — 중단합니다. 시작일을 늦추거나 COST_GUARD_GB 를 올리세요.")

    rows = run(client, START, end)
    print(f"  사건 레코드 {len(rows):,}건")
    if not rows:
        sys.exit("레코드가 없습니다 — 구간이나 국가코드를 확인하세요.")

    # ── 주 × 상대국 × 방향 으로 모은다 ───────────────────────────────
    acc = {}        # (week, partner, direction) -> [가중합, 멘션합, 건수]
    for r in rows:
        a1, a2 = r["a1"], r["a2"]
        partner = a2 if a1 == ANCHOR else a1
        if partner not in PARTNERS:
            continue
        direction = "response" if a1 == ANCHOR else "threat"
        d = datetime.strptime(str(r["SQLDATE"]), "%Y%m%d")
        k = (week_start(d), partner, direction)
        s = acc.setdefault(k, [0.0, 0, 0])
        s[0] += float(r["g"]) * int(r["m"])
        s[1] += int(r["m"])
        s[2] += 1

    weeks = sorted({k[0] for k in acc})
    idx = {w: i for i, w in enumerate(weeks)}
    history = {f"{p}_{d}": [None] * len(weeks)
               for p in PARTNERS for d in ("threat", "response")}

    for (w, p, dirn), (ws, ms, e) in acc.items():
        if not ms:
            continue
        # pipeline.py 와 같은 식: 평균 Goldstein 을 뒤집고 신뢰도로 수축한다.
        val = -1.0 * (ws / ms) * (e / (e + CONFIDENCE_K))
        history[f"{p}_{dirn}"][idx[w]] = round(val, 4)

    filled = {k: sum(1 for v in vals if v is not None) for k, vals in history.items()}
    print(f"  주 {len(weeks)}개 · {weeks[0]} ~ {weeks[-1]}")
    for k in sorted(filled):
        print(f"    {k:16s} {filled[k]:4d}/{len(weeks)} 주에 값 있음")

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "start_date": START,
        "end_date": end,
        "granularity": "week",
        "weeks": weeks,
        "total_rows": len(rows),
        "estimated_gb_processed": round(gb, 2),
        "confidence_k": CONFIDENCE_K,
        "history": history,
        "partner_names": NAMES,
        "_note": ("GDELT 사건 레코드만으로 만든 주간 계열. LLM 전수코딩 계열과는 "
                  "다른 추정량이므로 한 그림에서 같은 값으로 읽으면 안 된다."),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  저장: {OUT}")


if __name__ == "__main__":
    main()
