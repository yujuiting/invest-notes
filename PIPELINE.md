# 處理流程（PIPELINE）

所有資料只在這台機器上處理，不上傳、不對外送出。**不要印出 OPENAI_API_KEY**（不要 `echo`、`env`、`printenv` 把它顯示出來）。

## 目錄
```
tw-stock/
├── gooaye.xml / yu.xml          # 節目 RSS
├── state.json                   # 已處理集數（guid）
├── glossary.txt                 # 專有名詞、已知誤聽對照
├── transcripts/<節目>/           # 逐字稿（.raw.txt 為未校正原稿）
├── notes/                       # 每集筆記；_backup/ 放改版前的舊檔
├── knowhow/                     # 知識庫卡片，索引見 knowhow/README.md
├── predictions/
│   ├── predictions.jsonl        # 預測（一行一筆）
│   ├── score.py                 # 補基準、計分、產生計分板
│   ├── scoreboard.md            # 自動產生，勿手改
│   └── cache/                   # 行情快取
└── PIPELINE.md
```

## 每集流程
1. **抓新集數**：比對 RSS 與 `state.json`，只處理新 guid。
2. **逐字稿**：轉寫後存 `transcripts/<節目>/YYYY-MM-DD_<集名>.raw.txt`；依 `glossary.txt` 修正已知誤聽，不確定的列入「待確認」，不自動替換。逐字稿每 5 分鐘一個 `[HH:MM:SS]` 標記。
3. **筆記**：依下方格式寫 `notes/<節目>_YYYY-MM-DD_<集名>.md`。
4. **knowhow**：把主持人講出口的判斷邏輯併入 `knowhow/`（規則見 `knowhow/README.md`）。同一邏輯再出現就 +1 提及次數，不要重複新增。
5. **預測**：依下方「預測規則」新增或修訂（revision），再執行 `python3 predictions/score.py`。
6. **更新 `state.json`**。

## 筆記格式
第一行：`《節目名》EPxxx｜YYYY/MM/DD（週）集名`（無集數編號就註明）。

以下各節**只寫有內容的節**；本集沒有內容的節**整節省略**，不要寫「本集未提及」。每條後面附「約 X–Y 分」。

```markdown
## 大盤看法
- **台股**：……
- **美股**：……（指數收盤、漲跌、主持人怎麼看）
- **國際情勢**：……（聯準會／利率、油價、匯率、地緣政治、美債等）
## 點名的族群和個股（附代號）
- 台股附四碼代號、美股附 ticker；主持人沒點名個股就寫族群並註明「未點名個股」。
## 主持人的操作
- 只寫主持人自己講出口的買、賣、停損、參與 IPO、刻意避開等。
## 交易規則
- 講出口的規則；條件可量化者標【可回測】，原則類註明「無法回測」。
## 看法變化
- 和同一主持人前幾集比，哪裡改口、哪裡加碼重申。
## 新增 knowhow
- 新增或 +1 的條目代碼。
## 預測更新
- 新增、修訂或因此集內容受影響的預測 id。
```
- 「大盤看法」三個子項也一樣：沒講到的子項就省略（例如本集完全沒談台股就不寫「台股」那行）。
- 整理者自己的補充（例如查到的收盤數字）要標「（整理者註）」。

## 預測規則
### 市場與標的
| market | target（預測檔寫法） | kind | 資料來源 |
|---|---|---|---|
| TW | 加權指數 | direction（漲／跌） | TWSE MI_INDEX「發行量加權股價指數」 |
| TW | 證交所類股指數名稱，如「半導體類指數」「汽車類指數」 | relative（強於大盤／弱於大盤） | TWSE MI_INDEX |
| US | 標普500指數 | direction（漲／跌） | FRED `SP500` |
| US | 那斯達克綜合指數 | direction（漲／跌） | FRED `NASDAQCOM` |
| US | 費城半導體指數 | direction（漲／跌） | Nasdaq API `SOX` |

- 期間 `horizon_trading_days`：5 或 20 個**該市場**的交易日（台股看證交所行事曆，美股看 NYSE 行事曆，含感恩節 11/26 等休市）。
- **同一 market＋target＋期間同時最多一筆 open**；想改看法時不要新增，在 `revisions` 加 `{date, note}`，計分仍只看原始 call。
- 信心：低／中／高。依據薄弱（主持人沒直接講方向、只能間接推）一律給「低」並在 rationale 寫原因。
- rationale 必須引用 knowhow 代碼，`sources` 同時列節目（含約略分鐘）與 knowhow 條目；只能用主持人講過的話。

### 基準日（base_date）
- **台股**：內容發布後的第一個台股交易日收盤（盤後發布 → 下一交易日）。2026-10-29 早上發布的內容 → 2026-10-29。
- **美股**：內容發布（台北時間）之後才收盤的那一個美股交易日（美東日期）；若預測建立日比內容晚，以建立時點之後收盤的美股交易日為準。例：台北 2026-10-29 上午的內容 → 美股 2026-10-29（美東）那一盤，台北時間 10/30 清晨收盤。
- `base_close` 一律先寫 `null`，由 `score.py` 在資料公布後補上；美股資料源約晚一天上架。

### 欄位
`id, created, market, sources, target, kind, horizon_trading_days, call, confidence, rationale, base_date, base_close, [base_taiex_close（relative 才有）], due_date, due_date_estimated, status(open/hit/miss), revisions[]`；計分後由程式補 `base_close_date, result_date, result_close, result_return_pct, [result_taiex_*, result_excess_pct], scored_on, [due_date_note]`。舊資料沒有 `market` 時程式會補成 `"TW"`。

## score.py
```bash
cd /workspace/tw-stock/predictions
python3 score.py               # 補基準、計分、產生 scoreboard.md（可重複跑）
python3 score.py --offline     # 只用快取
python3 score.py --crosscheck  # 另外印出美股收盤雙來源比對（Yahoo Finance、Nasdaq API），不寫檔
```
- **快取**：台股 `cache/MI_INDEX_IND_YYYYMMDD.json`（當天 17:00 前的空資料不快取）、`cache/holidays_YYYY.json`；美股每個指數一個合併歷史檔 `cache/US_<代碼>.json`，只有在需要的日期不在快取時才重抓，且同一指數 60 分鐘內不重抓。
- **臨時休市**（例：2026-10-23 台股停市不在休市表）：計分時以「實際有收盤資料的交易日」往後數，若和原 due_date 不同，改寫 due_date 並記 `due_date_note`。
- **美股未公布 vs 休市**：週末與 NYSE 休市日直接跳過；某天若不在資料裡但資料源已有更晚日期 → 視為臨時休市；若是最新日期之後 → 視為尚未公布，等下次再跑。美東 18:00 前不把當天當成已收盤。
- 計分板分組：總體、市場、標的、期間、市場×期間、信心、來源節目；另列未到期與已計分清單。美股到期日若是提早收盤日（感恩節隔天、平安夜）會加註。

## 資料來源（2026-10-29 實測）
| 用途 | 網址 | 備註 |
|---|---|---|
| 台股指數收盤 | `https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=IND&response=json` | 非交易日 data 為空 |
| 台股休市日 | `https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?response=json&queryYear=115` | 民國年 |
| 標普500 | `https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500&cosd=YYYY-MM-DD` | 用 Python 預設 UA；偽裝瀏覽器 UA 反而會卡住 |
| 那斯達克綜合 | `https://fred.stlouisfed.org/graph/fredgraph.csv?id=NASDAQCOM&cosd=YYYY-MM-DD` | 休市日值為 "." |
| 費城半導體 | `https://api.nasdaq.com/api/quote/SOX/historical?assetclass=index&fromdate=..&todate=..&limit=..` | 需瀏覽器 UA 與 `Accept: application/json`；約晚一天上架 |
| 交叉比對 | Yahoo Finance chart API（^GSPC、^IXIC、^SOX）、Nasdaq API `COMP` | 只用來比對，不用來計分 |
