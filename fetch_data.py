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
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

KST = dt.timezone(dt.timedelta(hours=9))
NY = ZoneInfo("America/New_York")
WEEK = "월화수목금토일"

# 미국 시장 항목: 뉴욕 시간과 한국 시간을 함께 표시
#   "close"    = 뉴욕 정규장 종가(16:00) 기준
#   "intraday" = 거의 24시간 거래 → 마지막 체결 시각
US_TIME = {"nasdaq": "close", "sp500": "close",
           "es_fut": "intraday", "nq_fut": "intraday", "gold_intl": "intraday"}
# 미국 경제지표 발표 시각 (뉴욕 시간)
US_RELEASE = {"fed": ("FOMC 결정", 14, 0), "cpi": ("발표", 8, 30),
              "unrate": ("발표", 8, 30), "payems": ("발표", 8, 30)}


def two_clocks(t):
    """뉴욕 시각 t(aware) → ('10/16(금) 16:00', '10/17(토) 05:00')"""
    f = lambda x: f"{x.month}/{x.day}({WEEK[x.weekday()]}) {x:%H:%M}"
    return f(t.astimezone(NY)), f(t.astimezone(KST))


def add_us_time(item, ticker):
    kind = US_TIME.get(item["id"])
    if not kind:
        return
    try:
        if kind == "close":
            d = dt.date.fromisoformat(item["date"])
            t = dt.datetime.combine(d, dt.time(16, 0), NY)
            item["time_label"] = "뉴욕 종가"
        else:
            bars = yf.Ticker(ticker).history(period="5d", interval="5m")
            t = bars.index[-1].to_pydatetime()
            if t.tzinfo is None:
                t = t.replace(tzinfo=NY)
            item["time_label"] = "최근 체결"
        item["time_us"], item["time_kr"] = two_clocks(t)
    except Exception as e:
        print(f"  ({item['name']} 시각 확인 실패: {e})")


def add_release_time(item):
    rel = US_RELEASE.get(item["id"])
    if not rel:
        return
    label, h, m = rel
    now_ny = dt.datetime.now(NY)
    t = now_ny.replace(hour=h, minute=m, second=0, microsecond=0)
    kr = t.astimezone(KST)
    next_day = " (다음 날)" if kr.date() > t.date() else ""
    item["release"] = f"{label} 뉴욕 {t:%H:%M} = 한국 {kr:%H:%M}{next_day}"
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
    ("es_fut", "선물", "S&P 500 선물 (E-mini)", "ES=F", "pt", 2, 1),
    ("nq_fut", "선물", "나스닥 100 선물 (E-mini)", "NQ=F", "pt", 2, 1),
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

# 국내 지수 선물: 공공데이터포털 '금융위원회_파생상품시세정보' (하루 1회 갱신, 1영업일 지연)
FUTURES_API = "https://apis.data.go.kr/1160100/service/GetDerivativeProductInfoService/getFuturesPriceInfo"
KR_FUTURES = [
    # (id, 이름, 검색어, 종목명 패턴, 빼야 할 종목명 패턴)
    ("k200_fut", "코스피200 선물 (최근월물)", "코스피200", r"코스피\s?200", r"미니|옵션|변동성|섹터|위클리|ESG|스프레드|SP"),
    ("kq150_fut", "코스닥150 선물 (최근월물)", "코스닥150", r"코스닥\s?150", r"미니|옵션|섹터|스프레드|SP"),
]


def pick(d, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def fetch_kr_future(api_key, id_, name, keyword, include, exclude):
    begin = (dt.datetime.now(KST) - dt.timedelta(days=90)).strftime("%Y%m%d")
    rows, page = [], 1
    while page <= 10:
        params = {"serviceKey": api_key, "resultType": "json", "numOfRows": 1000, "pageNo": page,
                  "beginBasDt": begin, "likeItmsNm": keyword}
        r = requests.get(FUTURES_API, params=params, timeout=30)
        r.raise_for_status()
        body = r.json()["response"]["body"]
        items = body.get("items", {}).get("item", [])
        if isinstance(items, dict):
            items = [items]
        rows += items
        if len(rows) >= int(body.get("totalCount", 0)) or not items:
            break
        page += 1
    if rows and page == 1:
        print(f"  ({name} 응답 항목 예시: {list(rows[0].keys())})")
    by_day = {}
    for it in rows:
        nm = str(pick(it, "itmsNm", "isuNm", "prdNm") or "")
        if not re.search(include, nm) or re.search(exclude, nm):
            continue
        day = str(pick(it, "basDt") or "")
        price = pick(it, "clpr", "clsprc", "stlPrc")
        vol = float(pick(it, "trqu", "accTrdvol", "trdvol") or 0)
        if not day or price is None:
            continue
        # 같은 날 여러 만기월이 있으면 거래량이 가장 많은 종목(보통 최근월물)을 고름
        if day not in by_day or vol > by_day[day][1]:
            by_day[day] = (float(price), vol)
    if not by_day:
        raise ValueError(f"{name}: 조건에 맞는 종목 없음 (받은 행 {len(rows)}개)")
    history = [(f"{d[:4]}-{d[4:6]}-{d[6:]}", v[0]) for d, v in sorted(by_day.items())]
    return make_item(id_, "선물", name, "pt", 2, history, "한국거래소, 공공데이터포털 (1영업일 지연)",
                     note="정규장 종가 기준, 야간 거래 미반영")


def make_item(id_, group, name, unit, decimals, history, source, note=None, no_pct=False):
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
    if no_pct:
        item["no_pct"] = True  # 변화량 자체를 보여주는 지표라 퍼센트는 의미 없음
    return item


def fetch_yahoo(id_, group, name, ticker, unit, decimals, scale):
    df = yf.Ticker(ticker).history(period="6mo", interval="1d", auto_adjust=False)
    closes = df["Close"].dropna()
    if closes.empty:
        raise ValueError(f"{ticker}: 데이터 없음")
    history = [(i.strftime("%Y-%m-%d"), float(v) * scale) for i, v in closes.items()]
    item = make_item(id_, group, name, unit, decimals, history, f"Yahoo Finance ({ticker})")
    add_us_time(item, ticker)
    return item


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
                     note="전월 대비 일자리 증가 수, 이후 수정될 수 있음", no_pct=True)


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

    if key:
        for f in KR_FUTURES:
            try:
                items[f[0]] = fetch_kr_future(key, *f)
            except Exception as e:
                keep_old(f[0], f"{f[1]}: {e}")

    for c in CRYPTO:
        try:
            items[c[0]] = fetch_crypto(*c)
        except Exception as e:
            keep_old(c[0], f"{c[1]}: {e}")

    macro, macro_err = fetch_us_macro()
    for m in macro:
        add_release_time(m)
        items[m["id"]] = m
    for e in macro_err:
        keep_old(e.split()[2].rstrip(":"), e)

    order = ["kospi", "kosdaq", "nasdaq", "sp500", "k200_fut", "kq150_fut", "es_fut", "nq_fut", "gold_intl", "gold_krx", "gold_krw_est",
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
     "queries": ["중대재해처벌법", "중대재해법 판결", "중대재해처벌법 개정"],
     "must": r"중대재해"},
    {"id": "yellow", "section": "법·노동", "title": "노란봉투법 (노조법 2·3조)",
     "queries": ["노란봉투법", "노조법 2조 3조 개정", "노란봉투법 판결"],
     "must": r"노란봉투|노조법|노동조합법|원청|하청|교섭"},
    {"id": "ltcg", "section": "세금", "title": "양도세 · 장기보유특별공제 개편",
     "queries": ["장기보유특별공제 한도 10억", "장기거주소득공제", "양도세 개편 국회", "장특공제 개편"],
     "must": r"양도세|양도소득세|장기보유|장특|장기거주|세제개편|세법|부동산 세제"},
    {"id": "fed", "section": "미국·국제", "title": "연준 · 미국 금리",
     "queries": ["연준 기준금리", "FOMC 금리"],
     "must": r"연준|금리|FOMC|Fed|파월|워시"},
    {"id": "usdata", "section": "미국·국제", "title": "미국 물가 · 고용",
     "queries": ["미국 소비자물가지수", "미국 고용지표 비농업"],
     "must": r"물가|CPI|PCE|인플레|고용|일자리|실업"},
    {"id": "crypto", "section": "미국·국제", "title": "가상자산",
     "queries": ["비트코인 시세", "이더리움", "리플 XRP"],
     "must": r"비트코인|이더리움|리플|XRP|가상자산|암호화폐|코인"},
    {"id": "trump", "section": "미국·국제", "title": "트럼프",
     "queries": ["트럼프 관세", "트럼프 행정부"],
     "must": r"트럼프"},
    {"id": "war", "section": "미국·국제", "title": "전쟁 · 국제 분쟁",
     "queries": ["전쟁 휴전", "중동 전쟁", "우크라이나 전쟁"],
     "must": r"전쟁|휴전|분쟁|공습|교전|침공|미사일|폭격|평화협상|종전"},
]

# 제목에 이런 단어가 있으면 스팸·광고로 보고 뺍니다. 이상한 기사가 또 보이면 여기에 단어를 추가하세요.
BLOCK_WORDS = r"카지노|바카라|슬롯머신|토토사이트|먹튀|보너스 코드|프로모션 코드"
# 믿기 어려운 출처(언론사 이름)를 여기에 추가하면 그 출처 기사는 모두 빠집니다.
BLOCK_SOURCES = ["Calgary Roughnecks"]
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
            if not re.search(topic["must"], n["title"], re.I):
                continue  # 주제와 관계없는 기사
            if re.search(BLOCK_WORDS, n["title"]) or n["source"] in BLOCK_SOURCES:
                continue  # 스팸·광고
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
