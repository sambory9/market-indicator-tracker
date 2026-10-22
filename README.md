# market-indicator-tracker

알상무 '지표추적자' 방송에서 보는 지표를 매일 수집한다. Claude가 `data/latest.json`을 읽고 일간 리포트를 작성한다.

## 수집 지표

| 섹터 | 지표 | 출처 |
|---|---|---|
| 지수 | 코스피, 코스닥, S&P500, 나스닥, 다우, 러셀2000, VIX | 야후 |
| 종목 | 삼성전자, SK하이닉스, 엔비디아, 오라클 | 야후 |
| 환율 | 원/달러, 달러/엔, 달러지수 | 야후 |
| 원자재/코인 | 금, WTI, 비트코인 (MACD·RSI 포함) | 야후 |
| 금리 | 미국채 2·10년 / 국고채 3·10년 | FRED / 한국은행 ECOS |
| 수급 | 코스피·코스닥 개인·외국인·기관·연기금·투신 순매수 | pykrx(KRX) |

계산 항목은 1·5·20일 등락, 20/60/120일 이동평균 위치와 돌파·이탈, RSI14, 52주 고점 대비 위치다. 금리 차(10-2년, 10-3년)와 수급 연속일수도 계산한다.

## 설정

1. Settings → Secrets and variables → Actions → New repository secret
   - 이름 `ECOS_API_KEY`, 값: 한국은행 ECOS 인증키
2. Actions 탭 → `collect` → **Run workflow**로 첫 실행 (과거 데이터 수집)
3. 이후 평일 16:10 KST에 자동 실행

## 파일

- `data/prices/*.csv` — 지표별 일간 OHLC
- `data/flows/*.csv` — 주체별 순매수 (억원)
- `data/latest.json` — 최신값과 신호 (리포트 입력)
