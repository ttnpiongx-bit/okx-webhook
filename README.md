# okx-webhook

TradingView 訊號驅動的 OKX 永續合約自動交易系統。

從 TradingView 指標發出警報，經由本機 Flask 服務接收與驗證，
直接在 OKX 下單並自動掛好停損與分批停利。

## 系統架構
TradingView 指標
│ alert() 觸發，訊息含進場方向與停損價位
▼
webhook (HTTPS) ← cloudflared 隧道
▼
本機 Flask 服務 ── 控制台（部位狀態、下單紀錄）
│ ccxt
▼
OKX API
├─ 市價開倉
├─ 停損單（獨立掛出）
└─ 三張 reduce-only 限價停利單

**技術組成**

| 層 | 使用 |
|---|---|
| 訊號來源 | TradingView（FVG / iFVG / BOS 結構偵測） |
| 傳輸 | cloudflared 隧道 + 路徑密鑰驗證 |
| 服務端 | Python + Flask |
| 交易所介接 | ccxt |
| 交易標的 | XAU/USDT 永續合約（OKX） |

## 交易邏輯

**進場**
- 由 TradingView 訊號觸發
- 固定開倉價值，不依帳戶餘額浮動
- 同時只持有一單，有部位時忽略新訊號

**出場**
- 三段停利，各平 1/3
- 開倉時就把停利單掛到交易所端，不依賴本機程式持續運行
- 停利 1 成交後 → 停損移到開倉價（保本）
- 停利 2 成交後 → 停損移到停利 1 的價位

## 踩過的坑

這一段是這個專案最有價值的部分，都是實單上撞出來的。

**1. TradingView 的 alert 訊息欄位不會送出**

使用「任何 alert() 函數呼叫」時，webhook 送出的是指標自己產生的文字，
訊息欄位填的內容不會送出。
→ 密鑰必須放在網址路徑：`/hook-<密鑰>`

**2. OKX 雙向持倉模式下，止損不能綁在市價單上**

停損參數附加在市價開倉單上會被 `51278` 退回**整筆委託**。
→ 必須先開倉，成交後再單獨掛停損。

**3. Windows 上 cloudflared 要指定 IPv4**

`--url http://localhost:8081` 會走 IPv6 連不到，要用 `127.0.0.1`。

**4. TradingView webhook 只等 3 秒**

下單流程若同步執行會超時。
→ 收到訊號立即回 200，下單邏輯丟背景執行緒處理。

## 安裝

```bash
pip install -r requirements.txt
```

設定寫在 `my_config.py`（已列入 .gitignore，不會提交）：

```python
OKX_API_KEY = ""
OKX_API_SECRET = ""
OKX_API_PASSWORD = ""
WEBHOOK_SECRET = ""
```

啟動：

```bash
python okx_webhook_bot.py
cloudflared tunnel --url http://127.0.0.1:8081
```

## 狀態

2026 年 8 月起以小額實單運行中，仍在調整。

## ⚠️ 免責聲明

本專案為個人技術實作與學習紀錄。

- 不構成任何投資建議或買賣推薦
- 不保證獲利，歷史結果不代表未來績效
- 高槓桿合約交易風險極高，可能導致本金全部損失
- 使用本專案程式碼產生之交易結果由使用者自行承擔
- 作者非證券投資顧問事業，不提供投資顧問服務

## 授權

MIT
