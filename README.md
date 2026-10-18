# 오늘의 시장 시세

코스피, 코스닥, 나스닥, S&P 500, 국제/국내 금, 환율을 평일마다 자동으로 업데이트해서 보여주는 웹페이지입니다.

## 구조

- `fetch_data.py`: 시세를 가져와 `data/market.json`에 저장하는 스크립트
- `.github/workflows/update-data.yml`: 평일 15:40, 06:30(한국 시간)에 스크립트를 자동 실행
- `index.html`: `data/market.json`을 읽어 화면에 보여주는 페이지

## 설정 순서

1. GitHub에서 새 저장소(Public)를 만들고, 이 폴더의 파일을 모두 업로드합니다.
   (`.github` 폴더도 꼭 함께 올려야 자동 실행이 됩니다.)
2. Settings → Pages → Source를 "Deploy from a branch", Branch를 `main` / `(root)`로 저장합니다.
3. Actions 탭 → "시세 업데이트" → "Run workflow"로 한 번 실행해 봅니다.
4. 몇 분 뒤 `https://<내아이디>.github.io/<저장소이름>/` 주소로 접속합니다.

## (선택) 국내 금 KRX 시세

API 키가 없으면 국내 금 대신 "국제 금 원화 환산 1g"을 계산해서 보여줍니다.
KRX 금시장 실제 가격을 보려면:

1. 공공데이터포털(data.go.kr)에서 "금융위원회_일반상품시세정보" 활용 신청
2. 발급받은 일반 인증키(Decoding)를 복사
3. 저장소 Settings → Secrets and variables → Actions → New repository secret
   이름 `DATA_GO_KR_KEY`, 값에 인증키 붙여넣기

이 데이터는 1~2영업일 늦게 반영됩니다.

## 참고

- 시세는 Yahoo Finance 데이터를 사용하며, 증권사 시세나 서울 외환시장 종가와 조금 다를 수 있습니다.
- GitHub의 예약 실행은 몇 분에서 수십 분까지 늦어질 수 있습니다.
- 저장소에 60일 넘게 활동이 없으면 GitHub이 예약 실행을 멈출 수 있습니다. 이 경우 Actions 탭에서 다시 켜면 됩니다.
