"""
매일 시세를 가져와 data/market.json 에 저장하는 스크립트.
GitHub Actions가 평일마다 자동으로 실행합니다.
"""
import json
import os
import datetime as dt
from pathlib import Path

import requests
import yfinance as yf

KST = dt.timezone(dt.timedelta(hours=9))
OUT = Path(__file__).parent / "data" / "market.json"
HISTORY_DAYS = 60  # 추이 그래프에 쓸 최근 거래일 수

# (id, 그룹, 이름, 야후 파이낸스 티커, 단위, 소수점, 배율)
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

GOLD_API = "https://apis.data.go.kr/1160100/service/GetGeneralProductInfoService/getGoldPriceInfo"


def make_item(id_, group, name, unit, decimals, history, source):
    """history: [(날짜문자열, 값), ...] 오래된 것부터"""
    last_d, last_v = history[-1]
    prev_v = history[-2][1] if len(history) > 1 else last_v
    change = last_v - prev_v
    pct = (change / prev_v * 100) if prev_v else 0
    return {
        "id": id_, "group": group, "name": name, "unit": unit,
        "decimals": decimals,
        "value": round(last_v, decimals),
        "change": round(change, decimals),
        "change_pct": round(pct, 2),
        "date": last_d,
        "history": [{"d": d, "v": round(v, decimals)} for d, v in history[-HISTORY_DAYS:]],
        "source": source,
    }


def fetch_yahoo(id_, group, name, ticker, unit, decimals, scale):
    df = yf.Ticker(ticker).history(period="6mo", interval="1d", auto_adjust=False)
    closes = df["Close"].dropna()
    if closes.empty:
        raise ValueError(f"{ticker}: 데이터 없음")
    history = [(idx.strftime("%Y-%m-%d"), float(v) * scale) for idx, v in closes.items()]
    return make_item(id_, group, name, unit, decimals, history, f"Yahoo Finance ({ticker})")


def fetch_krx_gold(api_key):
    """공공데이터포털 '금융위원회_일반상품시세정보'에서 KRX 금 99.99_1kg 종가를 가져옴 (1~2영업일 지연)"""
    params = {"serviceKey": api_key, "resultType": "json", "numOfRows": 300, "pageNo": 1}
    r = requests.get(GOLD_API, params=params, timeout=20)
    r.raise_for_status()
    items = r.json()["response"]["body"]["items"]["item"]
    rows = [it for it in items if "1kg" in it.get("itmsNm", "").lower()]
    rows.sort(key=lambda it: it["basDt"])
    history = [(f'{it["basDt"][:4]}-{it["basDt"][4:6]}-{it["basDt"][6:]}', float(it["clpr"])) for it in rows]
    if not history:
        raise ValueError("KRX 금 데이터 없음")
    return make_item("gold_krx", "금", "국내 금 (KRX 1g)", "원/g", 0, history,
                     "KRX 금시장, 공공데이터포털 (1~2영업일 지연)")


def gold_krw_estimate(gold, usd):
    """API 키가 없을 때: 국제 금값을 원화 1g 가격으로 환산 (같은 날짜끼리 계산)"""
    fx = {h["d"]: h["v"] for h in usd["history"]}
    history = [(h["d"], h["v"] * fx[h["d"]] / 31.1035) for h in gold["history"] if h["d"] in fx]
    return make_item("gold_krw_est", "금", "국제 금 원화 환산 1g", "원/g", 0, history,
                     "국제 금 × 원/달러 ÷ 31.1035 (계산값)")


def main():
    old = {}
    if OUT.exists():
        old = {it["id"]: it for it in json.loads(OUT.read_text(encoding="utf-8")).get("items", [])}

    items, errors = {}, []
    for t in TICKERS:
        try:
            items[t[0]] = fetch_yahoo(*t)
        except Exception as e:  # 하나가 실패해도 나머지는 계속
            errors.append(f"{t[2]}: {e}")
            if t[0] in old:
                items[t[0]] = old[t[0]]  # 어제 값 유지

    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    try:
        if key:
            items["gold_krx"] = fetch_krx_gold(key)
        elif "gold_intl" in items and "usdkrw" in items:
            items["gold_krw_est"] = gold_krw_estimate(items["gold_intl"], items["usdkrw"])
    except Exception as e:
        errors.append(f"국내 금: {e}")
        if "gold_krx" in old:
            items["gold_krx"] = old["gold_krx"]

    order = ["kospi", "kosdaq", "nasdaq", "sp500", "gold_intl", "gold_krx", "gold_krw_est",
             "usdkrw", "jpykrw", "eurkrw"]
    out = {
        "updated_at": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "errors": errors,
        "items": [items[i] for i in order if i in items],
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장 완료: {len(out['items'])}개 항목, 오류 {len(errors)}건")
    for e in errors:
        print(" -", e)


if __name__ == "__main__":
    main()
