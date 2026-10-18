"""
시세·경제지표·뉴스를 가져와 data/market.json, data/news.json 에 저장하는 스크립트.
GitHub Actions가 정해진 시간마다 자동으로 실행합니다.
"""
import csv
import io
import json
import os
import re
import datetime as dt
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests
import yfinance as yf

KST = dt.timezone(dt.timedelta(hours=9))
DATA = Path(__file__).parent / "data"
HISTORY_DAYS = 60
UA = {"User-Agent": "Mozilla/5.0 (market-board; +https://github.com)"}

# ───────────────────────── 1. 시세 ─────────────────────────
# (id, 그룹, 이름, 야후 티커, 단위, 소수점, 배율)
TICKERS = [
    ("kospi", "국내 증시", "코스피", "^KS11", "pt", 2, 1),
    ("kosdaq", "국내 증시", "코스닥", "^KQ11", "pt", 2, 1),
    ("nasdaq", "미국 증시", "나스닥 종합", "^IXIC", "pt", 2, 1),
    ("sp500", "미국 증시", "S&P 500", "^GSPC", "pt", 2, 1),
    ("gold_intl", "금", "국제 금 (뉴욕 선물)", "GC=F", "달러/온스", 2, 1),
    ("usdkrw", "환율", "원/달러", "KRW=X", "원", 2, 1),
    ("jpykrw", "환율", "원/100엔", "JPYKRW=X", "원", 2, 100),
    ("eurkrw", "환율", "원/유로", "EURKRW=X", "원", 2, 1),
]

# 가상자산: 업비트 원화 시세 (실패하면 야후 원화 환산 시세)
CRYPTO = [
    ("btc", "비트코인", "KRW-BTC", "BTC-KRW"),
    ("eth", "이더리움", "KRW-ETH", "ETH-KRW"),
    ("xrp", "엑스알피(리플)", "KRW-XRP", "XRP-KRW"),
]

# 미국 경제지표: FRED (미국 세인트루이스 연준) 공개 CSV, 키 필요 없음
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"

GOLD_API = "https://apis.data.go.kr/1160100/service/GetGeneralProductInfoService/getGoldPriceInfo"


def make_item(id_, group, name, unit, decimals, history, source, note=None):
    """history: [(날짜, 값), ...] 오래된 것부터"""
    last_d, last_v = history[-1]
    prev_v = history[-2][1] if len(history) > 1 else last_v
    change = last_v - prev_v
    pct = (change / prev_v * 100) if prev_v else 0
    item = {
        "id": id_, "group": group, "name": name, "unit": unit, "decimals": decimals,
        "value": round(last_v, decimals), "change": round(change, decimals) + 0.0,
        "change_pct": round(pct, 2) + 0.0, "date": last_d,
        "history": [{"d": d, "v": round(v, decimals)} for d, v in history[-HISTORY_DAYS:]],
        "source": source,
    }
    if note:
        item["note"] = note
    return item


def fetch_yahoo(id_, group, name, ticker, unit, decimals, scale):
    df = yf.Ticker(ticker).history(period="6mo", interval="1d", auto_adjust=False)
    closes = df["Close"].dropna()
    if closes.empty:
        raise ValueError(f"{ticker}: 데이터 없음")
    history = [(i.strftime("%Y-%m-%d"), float(v) * scale) for i, v in closes.items()]
    return make_item(id_, group, name, unit, decimals, history, f"Yahoo Finance ({ticker})")


def fetch_crypto(id_, name, upbit_market, yahoo_ticker):
    try:
        r = requests.get("https://api.upbit.com/v1/candles/days",
                         params={"market": upbit_market, "count": HISTORY_DAYS}, headers=UA, timeout=20)
        r.raise_for_status()
        rows = sorted(r.json(), key=lambda c: c["candle_date_time_kst"])
        history = [(c["candle_date_time_kst"][:10], float(c["trade_price"])) for c in rows]
        dec = 0 if history[-1][1] >= 100 else 2
        return make_item(id_, "가상자산", name, "원", dec, history, "업비트 원화 시세",
                         note="24시간 거래, 오늘 값은 실시간 가격")
    except Exception:
        item = fetch_yahoo(id_, "가상자산", name, yahoo_ticker, "원", 0, 1)
        item["source"] += " · 업비트 연결 실패로 해외 시세 원화 환산"
        return item


def fred_series(series_id):
    r = requests.get(FRED_CSV.format(series_id), headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for row in csv.reader(io.StringIO(r.text)):
        if len(row) < 2 or not re.match(r"\d{4}-\d{2}-\d{2}", row[0]):
            continue  # 헤더나 빈 값(".") 건너뛰기
        try:
            out.append((row[0], float(row[1])))
        except ValueError:
            pass
    if not out:
        raise ValueError(f"FRED {series_id}: 데이터 없음")
    return out


def fetch_us_macro():
    items = []
    src = "FRED (세인트루이스 연준)"
    jobs = {
        "fed": lambda: make_item("fed", "미국 경제", "미국 기준금리 (상단)", "%", 2,
                                 fred_series("DFEDTARU"), src, note="연준 정책금리 목표 범위 상단"),
        "ust10": lambda: make_item("ust10", "미국 경제", "미국 10년물 국채금리", "%", 2,
                                   fred_series("DGS10"), src),
        "cpi": lambda: cpi_yoy(src),
        "unrate": lambda: make_item("unrate", "미국 경제", "미국 실업률", "%", 1,
                                    fred_series("UNRATE"), src, note="매월 고용보고서 발표"),
        "payems": lambda: payrolls(src),
    }
    errors = []
    for key, job in jobs.items():
        try:
            items.append(job())
        except Exception as e:
            errors.append(f"미국 지표 {key}: {e}")
    return items, errors


def cpi_yoy(src):
    s = fred_series("CPIAUCSL")
    yoy = [(s[i][0], (s[i][1] / s[i - 12][1] - 1) * 100) for i in range(12, len(s))]
    return make_item("cpi", "미국 경제", "미국 소비자물가 (전년 대비)", "%", 1, yoy, src,
                     note="CPI 상승률, 월별 발표 (약 2주 지연)")


def payrolls(src):
    s = fred_series("PAYEMS")
    diff = [(s[i][0], s[i][1] - s[i - 1][1]) for i in range(1, len(s))]
    return make_item("payems", "미국 경제", "미국 비농업 고용 증감", "천 명", 0, diff, src,
                     note="전월 대비 일자리 증가 수, 이후 수정될 수 있음")


def fetch_krx_gold(api_key):
    params = {"serviceKey": api_key, "resultType": "json", "numOfRows": 300, "pageNo": 1}
    r = requests.get(GOLD_API, params=params, timeout=20)
    r.raise_for_status()
    items = r.json()["response"]["body"]["items"]["item"]
    rows = sorted([it for it in items if "1kg" in it.get("itmsNm", "").lower()], key=lambda it: it["basDt"])
    history = [(f'{it["basDt"][:4]}-{it["basDt"][4:6]}-{it["basDt"][6:]}', float(it["clpr"])) for it in rows]
    if not history:
        raise ValueError("KRX 금 데이터 없음")
    return make_item("gold_krx", "금", "국내 금 (KRX 1g)", "원/g", 0, history,
                     "KRX 금시장, 공공데이터포털 (1~2영업일 지연)")


def gold_krw_estimate(gold, usd):
    fx = {h["d"]: h["v"] for h in usd["history"]}
    history = [(h["d"], h["v"] * fx[h["d"]] / 31.1035) for h in gold["history"] if h["d"] in fx]
    return make_item("gold_krw_est", "금", "국제 금 원화 환산 1g", "원/g", 0, history,
                     "국제 금 × 원/달러 ÷ 31.1035 (계산값)")


def update_market():
    path = DATA / "market.json"
    old = {}
    if path.exists():
        old = {it["id"]: it for it in json.loads(path.read_text(encoding="utf-8")).get("items", [])}
    items, errors = {}, []

    def keep_old(id_, msg):
        errors.append(msg)
        if id_ in old:
            items[id_] = old[id_]

    for t in TICKERS:
        try:
            items[t[0]] = fetch_yahoo(*t)
        except Exception as e:
            keep_old(t[0], f"{t[2]}: {e}")

    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    try:
        if key:
            items["gold_krx"] = fetch_krx_gold(key)
        elif "gold_intl" in items and "usdkrw" in items:
            items["gold_krw_est"] = gold_krw_estimate(items["gold_intl"], items["usdkrw"])
    except Exception as e:
        keep_old("gold_krx", f"국내 금: {e}")

    for c in CRYPTO:
        try:
            items[c[0]] = fetch_crypto(*c)
        except Exception as e:
            keep_old(c[0], f"{c[1]}: {e}")

    macro, macro_err = fetch_us_macro()
    for m in macro:
        items[m["id"]] = m
    for e in macro_err:
        keep_old(e.split()[2].rstrip(":"), e)

    order = ["kospi", "kosdaq", "nasdaq", "sp500", "gold_intl", "gold_krx", "gold_krw_est",
             "usdkrw", "jpykrw", "eurkrw", "btc", "eth", "xrp",
             "fed", "ust10", "cpi", "unrate", "payems"]
    out = {"updated_at": now_kst(), "errors": errors, "items": [items[i] for i in order if i in items]}
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[시세] {len(out['items'])}개 항목, 오류 {len(errors)}건")
    for e in errors:
        print("  -", e)


# ───────────────────────── 2. 뉴스 ─────────────────────────
# 구글 뉴스 검색 RSS (키 필요 없음). 제목·언론사·날짜·링크만 저장하고 본문은 원문 링크로 봅니다.
# 검색어를 바꾸고 싶으면 아래 queries 만 고치면 됩니다.
TOPICS = [
    {"id": "sapa", "section": "법·노동", "title": "중대재해처벌법",
     "queries": ["중대재해처벌법", "중대재해법 판결", "중대재해처벌법 개정"]},
    {"id": "yellow", "section": "법·노동", "title": "노란봉투법 (노조법 2·3조)",
     "queries": ["노란봉투법", "노조법 2조 3조 개정", "노란봉투법 판결"]},
    {"id": "ltcg", "section": "세금", "title": "양도세 · 장기보유특별공제 개편",
     "queries": ["장기보유특별공제 한도 10억", "장기거주소득공제", "양도세 개편 국회", "장특공제 개편"]},
    {"id": "fed", "section": "미국·국제", "title": "연준 · 미국 금리",
     "queries": ["연준 기준금리", "FOMC 금리"]},
    {"id": "usdata", "section": "미국·국제", "title": "미국 물가 · 고용",
     "queries": ["미국 소비자물가지수", "미국 고용지표 비농업"]},
    {"id": "crypto", "section": "미국·국제", "title": "가상자산",
     "queries": ["비트코인 시세", "이더리움", "리플 XRP"]},
    {"id": "trump", "section": "미국·국제", "title": "트럼프",
     "queries": ["트럼프 관세", "트럼프 행정부"]},
    {"id": "war", "section": "미국·국제", "title": "전쟁 · 국제 분쟁",
     "queries": ["전쟁 휴전", "중동 전쟁", "우크라이나 전쟁"]},
]
PER_TOPIC = 12
DAYS = 7

# 제목에 이런 단어가 있으면 꼬리표를 붙입니다 (자동 분류라 틀릴 수 있음)
TAGS = [
    ("판결", r"판결|선고|대법원|판례|유죄|무죄|징역|집행유예|1심|2심|항소|상고|기소|헌법재판"),
    ("법 개정", r"개정|개정안|입법|발의|시행령|법안|상임위|소위|입법예고|세법"),
    ("확정·통과", r"국회 통과|본회의|가결|의결|확정|공포|시행"),
    ("10억 한도", r"10억"),
]


def parse_rss(xml_text):
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(KST)
        except Exception:
            continue
        out.append({"title": title, "source": source, "link": (it.findtext("link") or "").strip(),
                    "published": pub.strftime("%Y-%m-%d %H:%M")})
    return out


def tag_title(title):
    return [name for name, pat in TAGS if re.search(pat, title)]


def norm(title):
    return re.sub(r"[\s\W_]+", "", title)[:40]


def fetch_topic(topic):
    seen, items = set(), []
    for q in topic["queries"]:
        url = ("https://news.google.com/rss/search?q=" + quote(f"{q} when:{DAYS}d")
               + "&hl=ko&gl=KR&ceid=KR:ko")
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        for n in parse_rss(r.text):
            k = norm(n["title"])
            if k in seen:
                continue
            seen.add(k)
            n["tags"] = tag_title(n["title"])
            items.append(n)
    items.sort(key=lambda n: n["published"], reverse=True)
    return items[:PER_TOPIC]


def update_news():
    path = DATA / "news.json"
    old = {}
    if path.exists():
        old = {t["id"]: t for t in json.loads(path.read_text(encoding="utf-8")).get("topics", [])}
    topics, errors = [], []
    for t in TOPICS:
        try:
            items = fetch_topic(t)
        except Exception as e:
            errors.append(f"{t['title']}: {e}")
            items = old.get(t["id"], {}).get("items", [])
        topics.append({"id": t["id"], "section": t["section"], "title": t["title"],
                       "queries": t["queries"], "items": items})

    # 장특공제 '10억 한도' 관련 확정 신호: 제목에 10억 + 확정·통과 꼬리표가 함께 있는 기사
    ltcg = next(t for t in topics if t["id"] == "ltcg")["items"]
    signals = [n for n in ltcg if "10억 한도" in n["tags"] and "확정·통과" in n["tags"]]
    out = {"updated_at": now_kst(), "errors": errors, "topics": topics,
           "ltcg_signal": {"count": len(signals), "items": signals[:5]}}
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[뉴스] {sum(len(t['items']) for t in topics)}건, 오류 {len(errors)}건")
    for e in errors:
        print("  -", e)


def now_kst():
    return dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    update_market()
    update_news()
