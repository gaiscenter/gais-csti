# -*- coding: utf-8 -*-
"""날짜 × 상대국별 해설을 만들어 llm_commentary.json 에 넣는다.

왜 따로 만드는가
----------------
파이프라인의 build_commentary 는 **오늘치 한 건**을 csti_output.json 에 쓰고 매
실행 덮는다. 그리고 그 글은 **감사 계열(GDELT+LLM 감사) 수치**를 설명한다.
대시보드 그래프가 LLM 전수코딩만 그리게 바뀌었으므로, 그 해설을 선 밑에 붙이면
화면에 없는 값을 설명하는 글이 된다.

또 하나. 그 해설은 5개국을 한 글에 담는다. 패널은 **고른 나라의 그래프 아래**에
붙으므로 나라별로 갈라야 한다 — 북한 그래프를 보는데 중국 얘기가 섞이면 안 된다.

쓰는 법
-------
    python build_day_commentary.py                 # 안 쓴 칸만
    COMMENTARY_FORCE=1 python build_day_commentary.py   # 전부 다시

출력
----
    llm_commentary.json
      {"generated_at":..., "codebook_fingerprint":...,
       "days": {"2026-09-28": {"PRK": {"ko":..., "en":..., "sources":[...]}}}}

지문이 바뀌면 그 칸은 다시 쓴다 — 코드북이 바뀌면 수치가 바뀌고, 옛 수치를
설명하는 글이 남으면 그래프와 어긋난다.
"""
import collections
import concurrent.futures
import glob
import json
import os
import re
import sys
import time

import llm_coder as L

CODED_DIR = os.environ.get("CODED_DIR") or "llm_coded"
OUT = os.environ.get("COMMENTARY_OUT") or "llm_commentary.json"
# .get(k, 기본값) 을 쓰면 안 된다 — 워크플로가 **빈 문자열**을 넘기므로
# 기본값이 안 먹고 모델 이름이 비어 호출이 통째로 실패한다. or 로 받는다.
MODEL = os.environ.get("COMMENTARY_MODEL") or "claude-haiku-4-5-20251001"
WORKERS = int(os.environ.get("COMMENTARY_WORKERS") or "6")
FORCE = os.environ.get("COMMENTARY_FORCE", "") == "1"
MAX_CELLS = int(os.environ.get("COMMENTARY_MAX") or "0")    # 0 = 전부
TRAJ_DAYS = 7          # 궤적을 몇 일 보여줄 것인가
TOP_ARTICLES = 10      # 한 칸에 넘길 근거 기사 수

K = L.CONFIDENCE_K
CHANNELS = {"military": "군사", "diplomatic": "외교", "economic": "경제"}
ANCHOR_KR = "대만"
ANCHOR_EN = "Taiwan"
COUNTRY_KR = {"CHN": "중국", "USA": "미국"}
COUNTRY_EN = {"CHN": "China", "USA": "the United States"}
ANCHOR_ZH = "臺灣"
COUNTRY_ZH = {"CHN": "中國", "USA": "美國"}


def _shrunk(rows):
    """[(goldstein, mentions)] -> (수축값, 원시값, 증거수). 비면 None."""
    if not rows:
        return None
    den = sum(m for _, m in rows)
    if not den:
        return None
    e = len(rows)
    raw = -1.0 * sum(g * m for g, m in rows) / den
    return raw * e / (e + K), raw, e


def load_days():
    """날짜 -> {상대국 -> {'cells': {...}, 'articles': [...]}}"""
    out = {}
    for p in sorted(glob.glob(os.path.join(CODED_DIR, "*.json"))):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        day = d.get("date")
        if not day:
            continue
        rows = collections.defaultdict(lambda: collections.defaultdict(list))
        arts = collections.defaultdict(list)
        for u, r in (d.get("articles") or {}).items():
            t = r.get("tags") or {}
            if t.get("v1") != "yes":
                continue
            g = L.GOLDSTEIN.get(t.get("v5"))
            if g is None:
                continue
            p2, v3, v4 = t.get("v2"), t.get("v3"), t.get("v4")
            if p2 not in COUNTRY_KR:
                continue
            m = r.get("mentions", 1)
            rows[p2][v3].append((g, m))
            ch = next((k for k, kr in CHANNELS.items()
                       if v4 in (k, kr)), None)
            if ch:
                rows[p2][f"{v3}:{ch}"].append((g, m))
            arts[p2].append({"url": u, "title": r.get("title") or "",
                             "code": t.get("v5"), "g": g, "m": m,
                             "dir": v3, "ch": v4})
        day_block = {p2: {"cells": {k: _shrunk(v) for k, v in rows[p2].items()},
                          "articles": arts[p2],
                          "n_day_coded": d.get("n_coded") or 0,
                          "fingerprint": d.get("codebook_fingerprint")}
                     for p2 in rows}
        # **사건이 하나도 없던 쌍도 기록한다.** 완성된 하루에 증거가 없으면
        # 지수는 0 인데, 해설이 아예 없으면 화면에 "해설이 아직 없습니다"가 떠
        # '자료가 빠졌다'로 읽힌다. 실제로는 "N건을 봤는데 그 쌍의 사건이
        # 없었다"이고, 둘은 전혀 다른 말이다 (2026-11-09 PRK 가 그랬다).
        for p2 in COUNTRY_KR:
            if p2 not in day_block:
                day_block[p2] = {"cells": {}, "articles": [],
                                 "n_day_coded": d.get("n_coded") or 0,
                                 "fingerprint": d.get("codebook_fingerprint")}
        out[day] = day_block
    return out


def numbers_block(days, day, partner):
    """수치 요약. **궤적을 반드시 함께 준다** — 오늘 값만 주면 모델이 방향을
    지어낸다(파이프라인에서 실제로 '하락'을 '상승'이라 쓴 일이 있다)."""
    ordered = sorted(days)
    i = ordered.index(day)
    win = ordered[max(0, i - TRAJ_DAYS + 1):i + 1]
    kr = COUNTRY_KR.get(partner, partner)
    lines = [f"{ANCHOR_KR}-{kr} · {day} (왼쪽이 과거, 오른쪽이 오늘)"]

    def traj(key):
        vals = []
        for dd in win:
            c = (days[dd].get(partner) or {}).get("cells") or {}
            v = c.get(key)
            vals.append("—" if not v else f"{v[0]:+.1f}")
        return " ".join(vals)

    cur = (days[day].get(partner) or {}).get("cells") or {}
    for dirn, dl in (("threat", f"{kr} → {ANCHOR_KR}"), ("response", f"{ANCHOR_KR} → {kr}")):
        v = cur.get(dirn)
        head = (f"  [{dl}] 전체 [{traj(dirn)}] 오늘 "
                + ("증거 없음" if not v else f"{v[0]:+.2f} (근거 {v[2]}건)"))
        lines.append(head)
        for ch, chkr in CHANNELS.items():
            cv = cur.get(f"{dirn}:{ch}")
            if cv:
                lines.append(f"      {chkr}: [{traj(f'{dirn}:{ch}')}] "
                             f"오늘 {cv[0]:+.2f} (근거 {cv[2]}건)")
            else:
                lines.append(f"      {chkr}: 오늘 증거 없음 — 값을 말하지 말 것")
    return "\n".join(lines)


def articles_block(cell):
    """영향력 큰 순으로 번호 매긴 근거 기사."""
    arts = sorted(cell["articles"], key=lambda a: -abs(a["g"] * a["m"]))[:TOP_ARTICLES]
    out = []
    for n, a in enumerate(arts, 1):
        out.append(f"[{n}] ({a['dir']}/{a['ch']}/{a['code']}, G={a['g']:+.1f}, "
                   f"언급 {a['m']}) {a['title'][:150]}")
    return "\n".join(out), arts


# 지명 표기 통일. 프롬프트에도 넣었지만 모형이 놓칠 수 있어 출력에서 한 번 더 거른다.
# **생성된 해설 문장에만 적용한다** — 기사 제목·원문은 그대로 둔다.
# 이미 올바른 형태로 쓴 것을 다시 감싸지 않도록 앞뒤를 살핀다.
# ── 용어 정규화 ────────────────────────────────────────────────
# KBTI 의 독도·동해 장치와 같은 구조다. 다만 양안에서는 틀렸을 때의 대가가
# 훨씬 크다. 아래는 **잠정 목록**이고, 최종 목록은 사람이 정해야 한다.
# 원칙: 중국 쪽 호칭(대만 당국/대만 지구/중국 대만성)을 쓰지 않는다.
#
# 한국어는 명사를 바꾸면 뒤 조사도 바뀐다 — '대만 당국이' 를 '대만 정부이' 로
# 두면 안 된다. 받침 유무로 조사를 다시 고른다.

_JOSA = {"이": "가", "가": "이", "은": "는", "는": "은",
         "을": "를", "를": "을", "과": "와", "와": "과"}


def _has_batchim(ch):
    """한글 음절의 받침 유무. 가-힣 범위 밖이면 받침 있는 것으로 본다."""
    if not ch or not ("가" <= ch <= "힣"):
        return True
    return (ord(ch) - 0xAC00) % 28 != 0


def _fit_josa(noun, josa):
    """noun 뒤에 올 조사를 받침에 맞춰 고른다."""
    if not josa:
        return ""
    bat = _has_batchim(noun[-1])
    pair = {"이": ("이", "가"), "가": ("이", "가"), "은": ("은", "는"), "는": ("은", "는"),
            "을": ("을", "를"), "를": ("을", "를"), "과": ("과", "와"), "와": ("과", "와")}
    if josa in pair:
        return pair[josa][0] if bat else pair[josa][1]
    return josa


def _ko_sub(noun):
    """명사를 noun 으로 바꾸고 뒤따르는 조사를 맞춘다."""
    def f(m):
        return noun + _fit_josa(noun, m.group("j") or "")
    return f


_J = r"(?P<j>이|가|은|는|을|를|과|와)?"

_TERMS = [
    # 한국어 — 조사까지 함께 잡는다
    (re.compile(r"(?:중국\s*대만성|대만\s*지구)" + _J), _ko_sub("대만")),
    (re.compile(r"대만\s*당국" + _J), _ko_sub("대만 정부")),
    # 영어
    (re.compile(r"\bTaiwan Province of China\b", re.I), "Taiwan"),
    (re.compile(r"\bTaiwan region\b", re.I), "Taiwan"),
    (re.compile(r"\bTaiwan authorities\b", re.I), "Taiwan's government"),
    (re.compile(r"\bChinese Taipei\b"), "Taiwan"),
    # 해협 명칭은 중립어를 쓴다
    (re.compile(r"(?<!대만)해협 양안"), "대만해협 양안"),

    # 번체중국어 — **순서가 중요하다.** 복합어를 먼저 잡지 않고 台→臺 를 먼저
    # 돌리면 '中國台灣省' 이 '中國臺灣省' 이 되어 아래 규칙을 빠져나간다.
    (re.compile(r"中國[台臺]灣省"), "臺灣"),
    (re.compile(r"[台臺]灣當局"), "臺灣政府"),
    (re.compile(r"[台臺]灣地區"), "臺灣"),
    (re.compile(r"中華[台臺]北"), "臺灣"),
    (re.compile(r"台灣"), "臺灣"),          # 표기 통일 (Kay 지정: 臺灣)
    # 大陸 계열은 대만에서도 쓰는 말이지만, 이 지수는 中國 으로 통일한다.
    # 한 해설 안에서 두 호칭이 섞이면 읽는 사람이 다른 주체로 오해한다.
    (re.compile(r"大陸當局"), "中國政府"),
    (re.compile(r"中國大陸"), "中國"),
    # 歐洲大陸·美洲大陸 같은 지리 용어는 건드리지 않는다 (앞 글자가 洲 다).
    (re.compile(r"(?<![歐美非亞洲])大陸(?!棚|性|法系)"), "中國"),
    # 대만 어법으로 통일한다. 홍콩·대륙 표기가 섞이면 바로 티가 난다.
    (re.compile(r"網絡"), "網路"),
    (re.compile(r"信息"), "資訊"),
    (re.compile(r"軟件"), "軟體"),
    (re.compile(r"硬件"), "硬體"),
]


def normalize_terms(text):
    if not text:
        return text
    for pat, rep in _TERMS:
        text = pat.sub(rep, text)
    return text

# 근거가 이보다 적은 칸에는 **해석을 쓰지 않는다.**
# 지수는 수축 e/(e+5) 로 얇은 증거를 깎는다(e=1 이면 0.167배). 그런데 글에는
# 수축이 없어서, 기사 한 건으로도 "−0.17의 긴장도 하락을 보였습니다" 같은
# 단정문이 나온다. 숫자는 겸손한데 문장은 아니다.
#
# 실측(2026-11-09): 해설 칸 149개 중 **40개(27%)가 기사 한 건**, 52개(35%)가
# 두 건 이하다. 대부분 일본·중국·러시아로, 그날 실제로 사건이 없었고 GDELT 가
# 곁가지 기사를 하나 올린 경우다.
#
# 대신 **사실만** 적는다 — 기사 제목을 그대로 보이고 해석하지 않는다.
# 보는 사람은 그 점이 무엇 때문인지 알 수 있고, 없는 이야기는 안 만들어진다.
# API 호출도 들지 않는다.
MIN_ARTICLES = int(os.environ.get("COMMENTARY_MIN_ARTICLES") or "3")


def thin_note(day, partner, cell):
    """근거가 얇은 칸의 사실 기록. 모델을 부르지 않는다."""
    arts = sorted(cell["articles"], key=lambda a: -abs(a["g"] * a["m"]))
    kr = COUNTRY_KR.get(partner, partner)
    en = COUNTRY_EN.get(partner, partner)
    n = len(arts)
    if n == 0:
        nd = cell.get("n_day_coded") or 0
        zh = COUNTRY_ZH.get(partner, partner)
        return {"ko": f"이 날 기사 {nd:,}건을 읽었고, 그중 {ANCHOR_KR}-{kr} 사이의 사건은 "
                      f"없었습니다. 지수 0 은 '자료가 없다'가 아니라 "
                      f"'사건이 확인되지 않았다'는 뜻입니다.",
                "en": f"{nd:,} articles were read for this day and none reported an "
                      f"event between {ANCHOR_EN} and {en}. A value of zero means no "
                      f"event was found, not that data is missing.",
                "zh": f"當日共讀取 {nd:,} 篇報導，其中沒有{ANCHOR_ZH}與{zh}之間的事件。"
                      f"指數為 0 並非「沒有資料」，而是「未發現事件」。",
                "model": "none (no events)",
                "codebook_fingerprint": cell["fingerprint"],
                "n_articles": 0, "thin": True, "empty": True,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "sources": []}
    # **제목을 본문에 넣지 않는다.** 아래 '근거 기사' 에 링크로 같은 제목이
    # 붙어서 두 번 보이고, 위쪽 글자만 본 사람은 링크가 없다고 읽는다.
    ko = (f"근거 기사가 {n}건뿐이라 {ANCHOR_KR}-{kr} 관계의 움직임으로 해석하지 "
          f"않습니다. 아래 근거 기사를 직접 보십시오.")
    eng = (f"Only {n} article{'s' if n != 1 else ''} — too thin to read as a movement "
           f"in {ANCHOR_EN}–{en} relations. See the source article"
           f"{'s' if n != 1 else ''} below.")
    zht = (f"佐證報導僅 {n} 篇，因此不將其解讀為{ANCHOR_ZH}與"
           f"{COUNTRY_ZH.get(partner, partner)}關係的變動。"
           f"請直接參閱下方佐證報導。")
    return {"ko": ko, "en": eng, "zh": zht, "model": "none (thin evidence)",
            "codebook_fingerprint": cell["fingerprint"],
            "n_articles": n, "thin": True,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "sources": [{"url": a["url"], "title": a["title"]} for a in arts[:3]]}



PROMPT = """아래는 대만과 {kr}({en}) 사이의 {day} 하루 긴장도 지수와, 그 지수를 만든 기사입니다.

{numbers}

근거 기사 (영향력 순)
{articles}

이 둘만 근거로 2~4문장의 짧은 해설을 **한국어·영어·번체중국어 세 가지로** 쓰십시오. 규칙:

1. **주어진 기사에 없는 사실을 쓰지 마십시오.** 배경지식·추측·다른 날 사건을 끌어오지 마십시오.
2. **어느 채널이 움직였는지 밝히고, 그 채널의 기사로만 설명하십시오.** 군사 지수를 경제 기사로 설명하는 것은 오류입니다.
3. **방향을 지어내지 마십시오.** 궤적에 근거해서만 상승·하락을 말하고, '증거 없음'인 칸은 값도 상태도 말하지 마십시오.
4. 근거가 1~2건뿐이면 그렇게 쓰십시오 — "근거 1건이라 단정하기 어렵다"가 억지 해석보다 낫습니다.
5. 지수 숫자를 그대로 나열하지 말고, 그날 **무슨 일이 있었는지**를 먼저 쓰십시오. 패널을 보는 사람은 숫자를 이미 보고 있습니다.
6. 제3국의 시각이 기사에 담겨 있어도, 그것은 지금 설명하는 두 나라 관계의 논조가 아닙니다.
7. **호칭 표기.** 기사 원문이 무엇이라 쓰든, 해설에서는 다음을 지킵니다.
   한국어: '대만'·'대만 정부'를 씁니다. '대만 당국'·'중국 대만성'·'대만 지구'를 쓰지 않습니다.
   영어:   'Taiwan' / "Taiwan's government" 를 씁니다. 'Taiwan Province of China',
           'Taiwan region', 'Taiwan authorities', 'Chinese Taipei' 를 쓰지 않습니다.
   번체중국어: **臺灣**(台灣 아님)·**臺灣政府**를 씁니다. 中國臺灣省·臺灣當局·
           臺灣地區·中華臺北를 쓰지 않습니다. 중국은 中國, 양안은 兩岸입니다.
   통일·독립 등 정치적 지위에 대한 평가를 해설이 내리지 않습니다 — 기사가 보도한
   사건만 씁니다.
8. **번체중국어는 대만 어법으로 씁니다.** 網路(網絡 아님), 資訊(信息 아님),
   軟體·硬體(軟件·硬件 아님). 홍콩·중국 대륙 표기를 쓰지 마십시오.

출력은 JSON 한 덩어리뿐입니다. 설명을 붙이지 마십시오.
{{"ko": "한국어 해설", "en": "English commentary", "zh": "繁體中文解說", "used": [사용한 기사 번호]}}"""


_HANGUL = re.compile(r"[가-힣]")


def _zh_clean(text):
    """번체 해설에 한글이 섞였는지. 모델이 세 언어를 한 번에 낼 때 드물게 샌다."""
    return not _HANGUL.search(text or "")


def write_one(client, days, day, partner, _retry=True):
    cell = days[day][partner]
    ab, arts = articles_block(cell)
    msg = PROMPT.format(kr=COUNTRY_KR.get(partner, partner),
                        en=COUNTRY_EN.get(partner, partner), day=day,
                        numbers=numbers_block(days, day, partner), articles=ab)
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=700,
            messages=[{"role": "user", "content": msg}])
        txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    except Exception as e:                                   # noqa: BLE001
        return day, partner, None, f"{type(e).__name__}: {str(e)[:120]}"
    try:
        s = txt[txt.index("{"):txt.rindex("}") + 1]
        j = json.loads(s)
    except (ValueError, IndexError):
        return day, partner, None, "JSON 파싱 불가"
    ko = normalize_terms((j.get("ko") or "").strip())
    en = normalize_terms((j.get("en") or "").strip())
    zh = normalize_terms((j.get("zh") or "").strip())
    if not ko and not en and not zh:
        return day, partner, None, "본문 비어 있음"
    # 번체에 한글이 섞이면 한 번만 다시 부른다. 두 번째도 섞이면 그대로 두되
    # 표시를 남긴다 — 조용히 넘기면 발표장에서 발견하게 된다.
    if zh and not _zh_clean(zh) and _retry:
        return write_one(client, days, day, partner, _retry=False)
    used = [arts[n - 1] for n in (j.get("used") or []) if 1 <= n <= len(arts)]
    return day, partner, {
        "ko": ko, "en": en, "zh": zh, "zh_mixed": (bool(zh) and not _zh_clean(zh)) or None,
        "model": MODEL,
        "codebook_fingerprint": cell["fingerprint"],
        # 이 해설이 몇 건을 보고 쓰였는지. 나중에 기사가 늘면 다시 쓰기 위해서다.
        "n_articles": len(cell["articles"]),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sources": [{"url": a["url"], "title": a["title"]} for a in used[:5]],
    }, None


def main():
    t0 = time.time()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY 가 없다.")
    import anthropic
    client = anthropic.Anthropic(api_key=key)

    days = load_days()
    if not days:
        sys.exit(f"{CODED_DIR}/ 에 코딩 결과가 없다.")

    prev = {}
    if os.path.exists(OUT):
        try:
            prev = (json.load(open(OUT, encoding="utf-8")).get("days") or {})
        except (ValueError, OSError):
            prev = {}

    # 이미 쓴 글에도 지명 규칙을 적용한다. **API 호출이 없다** — 규칙을 나중에
    # 넣었으므로 옛 글에는 안 걸려 있고, 전체를 다시 생성하면 비결정적 모형이라
    # 손댈 이유가 없는 글까지 바뀐다.
    fixed = 0
    for day, ps in prev.items():
        for pt, rec in ps.items():
            for f in ("ko", "en"):
                before = rec.get(f) or ""
                after = normalize_terms(before)
                if after != before:
                    rec[f] = after
                    fixed += 1
    if fixed:
        print(f"  지명 표기 교정 {fixed}개 필드 (호출 없음)")

    # **오늘(UTC)은 매번 다시 쓴다.** GDELT 의 하루는 UTC 09:00(한국시간)에 시작해
    # 하루 내내 기사가 들어온다. 첫 실행은 20~30건만 보고 쓰는데, 건너뛰기만 하면
    # 그 글이 그대로 굳어 저녁에도 아침 몇 건짜리 해설이 걸려 있다
    # (2026-11-03 04:10 에 26건만 보고 쓴 글이 그랬다).
    today_utc = time.strftime("%Y-%m-%d", time.gmtime())

    # **없어진 칸의 옛 해설을 지운다.** 지금까지는 더하기만 했다. 기사가 빠져
    # 그 쌍의 칸이 사라져도 옛 해설이 남아, 그래프에 없는 값을 설명하는 글이
    # 화면에 걸린다 (2026-11-08: 2016년 GSOMIA 기사가 빠진 뒤 JPN 해설이 그랬다).
    pruned = []
    for day in list(prev):
        live = days.get(day) or {}
        for partner in list(prev[day]):
            if partner not in live:
                del prev[day][partner]
                pruned.append(f"{day}/{partner}")
        if not prev[day]:
            del prev[day]
    if pruned:
        print(f"  근거가 사라져 해설을 지운 칸 {len(pruned)}개: " + ", ".join(pruned[:10]))

    todo, redo = [], []
    for day in sorted(days):
        for partner, cell in days[day].items():
            have = (prev.get(day) or {}).get(partner)
            if not have or FORCE:
                todo.append((day, partner))
                continue
            # 지문이 다르면 다시 쓴다 — 옛 수치를 설명하는 글이 남으면 안 된다.
            if have.get("codebook_fingerprint") != cell["fingerprint"]:
                todo.append((day, partner))
                continue
            # 아직 끝나지 않은 하루이거나, 기사가 늘었으면 다시 쓴다.
            n_now = len(cell["articles"])
            # 얇은 칸이 두꺼워졌거나 그 반대면 형식이 바뀌어야 한다
            was_thin, is_thin = bool(have.get("thin")), n_now < MIN_ARTICLES
            # 저장된 글이 **지금의 규칙과 어긋나면** 다시 쓴다. 두 가지를 본다.
            #   ① 번체에 한글이 샜다 (모델이 세 언어를 낼 때 드물게 샌다)
            #   ② 용어 규칙이 바뀌어 저장된 글이 더는 규칙을 만족하지 않는다
            # ②가 있어야 용어 목록을 고쳤을 때 해당 칸만 자동으로 다시 쓰인다.
            # 없으면 규칙만 바뀌고 글은 옛 표기로 남는다(2026-11-12: 大陸 2곳).
            # 사실기록은 기사 제목을 그대로 인용하므로 둘 다 적용하지 않는다.
            # 사실기록·무사건은 모델을 부르지 않으므로 매번 다시 쓴다. 문구를
            # 고쳐도 옛 글이 남지 않는다 (2026-11-13: 제목 중복을 뺐다).
            if have.get("thin") or have.get("empty"):
                todo.append((day, partner))
                continue
            zh_now = have.get("zh") or ""
            zh_bad = (not have.get("thin") and zh_now
                      and ((not _zh_clean(zh_now) and not have.get("zh_mixed"))
                           or normalize_terms(zh_now) != zh_now
                           or normalize_terms(have.get("ko") or "") != (have.get("ko") or "")))
            if day >= today_utc or have.get("n_articles") not in (None, n_now) \
                    or was_thin != is_thin or zh_bad:
                todo.append((day, partner))
                redo.append((day, partner, have.get("n_articles"), n_now))
    if redo:
        same = [r for r in redo if r[0] >= today_utc]
        grew = [r for r in redo if r[0] < today_utc]
        if same:
            print(f"  오늘({today_utc}) {len(same)}칸 — 하루가 끝나지 않아 다시 씀")
        if grew:
            print("  기사가 늘어 다시 쓰는 칸: "
                  + ", ".join(f"{d}/{p} {a}→{b}건" for d, p, a, b in grew[:8]))
    fps = {c["fingerprint"] for d in days.values() for c in d.values()}
    print(f"날짜 {len(days)}일 · 칸 {sum(len(v) for v in days.values())}개 · "
          f"쓸 것 {len(todo)}개 · 지문 {sorted(x or '?' for x in fps)}")
    if len(fps) > 1:
        print("  ::warning::말뭉치에 지문이 섞여 있다 — 해설이 서로 다른 코드북의 수치를 설명하게 된다")
    if MAX_CELLS:
        todo = todo[:MAX_CELLS]
        print(f"  이번 실행 상한 {MAX_CELLS}개")
    if not todo:
        if fixed or pruned:
            # 교정만 했어도 저장해야 한다. 안 그러면 다음 실행에서 또 교정한다.
            out = {d: dict(v) for d, v in prev.items()}
            with open(OUT, "w", encoding="utf-8") as f:
                json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "model": MODEL, "n_days": len(out),
                           "n_cells": sum(len(v) for v in out.values()),
                           "days": out}, f, ensure_ascii=False, indent=1)
            print(f"  저장: {OUT} — 교정·삭제만 반영")
        else:
            print("  쓸 것이 없다.")
        return

    out = {d: dict(v) for d, v in prev.items()}
    fails = collections.Counter()

    # 얇은 칸은 모델을 부르지 않고 사실만 적는다.
    thin = [(d, p) for d, p in todo if len(days[d][p]["articles"]) < MIN_ARTICLES]
    for d, p in thin:
        out.setdefault(d, {})[p] = thin_note(d, p, days[d][p])
    if thin:
        print(f"  근거 {MIN_ARTICLES}건 미만이라 해석 없이 사실만 적은 칸 {len(thin)}개 "
              f"(호출 없음)")
    todo = [x for x in todo if x not in set(thin)]
    if not todo:
        print("  모델을 부를 칸이 없다.")

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = [pool.submit(write_one, client, days, d, p) for d, p in todo]
        for f in concurrent.futures.as_completed(futs):
            day, partner, rec, err = f.result()
            done += 1
            if rec:
                out.setdefault(day, {})[partner] = rec
            else:
                fails[err.split(":")[0]] += 1
            if done % 25 == 0:
                print(f"    {done}/{len(todo)} · 실패 {sum(fails.values())}", flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "model": MODEL, "n_days": len(out),
                   "n_cells": sum(len(v) for v in out.values()),
                   "days": out}, f, ensure_ascii=False, indent=1)

    n = sum(len(v) for v in out.values())
    print(f"\n저장: {OUT} — {len(out)}일 {n}칸")
    if fails:
        print("실패: " + " · ".join(f"{k} {v}" for k, v in fails.most_common()))
    print(f"소요 {time.time()-t0:.0f}초")


if __name__ == "__main__":
    main()
