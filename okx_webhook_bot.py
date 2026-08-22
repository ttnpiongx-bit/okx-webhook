# -*- coding: utf-8 -*-
"""
TradingView Webhook -> OKX 自動下單（三段式止盈 + 移動止損版）
==========================================================

倉位計算：每單名目金額 = 帳戶總權益 × EQUITY_PCT%（每次下單前重新查詢）

出場規則（由指標的止盈通知驅動）：
    止盈1 → 平掉初始倉位的 1/3，止損移到「開倉價」（保本）
    止盈2 → 再平 1/3，止損移到「止盈1 的價位」
    止盈3 → 平掉剩餘全部

設定全部寫在下面 CONFIG 區塊，改完存檔直接執行：python okx_webhook_bot.py
"""
import json, re, time, hmac, threading, traceback
from collections import deque
from datetime import datetime
import ccxt
from flask import Flask, request, jsonify

# ==================== CONFIG 改這裡 ====================
OKX_API_KEY      = "填入你的API_KEY"
OKX_API_SECRET   = "填入你的SECRET"
OKX_API_PASSWORD = "填入你的PASSPHRASE"

USE_DEMO = False          # True=模擬盤  False=正式實盤
DRY_RUN  = True           # True=只記錄不下單(第一輪保持True)  False=真的下單

WEBHOOK_SECRET = "改成你自己的長密鑰abc123XYZ"
# 網址路徑。把密鑰放進路徑是必要的做法 —— TradingView 的「任何 alert() 函數呼叫」
# 只會送出指標自己產生的文字，你在訊息欄位填的東西不會被送出，
# 所以無法靠訊息內容夾帶密鑰（那樣每一則都會被擋成 403）。
# 設成 "/hook-<你的密鑰>"，網址本身就是通行證。
WEBHOOK_PATH   = "/webhook"
PORT           = 8081

SYMBOL   = "XAU/USDT:USDT"
TD_MODE  = "cross"        # cross全倉 / isolated逐倉
LEVERAGE = 3

# ---------- 倉位計算 ----------
# 三選一：
#   "order_value" = 每單固定「開倉價值」。不管本金多少、槓桿幾倍，都用這個價值開倉。
#                   實際佔用的保證金 = 開倉價值 ÷ 槓桿，由交易所那邊決定。
#   "equity_pct"  = 依帳戶權益的百分比計算
#   "fixed"       = 舊名稱，等同 order_value
POSITION_SIZING = "equity_pct"

# POSITION_SIZING="order_value" 時，每單的開倉價值（名目價值）USDT
ORDER_VALUE = 100.0

EQUITY_PCT      = 10.0           # POSITION_SIZING="equity_pct" 時，取總權益的百分之多少

# SIZING_BASIS 決定上面那個 10% 是指什麼，兩者差三倍，務必看清楚：
#   "notional" = 名目價值取10%。權益1000 → 名目100 USDT，佔用保證金約33（3倍槓桿）
#   "margin"   = 保證金取10%。權益1000 → 佔用保證金100，名目300 USDT
# 2026-08-19 改成 margin：權益約 55 USDT 時，notional 基準只換得到 1 張，
# 分不出三批（這個商品最小 1 張、只能整數張）。margin 基準是 3 張，剛好每批 1 張。
SIZING_BASIS = "margin"

FIXED_NOTIONAL = 10.0     # 舊設定名稱，ORDER_VALUE 沒設時才會用到
MIN_NOTIONAL   = 5.0      # 算出來低於這個就不下單
# 絕對上限。防的是「權益算錯」或「訊息被竄改」導致的巨額委託。
# 設 0 = 不設上限（用 order_value 模式時可以這樣，因為金額本來就是你自己寫死的）
MAX_NOTIONAL   = 200.0
EQUITY_CACHE_SEC = 30     # 權益查詢快取秒數

# ---------- 出場規則 ----------
TP_SPLITS      = 3        # 總倉位平均分成幾批出場

# 止盈掛在哪裡：
#   True  = 開倉時就把三張 reduce-only 限價單掛到交易所（預設）
#           → 程式當掉、電腦關機、隧道斷線，止盈照樣成交
#           → 程式只剩「搬移止損」這件事要做
#   False = 舊做法，等 TradingView 的止盈通知進來才由程式市價平倉
#           → 程式沒在跑就完全不會出場
# 需要進場訊息有帶三個止盈價才能用；抓不到價位時會自動退回 False 的行為。
TP_ON_EXCHANGE = True

# 背景監控間隔（秒）。交易所端的止盈成交後，程式靠這個發現部位變小、
# 然後把止損往上搬。不依賴 TradingView 的通知，所以通知漏掉也不影響。
MONITOR_INTERVAL_SEC = 5

# 啟動時（或發現狀態遺失時）要不要從交易所把現有部位的狀態重建回來。
# 程式重啟時如果手上有倉，記憶體裡的紀錄就沒了 —— 掛在交易所的止損止盈仍然有效，
# 但程式不知道開倉價、初始張數，就沒辦法在止盈觸發後搬止損，
# 而且會一直以為「有部位」而擋掉所有新訊號。
# 開著的話會去讀部位與未成交委託，把狀態拼回來。
ADOPT_ON_START = True

# 收到訊號後要不要「先回應、再慢慢做」。
# TradingView 等待 webhook 回應的時間很短，而一次進場要跟 OKX 往返七八次
# （查價、查權益、設槓桿、下單、掛止損、掛三張止盈），常常來不及回應，
# 它的日誌就會顯示 request took too long and timed out。
# True = 立刻回 200，實際動作丟到背景執行緒做（交易照常，只是不讓對方等）
ASYNC_PROCESSING = True

# 只能下整數張的商品（例如 OKX 的 XAU/USDT 永續，最小 1 張、精度 1 張），
# 如果張數不是 TP_SPLITS 的倍數，分批平倉會除不盡、尾批可能小於最小下單量。
# True = 開倉張數自動「往下」調成 TP_SPLITS 的倍數（曝險只會變小，不會變大）
ROUND_TO_SPLITS = True

# 張數不夠分成 TP_SPLITS 批時的處理：
#   True  = 直接拒絕這筆進場（安全，預設）
#   False = 照樣進場，但分批會失敗，實際上只有最後一段止盈會生效
REQUIRE_SPLITTABLE = True
DEFAULT_SL_PCT = 0.5      # 備援止損%。只在訊息裡找不到止損價時才用

# 優先採用訊號自己帶的止損價 —— 指標算出來的通常比百分比推算準
PREFER_SIGNAL_SL = True

# 止損怎麼掛：
#   "separate" = 先開倉，再單獨掛一張條件單（預設）
#                雙向持倉模式下，把止損綁在市價單上會被 OKX 以 51278 退回整筆委託。
#                缺點是成交到掛上止損之間有很短的空窗（通常 < 1 秒）。
#   "attached" = 綁在開倉單上（原子性最好，但你的帳戶模式不吃這套）
SL_MODE = "separate"

# 開了倉但止損掛不上去時，要不要立刻平掉？
# True = 平掉（保守，預設）。1000 USDT 的裸單對小帳戶太危險。
# False = 留著，只記錄警告 —— 你必須自己去 App 手動補止損。
CLOSE_IF_NO_SL = True
# 訊號止損價離開倉價超過這個百分比就不採信（防止抓錯數字造成離譜的止損）
SIGNAL_SL_MAX_PCT = 5.0

# 控制台頁面是否允許從隧道（外網）存取。
# ⚠️ 開著等於任何拿到你隧道網址的人都看得到日誌，日誌裡有密鑰。
# 想用手機看的話再開，並且要有心理準備。
ALLOW_REMOTE_DASHBOARD = False

# 止盈觸發後把止損搬到哪裡：
#   "entry" = 開倉價（保本）   "tp1" = 止盈1的價位   "keep" = 不動
SL_AFTER_TP1 = "entry"
SL_AFTER_TP2 = "tp1"

# 收到止盈通知但無法判斷是第幾段時的處理：
#   "close_all" = 全部平掉（保守，預設）   "ignore" = 略過不動作
TP_UNKNOWN_POLICY = "close_all"

# 一則訊息同時含「方向關鍵字」和「止盈/止損關鍵字」時當成什麼？
# 這種情況很常見 —— 進場通知往往會順便把止損價和三個止盈價都列出來。
#   "entry_wins" = 當成進場（預設）      "tp_wins" = 當成止盈
# 判斷不了時程式一定會在日誌留警告，先跑 DRY_RUN 看真實訊息再決定。
SIGNAL_PRIORITY = "entry_wins"

# ---------- 同時只做一單 ----------
# True  = 手上有部位時，任何進場訊號一律略過（不管同向反向），
#         一定要等三段止盈跑完、或止損被觸發、或收到全平訊號，才會接受下一次進場。
# False = 反向訊號會先平倉再開反向單（原本的行為）
ONE_TRADE_AT_A_TIME = True

# 這一單需要的保證金（開倉價值 ÷ 槓桿）最多可以佔權益的百分之多少。
# 超過就拒絕，不要等交易所回「保證金不足」才發現。設 0 = 不檢查。
MAX_MARGIN_PCT = 80.0

MAX_ORDERS_PER_HOUR = 12  # 保險絲：指標抽風連發時擋下來
DEDUPE_WINDOW_SEC   = 20  # 幾秒內的重複訊號只執行一次
SAME_SIDE_POLICY    = "ignore"   # ONE_TRADE_AT_A_TIME=False 時才有作用

LONG_KW  = ["強力做多","做多","買入","多單","看多","bos↑","buy","long","bullish"]
SHORT_KW = ["強力做空","做空","賣出","空單","看空","bos↓","sell","short","bearish"]
# 止盈關鍵字（會再往下找是第幾段）
TP_KW    = ["止盈","止贏","take profit","tp"]
# 全平關鍵字（不分段，直接清空）
CLOSE_KW = ["平倉","全平","出場","止損","close all","exit","stop loss"]
# ==================== CONFIG 結束 ====================

# ------------------------------------------------------------------
# 本機私密設定
# ------------------------------------------------------------------
# 同資料夾如果有 my_config.py，裡面所有大寫變數會覆蓋上面的預設值。
# 把 API 金鑰、WEBHOOK_SECRET 寫在 my_config.py，
# 這樣主程式更新時不會蓋掉你的設定，也不會不小心把金鑰傳給別人。
_LOCAL_CONFIG = []
try:
    import my_config as _mc
    for _k in dir(_mc):
        if _k.isupper() and not _k.startswith('_'):
            globals()[_k] = getattr(_mc, _k)
            _LOCAL_CONFIG.append(_k)
except ImportError:
    pass
except Exception as _e:
    print(f"⚠️ my_config.py 讀取失敗，改用主程式裡的預設值: {_e}")

app = Flask(__name__)
exchange = ccxt.okx({'apiKey': OKX_API_KEY, 'secret': OKX_API_SECRET,
                     'password': OKX_API_PASSWORD, 'enableRateLimit': True,
                     'options': {'defaultType': 'swap'}})
if USE_DEMO:
    exchange.set_sandbox_mode(True)

POSITION_MODE = None
MARKETS_OK = False
lock = threading.RLock()   # 用 RLock：log() 本身也會拿鎖，可重入才不會自己卡死自己
logs = deque(maxlen=300)
seen = {}
order_ts = deque()
equity_cache = {'value': None, 'at': 0.0}
# 出場動作（平倉、搬止損）一次只允許一個執行緒進行。
# webhook 收到止盈通知、背景監控發現部位變小，兩邊都會想動手。
action_lock = threading.Lock()
stats = {'received': 0, 'rejected': 0, 'orders': 0, 'equity': None}

# 部位狀態：分批出場與移動止損都靠這份資料
pstate = {
    'active': False,
    'side': None,            # 'long' / 'short'
    'entry': None,           # 開倉參考價
    'initial': 0.0,          # 開倉時的總張數，分批平倉以它為基準
    'tp_hit': [],            # 已觸發過的止盈段數
    'tp_prices': {},         # {1: 4405.2, 2: 4378.7}
    'sl_price': None,        # 目前止損價
    'sl_order_id': None,     # 目前止損委託的 id
    'tp_order_ids': [],      # 掛在交易所的止盈委託 id
    'tp_on_exchange': False, # 這一輪的止盈是不是掛在交易所端
}


def mask_secret(text):
    """
    把密鑰從文字裡遮掉再記錄。

    控制台頁面跟 /webhook 是同一個服務，隧道會把整個服務攤在外網上，
    所以日誌裡不能留明文密鑰 —— 否則任何人打開你的隧道網址就能拿到它，
    拿到就能對你的帳戶送假訊號。
    """
    if WEBHOOK_SECRET and len(WEBHOOK_SECRET) >= 4:
        return text.replace(WEBHOOK_SECRET, WEBHOOK_SECRET[:3] + "***")
    return text


def log(m):
    line = f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {mask_secret(str(m))}"
    with lock:
        logs.appendleft(line)
    print(line, flush=True)


def reset_pstate():
    with lock:
        pstate.update({'active': False, 'side': None, 'entry': None, 'initial': 0.0,
                       'tp_hit': [], 'tp_prices': {}, 'sl_price': None, 'sl_order_id': None,
                       'tp_order_ids': [], 'tp_on_exchange': False})


def ensure_markets():
    """延遲載入市場資料。失敗不會讓服務起不來，下次收到訊號會再試。"""
    global MARKETS_OK, POSITION_MODE
    if MARKETS_OK:
        return True
    try:
        exchange.load_markets()
        MARKETS_OK = True
    except Exception as e:
        log(f"⚠️ 載入市場資料失敗，稍後重試: {e}")
        return False
    try:
        d = exchange.private_get_account_config().get('data', [{}])
        POSITION_MODE = d[0].get('posMode', 'net_mode') if d else 'net_mode'
        log(f"帳戶持倉模式：{POSITION_MODE}")
    except Exception as e:
        POSITION_MODE = 'net_mode'
        log(f"⚠️ 無法偵測持倉模式，當成單向持倉: {e}")
    return True


def pos_side_open(side):
    # 雙向持倉模式下開倉必須帶 posSide，否則 OKX 回 51000 參數錯誤
    return None if POSITION_MODE != 'long_short_mode' else ('long' if side == 'buy' else 'short')


def pos_side_close(pside):
    # 平倉的 posSide 要填「原本部位的方向」，不是這次的買賣方向
    return None if POSITION_MODE != 'long_short_mode' else pside


def get_equity(force=False):
    """
    查詢帳戶 USDT 總權益。查不到回傳 None —— 呼叫端必須當成「不下單」，
    絕對不要用猜的數字代替，那等於用錯誤的本金算倉位。
    """
    now = time.time()
    with lock:
        if (not force and equity_cache['value'] is not None
                and now - equity_cache['at'] < EQUITY_CACHE_SEC):
            return equity_cache['value']
    try:
        bal = exchange.fetch_balance()
        eq = bal.get('total', {}).get('USDT')
        if eq is None:
            info = bal.get('info', {}).get('data', [{}])
            eq = float(info[0].get('totalEq')) if info and info[0].get('totalEq') else None
        if eq is None:
            log("⚠️ 回應裡找不到 USDT 權益欄位")
            return None
        eq = float(eq)
        with lock:
            equity_cache['value'] = eq
            equity_cache['at'] = now
            stats['equity'] = round(eq, 2)
        return eq
    except Exception as e:
        log(f"⚠️ 查詢帳戶權益失敗: {e}")
        return None


def clamp_notional(v, why):
    """套用上下限。被夾住時一定要留紀錄，不然你會以為自己的設定有生效。"""
    if MAX_NOTIONAL and MAX_NOTIONAL > 0 and v > MAX_NOTIONAL:
        log(f"⚠️ 算出 {v:.2f} USDT 超過上限 MAX_NOTIONAL={MAX_NOTIONAL}，已壓回上限。"
            f"若這不是你要的，把 MAX_NOTIONAL 調高或設成 0（不限制）")
        return MAX_NOTIONAL, why + f" → 受上限壓回 {MAX_NOTIONAL}"
    if v < MIN_NOTIONAL:
        return None, (f"算出 {v:.2f} USDT 低於最小值 MIN_NOTIONAL={MIN_NOTIONAL}，本次不下單")
    return v, why


def resolve_notional(data):
    """決定這一單的名目金額。回傳 (金額, 說明) 或 (None, 失敗原因)。"""
    explicit = data.get('notional') or data.get('amount')
    if explicit:
        try:
            v = float(explicit)
            if v > 0:
                return clamp_notional(v, "訊息指定")
        except (TypeError, ValueError):
            log(f"⚠️ 訊息裡的 notional 格式錯誤（{explicit}），改用預設計算方式")

    if POSITION_SIZING in ("order_value", "fixed"):
        # 每單固定開倉價值：不查權益、不看槓桿，就是你寫死的那個數字。
        # 實際佔用多少保證金由交易所依槓桿決定。
        v = ORDER_VALUE if POSITION_SIZING == "order_value" else FIXED_NOTIONAL
        return clamp_notional(v, f"固定開倉價值 {v:.2f} USDT")

    eq = get_equity()
    if eq is None or eq <= 0:
        return None, "查不到帳戶權益，無法計算倉位，本次不下單"

    if SIZING_BASIS == "margin":
        margin = eq * EQUITY_PCT / 100
        notional = margin * LEVERAGE
        why = (f"權益 {eq:.2f} × {EQUITY_PCT}% = 保證金 {margin:.2f}"
               f" × {LEVERAGE}倍 = 名目 {notional:.2f}")
    else:
        notional = eq * EQUITY_PCT / 100
        why = (f"權益 {eq:.2f} × {EQUITY_PCT}% = 名目 {notional:.2f}"
               f"（保證金約 {notional / LEVERAGE:.2f}，{LEVERAGE}倍）")
    return clamp_notional(notional, why)


def calc_amount(notional, price):
    """
    名目金額換算張數。回傳 (張數, 說明)；張數 0 代表不該下這一單。

    兩個關鍵：
    1. contractSize —— OKX 的 XAU/USDT 永續 1 張 = 0.001 盎司，少除會差三個數量級
    2. 整數張的商品要能被 TP_SPLITS 整除，否則分批平倉會有一批小於最小下單量
    """
    try:
        m = exchange.market(SYMBOL)
        csize = m.get('contractSize') or 1
        amin = (m.get('limits', {}).get('amount', {}).get('min')) or 0
        step = (m.get('precision', {}).get('amount')) or 0
    except Exception:
        csize, amin, step = 1, 0, 0

    raw = (notional / price) / csize
    amt = max(raw, amin)
    try:
        amt = float(exchange.amount_to_precision(SYMBOL, amt))
    except Exception:
        pass

    if amin and raw < amin:
        log(f"⚠️ {notional:.2f} USDT 低於交易所最小下單量，已提高為 {amt} 張"
            f"（實際約 {amt * csize * price:.2f} USDT），曝險比設定大")

    # 整數張的商品：把張數調成 TP_SPLITS 的倍數，分批才會剛好整除
    if ROUND_TO_SPLITS and TP_SPLITS > 1 and step and step >= 1 and amt >= TP_SPLITS:
        aligned = float(int(amt // TP_SPLITS) * TP_SPLITS)
        if aligned != amt:
            log(f"張數 {amt} 調整為 {aligned}（{TP_SPLITS} 的倍數，讓分批平倉能整除）")
            amt = aligned

    # 分不出 TP_SPLITS 批就別下單 —— 下了之後止盈1、止盈2 都會被交易所拒絕
    if TP_SPLITS > 1 and amin:
        per_split = amt / TP_SPLITS
        if per_split < amin:
            need = amin * TP_SPLITS * csize * price
            msg = (f"張數 {amt} 分成 {TP_SPLITS} 批後每批只有 {per_split:.4f} 張，"
                   f"低於最小下單量 {amin} 張，分批平倉會被拒絕。"
                   f"每單名目至少要 {need:.2f} USDT")
            if REQUIRE_SPLITTABLE:
                log(f"🚫 {msg} → 依 REQUIRE_SPLITTABLE 設定拒絕這筆進場")
                return 0.0, msg
            log(f"⚠️ {msg} → 仍照設定進場，但只有最後一段止盈會生效")

    return amt, ""


# ------------------------------------------------------------------
# 訊號解析
# ------------------------------------------------------------------
# 「止盈2」「止盈 ✅ 2」「TP2」「take profit 3」都要抓得到
TP_LEVEL_RE = re.compile(r'(?:止盈|止贏|take\s*profit|tp)\s*[^\d\n]{0,6}?([1-9])', re.I)
# 「止盈✅2：4378.7」這種格式，抓段數後面的價格
TP_PRICE_RE = re.compile(
    r'(?:止盈|止贏|take\s*profit|tp)\s*[^\d\n]{0,6}?[1-9]\s*[:：]?\s*(\d{2,7}(?:\.\d+)?)', re.I)


# 「止損 4480.5」「止損：4480.5」「止損❌4480.5」「SL: 4480.5」都要抓得到
SL_PRICE_RE = re.compile(
    r'(?:止損|止蝕|stop\s*loss|sl)\s*[^\d\n]{0,6}?(\d{2,7}(?:\.\d+)?)', re.I)


def parse_sl_price(text, side, entry):
    """
    從訊號裡找出指標給的止損價。回傳 (價格, 說明) 或 (None, 原因)。

    抓到之後一定要驗證方向與距離：
    多單的止損必須在開倉價下方、空單在上方，而且不能離譜地遠。
    抓錯數字（例如抓到止盈價或成交量）會造成完全錯誤的風險控制。
    """
    m = SL_PRICE_RE.search(text)
    if not m:
        return None, "訊息裡沒有止損價"
    try:
        sl = float(m.group(1))
    except ValueError:
        return None, "止損價格式無法解析"

    if sl <= 0 or entry <= 0:
        return None, "價格不合理"

    # 方向檢查
    if side == 'buy' and sl >= entry:
        return None, f"多單的止損價 {sl} 不該在開倉價 {entry:.2f} 之上"
    if side == 'sell' and sl <= entry:
        return None, f"空單的止損價 {sl} 不該在開倉價 {entry:.2f} 之下"

    # 距離檢查
    dist_pct = abs(entry - sl) / entry * 100
    if dist_pct > SIGNAL_SL_MAX_PCT:
        return None, (f"止損價 {sl} 離開倉價 {dist_pct:.2f}%，"
                      f"超過 SIGNAL_SL_MAX_PCT={SIGNAL_SL_MAX_PCT}%，不採信")

    return sl, f"採用訊號的止損價 {sl}（距開倉價 {dist_pct:.2f}%）"


# 進場訊息會一次列出三個止盈價：「止盈 1：4518.9  止盈 2：4523.25  止盈 3：4527.55」
TP_TABLE_RE = re.compile(
    r'(?:止盈|止贏|tp)\s*([1-9])\s*[:：]\s*(\d{2,7}(?:\.\d+)?)', re.I)


def parse_tp_table(text):
    """
    從進場訊息抓出「第幾段 → 價位」的對照表。

    這比等止盈通知才拿價位可靠得多 —— 止盈通知只寫「已到達止盈 1」，沒有帶價格，
    所以止盈2要把止損搬到止盈1價位時，得靠這張表。
    """
    table = {}
    for m in TP_TABLE_RE.finditer(text):
        try:
            table[int(m.group(1))] = float(m.group(2))
        except ValueError:
            pass
    return table


def parse_tp(text):
    """
    從止盈通知裡找出「第幾段」與「價位」。回傳 (levels集合, level, price)。

    levels 有兩段以上代表這是一則「列出所有止盈價位」的訊息（通常是進場通知），
    而不是「第N段達成」的事件通知 —— 真正的止盈事件只會提到一個段數。
    """
    levels = set()
    for m in TP_LEVEL_RE.finditer(text):
        try:
            levels.add(int(m.group(1)))
        except ValueError:
            pass
    level = next(iter(levels)) if len(levels) == 1 else None
    price = None
    m2 = TP_PRICE_RE.search(text)
    if m2 and level is not None:
        try:
            price = float(m2.group(1))
        except ValueError:
            price = None
    return levels, level, price


def parse_action(data):
    """
    決定這則訊號要做什麼。回傳 (action, 說明, extra)
    action: buy / sell / close / tp / None
    extra : tp 時為 {'level': n, 'price': p}
    """
    a = str(data.get('action') or data.get('side') or "").strip().lower()
    if a in ('buy', 'long'):
        return 'buy', 'JSON action', {}
    if a in ('sell', 'short'):
        return 'sell', 'JSON action', {}
    if a in ('close', 'exit', 'flat'):
        return 'close', 'JSON action', {}
    if a in ('tp', 'takeprofit', 'take_profit'):
        lvl = data.get('tp_level') or data.get('level')
        try:
            lvl = int(lvl) if lvl is not None else None
        except (TypeError, ValueError):
            lvl = None
        try:
            prc = float(data.get('tp_price') or data.get('price') or 0) or None
        except (TypeError, ValueError):
            prc = None
        return 'tp', 'JSON action', {'level': lvl, 'price': prc}

    blob = " ".join(str(v) for v in data.values())
    low = blob.lower()

    # 先把三種線索各自找出來，再一起決定，不要一看到關鍵字就下結論。
    # 原因：進場通知常常會順便列出止損價和三個止盈價，
    # 「先看到止盈就當止盈」會把進場訊號誤判成出場。
    lo = next((k for k in LONG_KW if k in low), None)
    sh = next((k for k in SHORT_KW if k in low), None)
    close_hit = next((k for k in CLOSE_KW if k in low), None)
    tp_hit = any(k in low for k in TP_KW)
    levels, level, price = parse_tp(blob) if tp_hit else (set(), None, None)

    if lo and sh:
        return None, f"多空關鍵字同時出現（{lo}/{sh}），安全起見不下單", {}

    direction = lo or sh
    # 提到兩個以上的止盈段數 → 這是「列出所有價位」的訊息，不是單一止盈事件
    is_tp_event = tp_hit and len(levels) <= 1

    if direction and (is_tp_event or close_hit):
        note = f"訊息同時含方向「{direction}」與出場字樣" \
               f"（{'止盈' if is_tp_event else close_hit}）"
        if SIGNAL_PRIORITY == "tp_wins":
            if is_tp_event:
                log(f"⚠️ {note}，依 SIGNAL_PRIORITY=tp_wins 當成止盈處理")
                return 'tp', note + "，當成止盈", {'level': level, 'price': price}
            log(f"⚠️ {note}，依 SIGNAL_PRIORITY=tp_wins 當成全平處理")
            return 'close', note + "，當成全平", {}
        log(f"⚠️ {note}，依 SIGNAL_PRIORITY=entry_wins 當成進場處理。"
            f"若判斷錯了請改設定或告知訊息格式")
        return ('buy' if lo else 'sell'), note + "，當成進場", {}

    if is_tp_event:
        return 'tp', (f"止盈通知，判定第 {level} 段" if level
                      else "止盈通知，但看不出是第幾段"), {'level': level, 'price': price}

    if tp_hit and len(levels) > 1:
        log(f"訊息裡出現多個止盈段數 {sorted(levels)}，判定為資訊性訊息，不當成止盈事件")

    if close_hit:
        return 'close', f"關鍵字「{close_hit}」", {}

    if lo:
        return 'buy', f"關鍵字「{lo}」", {}
    if sh:
        return 'sell', f"關鍵字「{sh}」", {}
    return None, "找不到方向關鍵字", {}


# ------------------------------------------------------------------
# 部位與委託
# ------------------------------------------------------------------
def get_positions():
    """
    查詢目前部位。

    ⚠️ 查詢失敗時回傳 None，不是空清單 —— 這兩者天差地遠：
    空清單 = 確定沒有部位；None = 不知道，可能有也可能沒有。
    以前這裡出錯回空清單，害得一次網路失敗就被當成「部位已清空」，
    程式因此撤掉了掛在交易所的三張止盈單。呼叫端必須自己處理 None。
    """
    try:
        return [p for p in exchange.fetch_positions([SYMBOL]) if (p.get('contracts') or 0) > 0]
    except Exception as e:
        log(f"⚠️ 查詢部位失敗（當成「不確定」，不會據此清狀態）: {e}")
        return None


def current_contracts():
    """
    目前這個商品還剩幾張。查詢失敗回傳 None（不是 0）。
    任何拿它來做決策的地方都必須先判斷 None，否則會把「查不到」當成「沒有」。
    """
    pos = get_positions()
    if pos is None:
        return None
    return sum((p.get('contracts') or 0) for p in pos)


def fetch_stop_orders():
    """
    查詢未觸發的條件單。OKX 的策略委託跟一般委託是分開的通道，
    不同 ccxt 版本用的參數名不一樣（stop / trigger），兩個都試。
    """
    for key in ('stop', 'trigger'):
        try:
            return exchange.fetch_open_orders(SYMBOL, params={key: True}), key
        except Exception:
            continue
    log("⚠️ 查詢條件單失敗（stop / trigger 兩種參數都試過）")
    return [], 'stop'


def cancel_stop_orders(keep_id=None):
    """撤掉所有條件單，可指定保留一張（剛掛好的新止損）"""
    orders, key = fetch_stop_orders()
    for o in orders:
        if keep_id and o.get('id') == keep_id:
            continue
        try:
            exchange.cancel_order(o['id'], SYMBOL, params={key: True})
            log(f"撤銷舊條件單 {o.get('id')}")
        except Exception as e:
            log(f"⚠️ 撤銷條件單 {o.get('id')} 失敗（可能已觸發或已撤）: {e}")


def place_tp_orders(pside, tp_table, total_amt):
    """
    把三段止盈以 reduce-only 限價單掛到交易所。

    這樣做的意義：止盈變成交易所在管，程式當掉、電腦關機、隧道斷線，
    出場照樣會發生。程式只剩「搬移止損」一件事。

    回傳已成功掛出的委託 id 清單。掛不齊時由呼叫端決定要不要退回舊做法。
    """
    close_side = 'sell' if pside == 'long' else 'buy'
    ids = []

    # 前面幾段各平 1/TP_SPLITS，最後一段收尾（把除不盡的餘數一起帶走）
    per = total_amt / TP_SPLITS
    try:
        per = float(exchange.amount_to_precision(SYMBOL, per))
    except Exception:
        pass

    for level in sorted(tp_table.keys()):
        if level > TP_SPLITS:
            continue
        price = tp_table[level]
        amt = per if level < TP_SPLITS else max(total_amt - per * (TP_SPLITS - 1), 0)
        try:
            amt = float(exchange.amount_to_precision(SYMBOL, amt))
        except Exception:
            pass
        if amt <= 0:
            continue

        params = {'tdMode': TD_MODE, 'reduceOnly': True}
        ps = pos_side_close(pside)
        if ps:
            params['posSide'] = ps
        px = float(exchange.price_to_precision(SYMBOL, price))

        try:
            o = exchange.create_order(SYMBOL, 'limit', close_side, amt, px, params)
            ids.append(o.get('id'))
            log(f"🎯 止盈{level} 已掛單：{amt} 張 @ {px}")
        except Exception as e:
            # 有些帳戶模式不吃 reduceOnly，去掉再試一次
            log(f"⚠️ 止盈{level} 掛單失敗（{e}），改用不帶 reduceOnly 再試")
            try:
                params.pop('reduceOnly', None)
                o = exchange.create_order(SYMBOL, 'limit', close_side, amt, px, params)
                ids.append(o.get('id'))
                log(f"🎯 止盈{level} 已掛單：{amt} 張 @ {px}")
            except Exception as e2:
                log(f"❌ 止盈{level} 掛不上去: {e2}")
    return ids


def cancel_tp_orders():
    """撤掉掛在交易所的止盈限價單（一般委託，不是條件單）"""
    try:
        opens = exchange.fetch_open_orders(SYMBOL)
    except Exception as e:
        log(f"⚠️ 查詢一般委託失敗: {e}")
        return
    for o in opens:
        try:
            exchange.cancel_order(o['id'], SYMBOL)
            log(f"撤銷止盈委託 {o.get('id')}")
        except Exception as e:
            log(f"⚠️ 撤銷委託 {o.get('id')} 失敗（可能已成交）: {e}")
    with lock:
        pstate['tp_order_ids'] = []


def place_stop_loss(pside, trigger_price, contracts):
    """
    掛一張獨立的止損條件單（觸發後市價平倉）。
    pside: 'long' / 'short'  —— 部位方向，不是下單方向
    """
    close_side = 'sell' if pside == 'long' else 'buy'
    params = {'tdMode': TD_MODE, 'reduceOnly': True,
              'stopLossPrice': float(exchange.price_to_precision(SYMBOL, trigger_price))}
    ps = pos_side_close(pside)
    if ps:
        params['posSide'] = ps
    o = exchange.create_order(SYMBOL, 'market', close_side, contracts, None, params)
    log(f"🛡️ 止損已掛在 {params['stopLossPrice']}（{contracts} 張）")
    return o.get('id')


def move_stop_loss(new_price, why):
    """
    搬移止損。先掛新的、再撤舊的 —— 順序反過來會有一小段沒有保護的空窗。
    兩張同時存在時都是 reduceOnly，先觸發的那張平掉部位，另一張自然失效。
    """
    remaining = current_contracts()
    if remaining is None:
        log("⚠️ 查不到部位張數，保留原止損不動（寧可不動也不要把保護撤掉）")
        return None
    if remaining <= 0:
        log("部位已清空，撤掉所有條件單")
        cancel_stop_orders()
        with lock:
            pstate['sl_order_id'] = None
            pstate['sl_price'] = None
        return None
    side = pstate.get('side')
    try:
        new_id = place_stop_loss(side, new_price, remaining)
    except Exception as e:
        log(f"❌ 掛新止損失敗，保留原止損不動: {e}")
        return None
    cancel_stop_orders(keep_id=new_id)
    with lock:
        pstate['sl_order_id'] = new_id
        pstate['sl_price'] = new_price
    log(f"🔀 止損移到 {new_price}（{why}）")
    return new_id


def partial_close(pside, contracts):
    """市價平掉指定張數"""
    if contracts <= 0:
        return 0.0
    remaining = current_contracts()
    if remaining is None:
        log("⚠️ 查不到部位張數，這次不平倉（避免平錯數量）")
        return 0.0
    qty = min(contracts, remaining)
    try:
        qty = float(exchange.amount_to_precision(SYMBOL, qty))
    except Exception:
        pass
    if qty <= 0:
        log(f"⚠️ 要平的 {contracts:.4f} 張經過精度處理後變成 0，這一批沒有平到。"
            f"通常是倉位太小、分不出 {TP_SPLITS} 批造成的")
        return 0.0
    close_side = 'sell' if pside == 'long' else 'buy'
    params = {'tdMode': TD_MODE, 'reduceOnly': True}
    ps = pos_side_close(pside)
    if ps:
        params['posSide'] = ps
    exchange.create_order(SYMBOL, 'market', close_side, qty, None, params)
    log(f"📤 分批平倉 {qty} 張（{pside}）")
    return qty


def close_all():
    done = []
    pos = get_positions()
    if pos is None:
        log("🚨 查不到部位，無法確認要平什麼，這次不動作。請自行到 OKX App 確認")
        return None
    for p in pos:
        c, s = p.get('contracts') or 0, p.get('side')
        prm = {'tdMode': TD_MODE, 'reduceOnly': True}
        ps = pos_side_close(s)
        if ps:
            prm['posSide'] = ps
        try:
            exchange.create_order(SYMBOL, 'market', 'sell' if s == 'long' else 'buy', c, None, prm)
            done.append(f"{s} {c}張")
            log(f"市價平倉：{s} {c} 張")
        except Exception as e:
            log(f"⚠️ 平倉失敗 {s} {c}張: {e}")
    cancel_stop_orders()
    cancel_tp_orders()
    reset_pstate()
    if done:
        get_equity(force=True)   # 平倉後權益變了，強制刷新
    return done


def open_position(side, notional, signal_text=""):
    """開倉並掛上初始止損，同時記錄部位狀態供後續分批出場使用"""
    price = exchange.fetch_ticker(SYMBOL)['last']
    amt, reject = calc_amount(notional, price)
    if amt <= 0:
        return {'action': 'rejected', 'reason': reject or '換算後張數為 0'}

    params = {'tdMode': TD_MODE}
    ps = pos_side_open(side)
    if ps:
        params['posSide'] = ps

    # 初始止損直接綁在開倉單上：這樣從成交的第一毫秒就有保護，沒有空窗。
    # 之後要搬動時，改用獨立的條件單並撤掉這張。
    #
    # 止損價的來源優先順序：
    #   1. 訊號自己帶的止損價（指標算出來的，比百分比推算準）
    #   2. 退回 DEFAULT_SL_PCT 的百分比推算
    sl_price = None
    if PREFER_SIGNAL_SL and signal_text:
        sig_sl, note = parse_sl_price(signal_text, side, price)
        if sig_sl:
            sl_price = float(exchange.price_to_precision(SYMBOL, sig_sl))
            log(f"🎯 {note}")
        else:
            log(f"訊號止損價不可用（{note}），改用 {DEFAULT_SL_PCT}% 推算")

    if sl_price is None and DEFAULT_SL_PCT > 0:
        sl_price = price * (1 - DEFAULT_SL_PCT / 100) if side == 'buy' \
            else price * (1 + DEFAULT_SL_PCT / 100)
        sl_price = float(exchange.price_to_precision(SYMBOL, sl_price))

    # 綁在開倉單上 vs 開完再單獨掛，看 SL_MODE。
    # 雙向持倉模式下綁在市價單上會被 OKX 以 51278 退回整筆委託，所以預設走 separate。
    if sl_price and SL_MODE == "attached":
        params['slTriggerPx'] = sl_price
        params['slOrdPx'] = '-1'     # -1 = 觸發後市價成交
    # 不掛固定止盈：出場改由指標的三段止盈通知驅動

    o = exchange.create_order(SYMBOL, 'market', side, amt, None, params)

    pside0 = 'long' if side == 'buy' else 'short'

    # separate 模式：成交後立刻補掛止損。
    # 這中間有很短的空窗，所以失敗時要立刻處理，不能讓裸單留著。
    sl_order_id = None
    if sl_price and SL_MODE != "attached":
        for attempt in (1, 2, 3):
            try:
                sl_order_id = place_stop_loss(pside0, sl_price, amt)
                break
            except Exception as e:
                log(f"⚠️ 第 {attempt} 次掛止損失敗: {e}")
                time.sleep(0.5)

        if sl_order_id is None:
            log("🚨 止損掛不上去，這是一筆沒有保護的部位")
            if CLOSE_IF_NO_SL:
                log("🚨 依 CLOSE_IF_NO_SL 設定，立刻把剛開的倉平掉")
                try:
                    close_all()
                    return {'action': 'rejected',
                            'reason': '止損掛不上去，已把剛開的部位平掉。'
                                      '請把日誌裡的錯誤訊息貼給我'}
                except Exception as e:
                    log(f"🚨🚨 連平倉都失敗了：{e} —— 請立刻到 OKX App 手動處理！")
                    return {'action': 'danger',
                            'reason': f'有裸單且自動平倉失敗，請立刻手動處理：{e}'}
            else:
                log("🚨 依設定保留部位。請立刻到 OKX App 手動補上止損！")

    # 張數必須取整，所以實際開倉價值會跟設定值差一點，把真實數字印出來免得對不上帳
    try:
        _cs = exchange.market(SYMBOL).get('contractSize') or 1
        real = amt * _cs * price
        if abs(real - notional) > 0.01:
            log(f"（張數取整後，實際開倉價值 {real:.2f} USDT，設定值是 {notional:.2f}，"
                f"差 {real - notional:+.2f}）")
    except Exception:
        pass

    # 進場訊息通常會一次列出三個止盈價，先存起來。
    # 止盈通知本身只寫「已到達止盈 1」不帶價格，之後要搬止損就得靠這張表。
    tp_table = parse_tp_table(signal_text) if signal_text else {}

    # 把止盈掛到交易所端。掛得齊才算數，掛不齊就撤掉、退回「等訊號才平倉」的舊做法，
    # 免得變成一半在交易所、一半靠程式的混合狀態，那最難除錯。
    tp_ids = []
    tp_on_exchange = False
    if TP_ON_EXCHANGE and len(tp_table) >= TP_SPLITS:
        tp_ids = place_tp_orders(pside0, tp_table, amt)
        if len(tp_ids) == TP_SPLITS:
            tp_on_exchange = True
            log(f"✅ 三段止盈已全部掛在交易所端，程式當掉也會執行")
        else:
            log(f"⚠️ 止盈只掛成 {len(tp_ids)}/{TP_SPLITS} 張，撤掉改用程式收訊號平倉")
            cancel_tp_orders()
            tp_ids = []
    elif TP_ON_EXCHANGE:
        log(f"訊息裡只找到 {len(tp_table)} 個止盈價（需要 {TP_SPLITS} 個），"
            f"這一輪改用程式收訊號平倉")

    pside = 'long' if side == 'buy' else 'short'
    with lock:
        pstate.update({'active': True, 'side': pside, 'entry': price, 'initial': amt,
                       'tp_hit': [], 'tp_prices': dict(tp_table), 'sl_price': sl_price,
                       'sl_order_id': sl_order_id,
                       'tp_order_ids': tp_ids, 'tp_on_exchange': tp_on_exchange})
    log(f"✅ {side.upper()} {amt} 張 @約 {price:.2f}（名目 {notional:.2f} USDT）"
        f" 初始止損 {sl_price if sl_price else '未掛'}"
        f" ｜ 出場等指標的 {TP_SPLITS} 段止盈通知")
    if tp_table:
        log("📋 已記下訊號的止盈價位：" +
            "、".join(f"止盈{k} {v}" for k, v in sorted(tp_table.items())))
    return {'id': o.get('id'), 'side': side, 'amount': amt, 'price': price,
            'notional': round(notional, 2), 'sl': sl_price}


def advance_after_tp(level):
    """
    某一段止盈完成後，把止損搬到該去的位置。

    只負責搬止損，不負責平倉 —— 平倉可能是交易所的止盈單成交的，
    也可能是程式自己市價平的，這裡不管來源。
    重複呼叫同一段是安全的（已記錄過就直接跳過）。
    """
    with lock:
        if level in pstate['tp_hit']:
            return False
        pside = pstate['side']
        entry = pstate['entry']
        tp_prices = dict(pstate['tp_prices'])
        cur_sl = pstate['sl_price']

    rule = SL_AFTER_TP1 if level == 1 else (SL_AFTER_TP2 if level == 2 else 'keep')
    new_sl, why = None, ""
    if rule == 'entry':
        new_sl, why = entry, f"止盈{level}完成，止損移到開倉價保本"
    elif rule == 'tp1':
        if tp_prices.get(1):
            new_sl, why = tp_prices[1], f"止盈{level}完成，止損移到止盈1價位"
        else:
            new_sl, why = entry, f"止盈{level}完成，但沒有止盈1的價位，改為移到開倉價保本"
            log("⚠️ 沒有記錄到止盈1的價位，止損改移到開倉價")
    elif rule == 'keep':
        log(f"止盈{level}完成，依設定止損維持不動")

    with lock:
        pstate['tp_hit'].append(level)

    if not new_sl:
        return True

    # 方向合理性：多單止損只能往上搬，空單只能往下搬
    if (pside == 'long' and cur_sl and new_sl <= cur_sl) or \
       (pside == 'short' and cur_sl and new_sl >= cur_sl):
        log(f"新止損 {new_sl} 不比現有的 {cur_sl} 有利，維持原止損不動")
        return True

    move_stop_loss(new_sl, why)
    return True


def monitor_loop():
    """
    背景監控：盯著部位張數變化。

    止盈掛在交易所端之後，成交的那一刻程式並不會被通知。
    這個迴圈定期比對「現在剩幾張」跟「開倉時有幾張」，
    推算出已經完成到第幾段，然後把止損搬到對應位置。

    好處是完全不依賴 TradingView 的止盈通知 —— 通知漏了、隧道斷了都沒差。
    """
    log(f"背景監控啟動（每 {MONITOR_INTERVAL_SEC} 秒檢查一次部位）")
    while True:
        time.sleep(MONITOR_INTERVAL_SEC)
        try:
            with lock:
                active = pstate['active']
                initial = pstate['initial']
                on_ex = pstate['tp_on_exchange']
                done = list(pstate['tp_hit'])
            if not active or initial <= 0 or not on_ex:
                continue

            if not action_lock.acquire(blocking=False):
                continue          # 有人正在動手，這輪跳過
            try:
                remaining = sync_position_state()
                if remaining is None:
                    continue          # 查詢失敗，這輪什麼都不做
                if remaining <= 0:
                    continue          # 清理由 sync_position_state 負責（且需連續確認）

                # 剩下的張數落在哪一段，就代表前幾段已經成交了
                per = initial / TP_SPLITS
                for level in range(1, TP_SPLITS):
                    if level in done:
                        continue
                    # 留一點容差，避免張數取整造成誤判
                    if remaining <= initial - per * level + per * 0.25:
                        log(f"背景監控：偵測到止盈{level} 已成交"
                            f"（剩 {remaining} 張 / 初始 {initial} 張）")
                        advance_after_tp(level)
            finally:
                action_lock.release()
        except Exception as e:
            log(f"⚠️ 背景監控發生例外（不影響交易）: {e}")


def handle_tp(level, price):
    """
    處理止盈通知。
      第1段 → 平 1/3，止損移到開倉價（保本）
      第2段 → 再平 1/3，止損移到止盈1的價位
      第3段 → 全平
    """
    # 先對帳（這一步可能會從交易所接管部位），再讀狀態。
    # 順序反過來的話會拿到接管前的舊值，把剛接管好的部位當成「沒有紀錄」。
    remaining_now = sync_position_state()
    if remaining_now is None:
        log("⚠️ 查不到部位，這則止盈通知先不處理（交易所端的委託不受影響）")
        return {'action': 'tp', 'result': '查詢失敗，略過'}
    if remaining_now <= 0:
        log("收到止盈通知，但目前沒有部位（可能已被止損或手動平掉）")
        return {'action': 'tp', 'result': '無部位，略過'}

    with lock:
        active = pstate['active']
        pside = pstate['side']
        initial = pstate['initial']
        entry = pstate['entry']
        tp_hit = list(pstate['tp_hit'])
        tp_prices = dict(pstate['tp_prices'])

    if not active:
        # 有部位但沒有狀態紀錄（程式重啟過，或狀態被誤清）。
        # 先看交易所上還有沒有止盈委託 —— 有的話出場已經交給交易所了，
        # 這時候插手市價全平反而會破壞原本的分批計畫。
        try:
            opens = exchange.fetch_open_orders(SYMBOL)
        except Exception as e:
            log(f"⚠️ 查不到掛單（{e}），保守起見不動作")
            return {'action': 'tp', 'result': '狀態遺失且查不到掛單，未動作'}

        if opens:
            log(f"⚠️ 有部位但沒有開倉紀錄，不過交易所還有 {len(opens)} 張止盈委託 → "
                f"出場交給交易所，程式不插手")
            return {'action': 'tp', 'result': f'狀態遺失，但交易所有 {len(opens)} 張止盈掛單，未動作'}

        log("⚠️ 有部位、沒有開倉紀錄、交易所也沒有止盈掛單 → 無法分批，改為全部平倉")
        return {'action': 'tp', 'result': '狀態遺失且無掛單，全平', 'closed': close_all()}

    if level is None:
        if TP_UNKNOWN_POLICY == 'ignore':
            log("⚠️ 看不出是第幾段止盈，依設定略過不動作")
            return {'action': 'tp', 'result': '無法判斷段數，略過'}
        log("⚠️ 看不出是第幾段止盈，依設定全部平倉（保守處理）")
        return {'action': 'tp', 'result': '無法判斷段數，全平', 'closed': close_all()}

    if level in tp_hit:
        log(f"止盈{level} 先前已處理過，略過")
        return {'action': 'tp', 'result': f'止盈{level} 重複，略過'}

    # 止盈掛在交易所端時，平倉是交易所做的，程式只要把止損搬過去。
    # 這裡不能再送市價單，否則會多平一份。
    with lock:
        on_exchange = pstate['tp_on_exchange']
    if on_exchange:
        # 止盈掛在交易所時，這則通知只代表「價格碰到了」，不代表限價單一定成交
        # （可能只是插一下就回去）。所以這裡不搬止損 —— 交給背景監控，
        # 它是看「部位張數真的變少了」才動作，比通知準確。
        if price:
            with lock:
                pstate['tp_prices'][level] = price
        log(f"止盈{level}：出場由交易所端的委託負責；"
            f"止損搬移交給背景監控依實際成交判斷")
        return {'action': 'tp', 'level': level,
                'result': '已記錄，出場與搬止損由交易所與背景監控處理',
                'remaining': remaining_now}

    if price:
        with lock:
            pstate['tp_prices'][level] = price
        tp_prices[level] = price

    # 最後一段：全部出場
    if level >= TP_SPLITS:
        log(f"🎯 止盈{level}（最後一段）→ 平掉剩餘全部")
        closed = close_all()
        return {'action': 'tp', 'level': level, 'result': '全平', 'closed': closed}

    # 前面幾段：各平 1/3
    qty = initial / TP_SPLITS
    log(f"🎯 止盈{level} → 平掉初始倉位的 1/{TP_SPLITS}（約 {qty:.4f} 張）")
    closed_qty = partial_close(pside, qty)

    with lock:
        pstate['tp_hit'].append(level)

    # 決定止損要搬到哪
    rule = SL_AFTER_TP1 if level == 1 else (SL_AFTER_TP2 if level == 2 else 'keep')
    new_sl = None
    why = ""
    if rule == 'entry':
        new_sl, why = entry, "止盈1觸發，止損移到開倉價保本"
    elif rule == 'tp1':
        if tp_prices.get(1):
            new_sl, why = tp_prices[1], "止盈2觸發，止損移到止盈1價位"
        else:
            # 沒收到止盈1的價位（可能漏收或格式沒帶價），退而求其次移到開倉價。
            # 寧可保本也不要把止損留在原始位置。
            new_sl, why = entry, "止盈2觸發，但沒有止盈1的價位，改為移到開倉價保本"
            log("⚠️ 沒有記錄到止盈1的價位，止損改移到開倉價")

    if new_sl:
        # 方向合理性檢查：多單止損只能往上搬，空單只能往下搬
        if (pside == 'long' and new_sl <= (pstate['sl_price'] or 0)) or \
           (pside == 'short' and pstate['sl_price'] and new_sl >= pstate['sl_price']):
            log(f"⚠️ 新止損 {new_sl} 不比現有的 {pstate['sl_price']} 有利，維持原止損不動")
        else:
            move_stop_loss(new_sl, why)

    return {'action': 'tp', 'level': level, 'closed_contracts': closed_qty,
            'new_sl': pstate.get('sl_price'), 'remaining': current_contracts()}


# 「部位歸零」要連續確認幾次才算數。一次查詢失敗或交易所短暫回報不一致
# 就把狀態清掉、把止盈單撤掉，代價太大了。
FLAT_CONFIRM_NEEDED = 2
_flat_confirm = {'count': 0}


def _order_trigger_price(o):
    """從各種可能的欄位裡把條件單的觸發價挖出來（不同 ccxt 版本欄位名不一樣）"""
    for k in ('stopLossPrice', 'triggerPrice', 'stopPrice'):
        v = o.get(k)
        if v:
            return float(v)
    info = o.get('info') or {}
    for k in ('slTriggerPx', 'triggerPx', 'ordPx'):
        v = info.get(k)
        try:
            if v and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def adopt_existing_position():
    """
    從交易所把現有部位的狀態重建回程式裡。

    用在兩個時機：程式啟動、以及執行中發現「有部位但沒有紀錄」。
    能重建的東西全部來自交易所，所以不會有存檔過期的問題：

      方向、開倉均價、目前張數  → 部位資料
      目前止損價               → 未觸發的條件單
      三段止盈價、還剩幾段      → 未成交的止盈限價單
      初始張數                 → 由「每段的張數 × 段數」回推

    回傳 True 表示成功接管。
    """
    pos = get_positions()
    if pos is None:
        log("接管部位：查詢失敗，稍後再試")
        return False
    if not pos:
        return False

    p = pos[0]
    side = p.get('side')
    contracts = float(p.get('contracts') or 0)
    entry = p.get('entryPrice')
    if not entry:
        try:
            entry = float((p.get('info') or {}).get('avgPx'))
        except (TypeError, ValueError):
            entry = None
    if not side or contracts <= 0 or not entry:
        log(f"接管部位：資料不完整（方向={side} 張數={contracts} 開倉價={entry}），放棄接管")
        return False

    # 止損
    sl_price, sl_id = None, None
    stops, _key = fetch_stop_orders()
    for o in stops or []:
        px = _order_trigger_price(o)
        if px:
            sl_price, sl_id = px, o.get('id')
            break

    # 止盈限價單：還沒成交的那幾張
    tp_prices, tp_ids, per = {}, [], None
    try:
        opens = exchange.fetch_open_orders(SYMBOL)
    except Exception as e:
        log(f"接管部位：查不到止盈委託（{e}），只重建部位本身")
        opens = []

    # 多單的止盈價由低到高是 1→2→3；空單相反
    opens = [o for o in opens if o.get('price')]
    opens.sort(key=lambda o: float(o['price']), reverse=(side == 'short'))

    if opens:
        per = float(opens[0].get('amount') or 0) or None
        # 還剩 n 張止盈單 → 代表前面 (TP_SPLITS - n) 段已經成交
        done_count = max(TP_SPLITS - len(opens), 0)
        for i, o in enumerate(opens):
            level = done_count + i + 1
            tp_prices[level] = float(o['price'])
            tp_ids.append(o.get('id'))
        tp_hit = list(range(1, done_count + 1))
    else:
        tp_hit = []

    initial = (per * TP_SPLITS) if per else contracts

    with lock:
        pstate.update({
            'active': True, 'side': side, 'entry': float(entry),
            'initial': initial, 'tp_hit': tp_hit, 'tp_prices': tp_prices,
            'sl_price': sl_price, 'sl_order_id': sl_id,
            'tp_order_ids': tp_ids, 'tp_on_exchange': bool(tp_ids),
        })

    log(f"🔄 已從交易所接管現有部位：{side} {contracts} 張 @開倉 {entry}")
    log(f"   推算初始 {initial} 張，已完成止盈 {tp_hit or '無'}，"
        f"剩餘止盈價位 {tp_prices or '無'}，目前止損 {sl_price}")
    if not tp_ids:
        log("   ⚠️ 交易所上沒有止盈委託 —— 出場只剩止損，分段出場已經失效")
    return True


def sync_position_state():
    """
    對帳：交易所才是唯一事實來源。

    止損被觸發、或你自己在 App 上平掉了，程式的狀態就過期了，
    這裡負責清乾淨並撤掉殘留委託。

    但「查不到」不等於「沒有」—— 查詢失敗時什麼都不做，
    而且要連續 FLAT_CONFIRM_NEEDED 次都確定是 0 才敢動手清理。
    回傳剩餘張數，或 None（查詢失敗）。
    """
    remaining = current_contracts()

    if remaining is None:
        return None                     # 查不到就當沒發生，下次再說

    if remaining > 0:
        _flat_confirm['count'] = 0
        # 有部位卻沒有紀錄（多半是程式重啟過）→ 先試著從交易所接管，
        # 接管成功就能繼續搬止損，不然這筆單等於沒人管
        if ADOPT_ON_START and not pstate['active']:
            adopt_existing_position()
        return remaining

    if not pstate['active']:
        _flat_confirm['count'] = 0
        return 0

    _flat_confirm['count'] += 1
    if _flat_confirm['count'] < FLAT_CONFIRM_NEEDED:
        log(f"部位查到 0 張，但只確認 {_flat_confirm['count']}/{FLAT_CONFIRM_NEEDED} 次，先不清理狀態")
        return 0

    log("已連續確認部位不存在（止損觸發或手動平倉），清理狀態與殘留委託")
    cancel_stop_orders()
    cancel_tp_orders()
    reset_pstate()
    get_equity(force=True)
    _flat_confirm['count'] = 0
    return 0


def execute_entry(action, notional, signal_text=""):
    want = 'long' if action == 'buy' else 'short'
    remaining = sync_position_state()

    if remaining is None:
        msg = "查不到目前部位，無法確認是否已有倉，這次不進場"
        log(f"🚫 {msg}")
        return {'action': 'rejected', 'reason': msg}

    if ONE_TRADE_AT_A_TIME and remaining > 0:
        with lock:
            done = list(pstate['tp_hit'])
            sl = pstate['sl_price']
            sd = pstate['side'] or '（無紀錄）'
        log(f"⏸️ 手上還有 {remaining} 張 {sd} 部位（已止盈 {done or '無'}，"
            f"止損 {sl}），依「只做一單」設定略過這則進場訊號")
        return {'action': 'skipped',
                'reason': '已有部位，只做一單，等止盈或止損完成'}

    pos = get_positions() or []
    if [p for p in pos if p.get('side') == want] and SAME_SIDE_POLICY == 'ignore':
        log(f"已有 {want} 部位，依設定略過")
        return {'action': 'skipped', 'reason': f'已持有 {want}'}
    if [p for p in pos if p.get('side') != want]:
        log("偵測到反向部位，先平倉再開新單")
        close_all()
        time.sleep(1)   # 等交易所更新部位，避免平倉還沒生效就開反向單

    now = time.time()
    with lock:
        while order_ts and now - order_ts[0] > 3600:
            order_ts.popleft()
        if len(order_ts) >= MAX_ORDERS_PER_HOUR:
            return {'action': 'rejected', 'reason': f'已達每小時 {MAX_ORDERS_PER_HOUR} 單上限'}
    # 先確認保證金夠不夠。用固定開倉價值模式時這是最常見的失敗原因：
    # 開倉價值寫得比帳戶撐得起的還大，交易所會直接拒單。
    if MAX_MARGIN_PCT and MAX_MARGIN_PCT > 0:
        eq = get_equity()
        need = notional / LEVERAGE if LEVERAGE else notional
        if eq and need > eq * MAX_MARGIN_PCT / 100:
            msg = (f"這一單需要保證金約 {need:.2f} USDT"
                   f"（開倉價值 {notional:.2f} ÷ {LEVERAGE}倍），"
                   f"超過權益 {eq:.2f} 的 {MAX_MARGIN_PCT}%。"
                   f"請調低 ORDER_VALUE、調高 LEVERAGE、或入金")
            log(f"🚫 {msg}")
            return {'action': 'rejected', 'reason': msg}

    try:
        exchange.set_leverage(LEVERAGE, SYMBOL, {'mgnMode': TD_MODE})
    except Exception as e:
        log(f"🚫 設定 {LEVERAGE} 倍槓桿失敗: {e}")
        log(f"   會沿用交易所現有的槓桿。如果那個值比 {LEVERAGE} 小，"
            f"這一單需要的保證金會比預期多，可能被拒單 —— 請到 OKX App 手動確認槓桿設定")

    r = open_position(action, notional, signal_text)
    if r.get('action') == 'rejected':
        return r
    with lock:
        order_ts.append(time.time())
        stats['orders'] += 1
    get_equity(force=True)
    return {'action': action, 'order': r}


# ------------------------------------------------------------------
# Webhook
# ------------------------------------------------------------------
@app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    raw = request.get_data(as_text=True) or ""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            data = {'text': raw}
    except Exception:
        data = {'text': raw}

    with lock:
        stats['received'] += 1
    log(f"📨 {raw[:250]}")

    # 密鑰驗證：JSON 走 secret 欄位，純文字則檢查密鑰有沒有出現在內容裡
    # 注意：compare_digest 傳 str 時只接受純 ASCII，密鑰含中文會拋 TypeError，
    # 所以一律先轉成 bytes 再比對（也才是定時安全比較的正確用法）
    # 密鑰有三種通過方式，任一即可：
    #   1. 網址路徑本身就含密鑰（TradingView alert() 模式唯一可行的做法）
    #   2. JSON 的 secret 欄位
    #   3. 純文字內容裡含密鑰
    path_ok = bool(WEBHOOK_SECRET) and WEBHOOK_SECRET in WEBHOOK_PATH
    sec = str(data.get('secret') or "")
    try:
        sec_ok = bool(sec) and hmac.compare_digest(sec.encode('utf-8'),
                                                   WEBHOOK_SECRET.encode('utf-8'))
    except Exception as e:
        log(f"⚠️ 密鑰比對發生例外，視為不符: {e}")
        sec_ok = False
    if not (WEBHOOK_SECRET and (path_ok or sec_ok or WEBHOOK_SECRET in raw)):
        with lock:
            stats['rejected'] += 1
        log("🚫 密鑰不符，拒絕")
        return jsonify({'ok': False, 'error': 'bad secret'}), 403

    key = str(data.get('id') or data.get('time') or raw)[:300]
    now = time.time()
    with lock:
        for k, t in list(seen.items()):
            if now - t > DEDUPE_WINDOW_SEC:
                seen.pop(k, None)
        dup = key in seen
        seen[key] = now
    if dup:
        log("↩️ 重複訊號，略過")
        return jsonify({'ok': True, 'skipped': 'duplicate'}), 200

    if ASYNC_PROCESSING:
        # 先回應，再在背景做。TradingView 不必等我們跟交易所往返完。
        threading.Thread(target=process_signal, args=(raw, data), daemon=True).start()
        return jsonify({'ok': True, 'accepted': True}), 200
    return jsonify(process_signal(raw, data)), 200


def process_signal(raw, data):
    """
    實際處理一則訊號。同步或背景執行都是走這個函式，
    所以測試涵蓋到的邏輯跟正式運作完全一樣，差別只在誰呼叫它。
    """
    action, why, extra = parse_action(data)
    if action is None:
        log(f"❓ 無法判斷：{why}")
        return {'ok': False, 'error': why}

    # ---- 止盈 ----
    if action == 'tp':
        lvl, prc = extra.get('level'), extra.get('price')
        log(f"➡️ 判定 止盈{lvl if lvl else '?'}"
            f"{f'（價位 {prc}）' if prc else ''}　{why}")
        if DRY_RUN:
            log("🧪 DRY_RUN：只記錄，沒有送出任何委託")
            return {'ok': True, 'dry_run': True, 'action': 'tp',
                    'level': lvl, 'price': prc, 'reason': why}
        if not ensure_markets():
            return {'ok': False, 'error': '市場資料未就緒'}
        try:
            return {'ok': True, 'result': handle_tp(lvl, prc)}
        except Exception as e:
            log(f"❌ 止盈處理失敗: {e}")
            traceback.print_exc()
            return {'ok': False, 'error': str(e)}

    # ---- 全平 ----
    if action == 'close':
        log(f"➡️ 判定 CLOSE（{why}）")
        if DRY_RUN:
            log("🧪 DRY_RUN：只記錄，沒有送出任何委託")
            return {'ok': True, 'dry_run': True, 'action': 'close', 'reason': why}
        if not ensure_markets():
            return {'ok': False, 'error': '市場資料未就緒'}
        try:
            return {'ok': True, 'result': {'action': 'close',
                                       'closed': close_all() or '目前無部位'}}
        except Exception as e:
            log(f"❌ 執行失敗: {e}")
            traceback.print_exc()
            return {'ok': False, 'error': str(e)}

    # ---- 進場 ----
    notional, size_why = resolve_notional(data)
    if notional is None:
        log(f"🚫 倉位計算失敗：{size_why}")
        return {'ok': False, 'error': size_why}

    log(f"➡️ 判定 {action.upper()}（{why}） 倉位：{size_why}")

    if DRY_RUN:
        # 演練模式也把張數換算跑一遍。不做的話，「倉位太小分不出批」這種問題
        # 要等到真的開真單才會浮現，失去演練的意義。
        try:
            if ensure_markets():
                px = exchange.fetch_ticker(SYMBOL)['last']
                amt, reject = calc_amount(notional, px)
                if amt > 0:
                    log(f"🧪 演練換算：{amt} 張 @約 {px:.2f}，"
                        f"每批 {amt / TP_SPLITS:.2f} 張 → 可以正常分批")
                else:
                    log(f"🧪 演練換算：這筆進場「會被拒絕」→ {reject}")
        except Exception as e:
            log(f"🧪 演練換算跳過（查不到價格）: {e}")
        log("🧪 DRY_RUN：只記錄，沒有送出任何委託")
        return {'ok': True, 'dry_run': True, 'action': action,
                'notional': round(notional, 2), 'sizing': size_why,
                'reason': why}

    if not ensure_markets():
        return {'ok': False, 'error': '市場資料未就緒'}
    try:
        return {'ok': True, 'result': execute_entry(action, notional, raw)}
    except Exception as e:
        log(f"❌ 執行失敗: {e}")
        traceback.print_exc()
        return {'ok': False, 'error': str(e)}


@app.route('/health')
def health():
    return jsonify({'ok': True, 'dry_run': DRY_RUN, 'demo': USE_DEMO,
                    'stats': stats, 'position': pstate})


def came_through_tunnel():
    """
    判斷這個請求是不是從外網（Cloudflare 隧道）進來的。

    cloudflared 轉發時會補上 CF-Connecting-IP / X-Forwarded-For 標頭，
    你自己在本機開 127.0.0.1:8081 則不會有。
    """
    return bool(request.headers.get('CF-Connecting-IP')
                or request.headers.get('X-Forwarded-For'))


@app.route('/')
def index():
    # 控制台會顯示完整日誌。隧道是公開的，不擋的話等於把日誌攤給所有人看。
    if came_through_tunnel() and not ALLOW_REMOTE_DASHBOARD:
        return ("控制台只開放本機存取。<br>"
                "請在這台電腦上開 <code>http://127.0.0.1:%d</code>。<br>"
                "真的要從外面看，把 my_config.py 的 "
                "<code>ALLOW_REMOTE_DASHBOARD</code> 設成 True。" % PORT), 403

    with lock:
        rows = "<br>".join(logs)
        s = dict(stats)
        p = dict(pstate)
    banner = ("🧪 DRY_RUN 演練中 — 不會下任何真單" if DRY_RUN else
              ("⚠️ 下單中（模擬盤）" if USE_DEMO else "🔴 下單中（正式實盤）"))
    color = "#4fc3f7" if DRY_RUN else ("#ffb74d" if USE_DEMO else "#ff5252")
    _cap = f"上限 {MAX_NOTIONAL}" if (MAX_NOTIONAL and MAX_NOTIONAL > 0) else "無上限"
    if POSITION_SIZING == "equity_pct":
        sizing = f"權益 × {EQUITY_PCT}%（{SIZING_BASIS}），{_cap} / 下限 {MIN_NOTIONAL}"
    else:
        _v = ORDER_VALUE if POSITION_SIZING == "order_value" else FIXED_NOTIONAL
        sizing = (f"每單固定開倉價值 {_v} USDT（{LEVERAGE}倍 → 保證金約 "
                  f"{_v / LEVERAGE:.2f}），{_cap}")
    eq = f"{s['equity']} USDT" if s.get('equity') is not None else "尚未查詢"
    # 交易所才是真的。以前這裡只看程式紀錄，重啟後明明有倉卻顯示「無部位」，
    # 看到的人會以為沒事，其實有一筆單沒人管。
    live = None
    if not DRY_RUN:
        try:
            live = current_contracts()
        except Exception:
            live = None

    if p['active']:
        posinfo = (f"{p['side']} ｜ 開倉 {p['entry']} ｜ 初始 {p['initial']} 張 ｜ "
                   f"已止盈 {p['tp_hit'] or '無'} ｜ 目前止損 {p['sl_price']} ｜ "
                   f"止盈{'掛在交易所 ✅' if p.get('tp_on_exchange') else '靠程式收訊號 ⚠️'}")
        if live is not None and live > 0:
            posinfo += f" ｜ 交易所實際 {live} 張"
    elif live is None:
        posinfo = "程式無紀錄；交易所部位查詢失敗"
    elif live > 0:
        posinfo = (f"⚠️ 交易所有 {live} 張，但程式沒有紀錄"
                   f"（重啟過？）→ 下次檢查會自動接管")
    else:
        posinfo = "無部位"
    return f"""<html><head><meta charset="utf-8"><title>OKX Webhook</title>
<meta http-equiv="refresh" content="3"><style>
body{{background:#12121a;color:#eee;font-family:sans-serif;padding:16px}}
h2{{color:#00e676;margin:0}} .b{{border:1px solid {color};color:{color};
padding:10px;border-radius:6px;margin:12px 0;text-align:center;font-weight:bold}}
code{{background:#0d0d13;padding:2px 6px;border-radius:4px;color:#ffb74d}}
pre{{background:#0d0d13;padding:12px;border-radius:6px;font-size:13px;
line-height:1.7;white-space:pre-wrap;word-break:break-all}}</style></head><body>
<h2>TradingView → OKX Webhook</h2><div class="b">{banner}</div>
<p>收到 {s['received']} 則 ｜ 拒絕 {s['rejected']} ｜ 已開倉 {s['orders']}
 ｜ 路徑 <code>{WEBHOOK_PATH}</code></p>
<p>帳戶權益：<code>{eq}</code> ｜ 倉位規則：<code>{sizing}</code></p>
<p>目前部位：<code>{posinfo}</code></p>
<pre>{rows}</pre></body></html>"""


if __name__ == '__main__':
    log("=" * 54)
    if _LOCAL_CONFIG:
        log(f"已載入 my_config.py，覆蓋 {len(_LOCAL_CONFIG)} 項設定：{', '.join(_LOCAL_CONFIG)}")
    else:
        log("沒有 my_config.py，使用主程式裡的設定"
            "（建議把金鑰搬到 my_config.py，更新主程式才不會被蓋掉）")
    log(f"啟動 port {PORT}  路徑 {WEBHOOK_PATH}")
    log(f"環境：{'模擬盤' if USE_DEMO else '⚠️ 正式實盤'}")
    log(f"模式：{'DRY_RUN 演練' if DRY_RUN else '🔴 會下真單'}")
    log(f"商品 {SYMBOL}  槓桿 {LEVERAGE}倍  {TD_MODE}")
    cap = f"上限 {MAX_NOTIONAL}" if (MAX_NOTIONAL and MAX_NOTIONAL > 0) else "無上限"
    if POSITION_SIZING == "equity_pct":
        log(f"倉位：每單 = 帳戶權益 × {EQUITY_PCT}%（基準 {SIZING_BASIS}）"
            f"，下限 {MIN_NOTIONAL} {cap} USDT")
    else:
        _v = ORDER_VALUE if ORDER_VALUE else FIXED_NOTIONAL
        log(f"倉位：每單固定開倉價值 {_v} USDT（不看本金與槓桿），下限 {MIN_NOTIONAL} {cap}")
        if MAX_NOTIONAL and 0 < MAX_NOTIONAL < _v:
            log(f"🚫 注意：ORDER_VALUE={_v} 大於 MAX_NOTIONAL={MAX_NOTIONAL}，"
                f"每一單都會被壓回 {MAX_NOTIONAL}。請把上限調高或設成 0")
    log(f"初始止損 {DEFAULT_SL_PCT}%")
    log(f"出場：{TP_SPLITS} 段止盈，各平 1/{TP_SPLITS}；"
        f"止盈1後止損→{SL_AFTER_TP1}，止盈2後止損→{SL_AFTER_TP2}")
    log(f"止盈掛法：{'交易所端限價單（程式當掉也會執行）' if TP_ON_EXCHANGE else '等訊號由程式市價平倉'}")
    log(f"回應方式：{'立即回應、背景處理（避免 TradingView 逾時）' if ASYNC_PROCESSING else '同步處理'}")
    log(f"進場限制：{'同時只做一單，有部位時略過所有進場訊號' if ONE_TRADE_AT_A_TIME else '允許反向翻倉'}")
    if not DRY_RUN and not USE_DEMO:
        log("🔴 正式實盤 + 真實下單：每則訊號都會動到真錢")
    log("=" * 54)
    ensure_markets()
    eq0 = get_equity(force=True)
    if eq0 is not None:
        if POSITION_SIZING in ("order_value", "fixed"):
            notional0 = ORDER_VALUE if ORDER_VALUE else FIXED_NOTIONAL
        elif SIZING_BASIS == "margin":
            notional0 = eq0 * EQUITY_PCT / 100 * LEVERAGE
        else:
            notional0 = eq0 * EQUITY_PCT / 100
        if MAX_NOTIONAL and 0 < MAX_NOTIONAL < notional0:
            notional0 = MAX_NOTIONAL
        log(f"目前權益 {eq0:.2f} USDT → 每單開倉價值約 {notional0:.2f} USDT"
            f"（佔用保證金約 {notional0 / LEVERAGE:.2f}，{LEVERAGE}倍）")
        # 把風險換算成看得懂的數字。每次啟動都印，免得金額改大了自己沒感覺。
        if DEFAULT_SL_PCT > 0:
            loss = notional0 * DEFAULT_SL_PCT / 100
            pct = loss / eq0 * 100 if eq0 else 0
            note = ""
            if pct >= 20:
                note = "  ← 單筆就吃掉權益兩成以上，連錯幾次會很痛"
            elif pct >= 10:
                note = "  ← 單筆超過權益一成"
            log(f"風險估算：觸發 {DEFAULT_SL_PCT}% 止損約虧 {loss:.2f} USDT"
                f"，佔目前權益 {pct:.1f}%{note}")
        else:
            log("⚠️ DEFAULT_SL_PCT=0，沒有掛止損。程式當掉或斷線時完全沒有保護")
    else:
        log("⚠️ 啟動時查不到帳戶權益。若金鑰還沒填這是正常的；"
            "若已填請檢查金鑰、IP白名單、以及模擬/實盤設定是否一致")
    if ADOPT_ON_START and not DRY_RUN:
        try:
            if not adopt_existing_position():
                log("啟動時沒有既有部位")
        except Exception as e:
            log(f"⚠️ 接管既有部位時發生例外（不影響啟動）: {e}")

    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT, threaded=True)
