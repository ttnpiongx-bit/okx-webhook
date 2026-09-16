# -*- coding: utf-8 -*-
"""
複製成 my_config.py 再填（my_config.py 已在 .gitignore，不會被提交）。
這裡的任何大寫變數都會覆蓋 okx_webhook_bot.py 裡的預設值。

Copy to my_config.py and fill in. Any UPPERCASE name here overrides the
default in okx_webhook_bot.py. my_config.py is git-ignored.
"""

# ---- OKX API（在 OKX 後台建立，建議只勾「交易」權限並綁 IP 白名單）----
OKX_API_KEY      = ""
OKX_API_SECRET   = ""
OKX_API_PASSWORD = ""

USE_DEMO = True       # True = OKX 模擬盤   False = 正式實盤
DRY_RUN  = True       # True = 只記錄不下單。第一輪一定保持 True

# ---- Webhook ----
# 密鑰要放進網址路徑：TradingView alert() 模式不會送出訊息欄位的內容
WEBHOOK_SECRET = "change-me-to-a-long-random-string"
WEBHOOK_PATH   = "/hook-" + WEBHOOK_SECRET
PORT           = 8081

# ---- 商品與倉位 ----
SYMBOL   = "XAU/USDT:USDT"
TD_MODE  = "cross"
LEVERAGE = 100

POSITION_SIZING = "order_value"   # 每單固定開倉價值
ORDER_VALUE     = 1000.0          # USDT（100 倍槓桿 → 保證金約 10 USDT）
MAX_NOTIONAL    = 0               # 0 = 不設上限（order_value 模式金額本來就寫死）

# ---- 出場 ----
TP_SPLITS      = 3
TP_ON_EXCHANGE = True
SL_AFTER_TP1   = "entry"
SL_AFTER_TP2   = "tp1"

# ---- 控制台 ----
ALLOW_REMOTE_DASHBOARD = False    # 隧道是公開的，日誌裡有密鑰，別開
