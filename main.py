# -*- coding: utf-8 -*-
# ==============================================================================
# Trend-Pullback Bot v2
#   * فقط Long اسپات، تایم‌فریم ۱ ساعته (ورود) + ۴ ساعته (روند)، فقط روی کندل بسته‌شده
#   * دو ستاپ: Pullback در روند صعودی  +  Breakout با حجم
#   * خروج: حد ضرر ساختاری، TP1 جزئی، تریلینگ ATR، خروج زمانی
#   * رژیم بازار (TREND / CHOP / RISK_OFF) با قوانین عددی + هیسترزیس
#   * AI فقط رژیم کلی بازار را بررسی می‌کند (نه تنظیم عددی، نه وتوی تک‌تک معاملات)
#   * بک‌تستر داخلی:  python _bot_.py backtest 365
# ==============================================================================
import os
import sys
import json
import time
import html
import logging
import threading
import gc
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("trading_signals.log", encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("bot")


# ============================== تنظیمات ==============================
class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    PERSONAL_CHAT_ID = os.getenv("PERSONAL_CHAT_ID")
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

    SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "AVAX/USDT",
               "NEAR/USDT", "ADA/USDT", "DOGE/USDT", "LINK/USDT", "PAXG/USDT", "LTC/USDT"]
    GROUPS = {"BTC/USDT": "majors", "ETH/USDT": "majors", "BNB/USDT": "majors", "LTC/USDT": "majors",
              "SOL/USDT": "alt", "AVAX/USDT": "alt", "NEAR/USDT": "alt", "ADA/USDT": "alt",
              "LINK/USDT": "alt", "XRP/USDT": "alt", "DOGE/USDT": "meme", "PAXG/USDT": "gold"}
    MAX_PER_GROUP = 2

    CAPITAL = float(os.getenv("VIRTUAL_CAPITAL_USDT", 10000))
    RISK_PCT = float(os.getenv("RISK_PER_TRADE_PCT", 1.0))      # ریسک پایه هر معامله (٪ سرمایه)
    NOTIONAL_CAP_PCT = float(os.getenv("NOTIONAL_CAP_PCT", 30))  # سقف ارزش هر پوزیشن (٪ سرمایه)

    # هزینه‌های واقعی (بایننس اسپات: ۰٫۱٪ هر طرف؛ با BNB ۰٫۰۷۵٪)
    FEE_PCT = float(os.getenv("EXCHANGE_TAKER_FEE_PCT", 0.1))
    SLIPPAGE_PCT = float(os.getenv("SLIPPAGE_PCT", 0.05))

    # خروج
    TP1_R = 1.5
    TP1_FRACTION = 0.4
    TRAIL_ATR = 2.5
    TIME_STOP_H = 24      # اگر بعد از ۲۴ ساعت هنوز به +0.5R نرسیده، بسته شود
    MAX_HOLD_H = 120
    LOSS_COOLDOWN_H = 12
    REENTRY_COOLDOWN_H = 6

    # رژیم
    PERF_WINDOW_DAYS = 5
    PERF_MIN_TRADES = 10
    PERF_BAD_AVG_R = -0.25
    AI_REGIME_INTERVAL_H = float(os.getenv("AI_REGIME_INTERVAL_H", 6))
    AI_ENABLED = os.getenv("AI_REGIME_ENABLED", "1") == "1"

    MONITOR_SECONDS = 30
    STATE_FILE = "bot_state_v2.json"
    JOURNAL_FILE = "journal_v2.json"
    TRADES_FILE = "paper_trades_v2.json"

    def validate(self):
        miss = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not getattr(self, k)]
        if miss:
            raise ValueError("متغیرهای محیطی تنظیم نشدن: " + ", ".join(miss))


CFG = Config()

PROFILES = {
    "TREND":    {"setups": {"pullback", "breakout"}, "risk_mult": 1.0, "max_open": 3},
    "CHOP":     {"setups": {"pullback"},             "risk_mult": 0.6, "max_open": 2},
    "RISK_OFF": {"setups": set(),                    "risk_mult": 0.0, "max_open": 0},
}
REGIME_LABEL = {"TREND": "🟢 روندی", "CHOP": "🟡 رنج/نامطمئن", "RISK_OFF": "🔴 ریسک‌گریز (بدون ورود جدید)"}
SETUP_LABEL = {"pullback": "برگشت از اصلاح در روند صعودی", "breakout": "شکست مقاومت با حجم"}


# ============================== داده ==============================
HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]


def _get(path: str, params: dict):
    last = None
    for h in HOSTS:
        try:
            r = requests.get(h + path, params=params, timeout=12)
            if r.status_code == 200:
                return r.json()
            last = f"{h} HTTP {r.status_code}"
        except Exception as e:
            last = f"{h} {e}"
    raise RuntimeError(last)


def klines(symbol: str, interval: str, limit: int = 500, start: Optional[int] = None) -> pd.DataFrame:
    p = {"symbol": symbol.replace("/", ""), "interval": interval, "limit": limit}
    if start is not None:
        p["startTime"] = int(start)
    raw = _get("/api/v3/klines", p)
    rows = [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), r[6]] for r in raw]
    return pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time"])


def last_price(symbol: str) -> float:
    return float(_get("/api/v3/ticker/price", {"symbol": symbol.replace("/", "")})["price"])


def closed_only(df: pd.DataFrame, now_ms: Optional[float] = None) -> pd.DataFrame:
    """کندل در حال شکل‌گیری حذف می‌شود (ایراد اصلی نسخه‌ی قبلی: اندیکاتورها روی کندل ناقص حساب می‌شد)."""
    now_ms = now_ms or time.time() * 1000
    return df[df["close_time"] < now_ms].reset_index(drop=True)


STEP_MS = {"1h": 3_600_000, "4h": 14_400_000, "1m": 60_000}


def history(symbol: str, interval: str, bars: int) -> pd.DataFrame:
    step = STEP_MS[interval]
    start = int(time.time() * 1000) - bars * step
    parts = []
    while True:
        df = klines(symbol, interval, 1000, start)
        if df.empty:
            break
        parts.append(df)
        start = int(df["open_time"].iloc[-1]) + step
        if len(df) < 2 or start > time.time() * 1000:
            break
        time.sleep(0.15)
    out = pd.concat(parts).drop_duplicates("open_time").reset_index(drop=True)
    return closed_only(out)


# ============================== اندیکاتورها ==============================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    c = d["close"]
    d["ema20"] = c.ewm(span=20, adjust=False).mean()
    d["ema50"] = c.ewm(span=50, adjust=False).mean()
    d["ema200"] = c.ewm(span=200, adjust=False).mean()

    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))

    pc = c.shift()
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    d["atr"] = atr

    um = d["high"].diff()
    dm = -d["low"].diff()
    plus = pd.Series(np.where((um > dm) & (um > 0), um, 0.0), index=d.index)
    minus = pd.Series(np.where((dm > um) & (dm > 0), dm, 0.0), index=d.index)
    pdi = 100 * plus.ewm(alpha=1 / 14, adjust=False).mean() / atr
    mdi = 100 * minus.ewm(alpha=1 / 14, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    d["adx"] = dx.ewm(alpha=1 / 14, adjust=False).mean()

    d["vol_sma"] = d["volume"].rolling(20).mean()
    d["hh20"] = d["high"].rolling(20).max().shift(1)
    rel = atr / c
    d["atr_pctl"] = (rel.rolling(200).apply(lambda x: (x[:-1] < x[-1]).mean(), raw=True) * 100).fillna(50)
    return d


def prepare(df1: pd.DataFrame, df4: pd.DataFrame) -> pd.DataFrame:
    """اندیکاتورهای ۱h + وضعیت روند ۴h (فقط از کندل‌های ۴h که تا آن لحظه بسته شده‌اند)."""
    d1 = add_indicators(df1)
    d4 = add_indicators(df4)
    c = d4["close"]
    up = (c > d4["ema50"]) & (d4["ema50"] > d4["ema200"]) & (d4["ema50"] > d4["ema50"].shift(6))
    dn = (c < d4["ema50"]) & (d4["ema50"] < d4["ema200"])
    d4["t4"] = np.where(up, "UP", np.where(dn, "DOWN", "FLAT"))
    d4.iloc[:200, d4.columns.get_loc("t4")] = "FLAT"   # گرم‌شدن EMA200
    right = d4[["close_time", "t4", "adx"]].rename(columns={"close_time": "ct4", "adx": "adx4"})
    out = pd.merge_asof(d1.sort_values("close_time"), right.sort_values("ct4"),
                        left_on="close_time", right_on="ct4", direction="backward")
    out["t4"] = out["t4"].fillna("FLAT")
    return out


# ============================== منطق سیگنال ==============================
def find_setup(df: pd.DataFrame, i: int, allowed: set) -> Optional[dict]:
    """بررسی کندل بسته‌شده‌ی i. همان تابع هم در لایو و هم در بک‌تست استفاده می‌شود."""
    if i < 60 or not allowed:
        return None
    if df["t4"].values[i] != "UP":
        return None
    r = df.iloc[i]
    p = df.iloc[i - 1]
    if pd.isna(r["atr"]) or pd.isna(r["ema50"]) or pd.isna(r["rsi"]) or pd.isna(r["vol_sma"]) or r["vol_sma"] <= 0:
        return None
    if r["atr_pctl"] >= 97:        # نوسان غیرعادی
        return None
    close, atr = float(r["close"]), float(r["atr"])
    volr = float(r["volume"] / r["vol_sma"])

    if "pullback" in allowed:
        lows = df["low"].values[i - 7:i + 1]
        emas = df["ema20"].values[i - 7:i + 1]
        touched = bool((lows <= emas).any())
        rsi_min = float(df["rsi"].values[i - 5:i + 1].min())
        if (close > r["ema50"] and r["ema20"] > r["ema50"] and touched and rsi_min <= 45
                and close > r["open"] and close > p["high"] and 45 <= r["rsi"] <= 65
                and (close - r["ema20"]) <= 1.5 * atr and volr >= 0.7):
            sl = min(float(lows.min()) - 0.3 * atr, close - 1.2 * atr)
            if close - sl <= 3.5 * atr:
                return {"setup": "pullback", "sl": sl, "atr": atr, "close": close}

    if "breakout" in allowed:
        rng = float(r["high"] - r["low"])
        if (rng > 0 and r["adx"] >= 22 and not pd.isna(r["hh20"]) and close > r["hh20"] and volr >= 1.4
                and (close - r["low"]) / rng >= 0.7 and (close - r["ema20"]) <= 2.2 * atr
                and r["rsi"] < 78 and close > r["ema50"]):
            return {"setup": "breakout", "sl": close - 2.0 * atr, "atr": atr, "close": close}
    return None


def size_position(entry: float, sl: float, risk_mult: float) -> Optional[dict]:
    risk_per_unit = entry - sl
    if risk_per_unit <= 0 or risk_mult <= 0:
        return None
    risk_usdt = CFG.CAPITAL * CFG.RISK_PCT / 100 * risk_mult
    qty = risk_usdt / risk_per_unit
    qty = min(qty, CFG.CAPITAL * CFG.NOTIONAL_CAP_PCT / 100 / entry)
    return {"qty": qty, "risk_usdt": qty * risk_per_unit}


def build_trade(symbol: str, plan: dict, entry: float, ts_ms: int, regime: str, risk_mult: float) -> Optional[dict]:
    """از پلان سیگنال و قیمت ورود واقعی، یک معامله‌ی کامل می‌سازد (لایو و بک‌تست یکسان)."""
    sl, atr = plan["sl"], plan["atr"]
    risk = entry - sl
    if risk < 1.0 * atr:
        sl = entry - 1.0 * atr
        risk = entry - sl
    if risk > 4.0 * atr or entry - plan["close"] > 0.6 * atr:
        return None   # قیمت از نقطه‌ی سیگنال فرار کرده یا استاپ خیلی دور است
    pos = size_position(entry, sl, risk_mult)
    if not pos or pos["qty"] <= 0:
        return None
    return {
        "symbol": symbol, "setup": plan["setup"], "regime": regime, "entry": entry, "sl": sl, "init_sl": sl,
        "tp1": entry + CFG.TP1_R * risk, "atr": atr, "qty": pos["qty"], "qty_left": pos["qty"],
        "risk_usdt": pos["risk_usdt"], "tp1_hit": False, "highest": entry, "pnl": 0.0,
        "open_ts": int(ts_ms), "last_ts": int(ts_ms),
    }


def advance(tr: dict, ts: int, o: float, h: float, l: float, c: float, candle_ms: int) -> List[dict]:
    """
    یک کندل را روی معامله اعمال می‌کند. اگر در یک کندل هم استاپ و هم هدف ممکن باشد،
    محافظه‌کارانه استاپ اول حساب می‌شود. خروجی: لیست رویدادها.
    """
    events: List[dict] = []
    fee, slip = CFG.FEE_PCT / 100, CFG.SLIPPAGE_PCT / 100

    def leg(qty: float, price: float, reason: str, final: bool, market: bool):
        fill = price * (1 - slip) if market else price
        pnl = (fill - tr["entry"]) * qty - fee * (tr["entry"] + fill) * qty
        tr["pnl"] += pnl
        tr["qty_left"] -= qty
        events.append({"price": fill, "qty": qty, "pnl": pnl, "r": pnl / tr["risk_usdt"], "reason": reason,
                       "final": final, "pct": qty / tr["qty"] * 100})

    tr["last_ts"] = ts
    if l <= tr["sl"]:
        fill_ref = o if o < tr["sl"] else tr["sl"]       # گپ به پایین
        reason = "تریلینگ/سر‌به‌سر بعد از TP1" if tr["tp1_hit"] else "برخورد به حد ضرر"
        leg(tr["qty_left"], fill_ref, reason, True, True)
        return events

    if not tr["tp1_hit"] and h >= tr["tp1"]:
        q = tr["qty"] * CFG.TP1_FRACTION
        leg(q, tr["tp1"], f"TP1 ({CFG.TP1_R}R) - بستن {int(CFG.TP1_FRACTION * 100)}٪", False, False)
        tr["tp1_hit"] = True
        tr["sl"] = max(tr["sl"], tr["entry"] * (1 + 2.5 * fee))    # سر‌به‌سر با پوشش کارمزد

    tr["highest"] = max(tr["highest"], h)
    if tr["tp1_hit"]:
        tr["sl"] = max(tr["sl"], tr["highest"] - CFG.TRAIL_ATR * tr["atr"])

    age_h = (ts + candle_ms - tr["open_ts"]) / 3_600_000
    risk_per_unit = tr["entry"] - tr["init_sl"]
    if tr["qty_left"] > 0 and not tr["tp1_hit"] and age_h >= CFG.TIME_STOP_H and c < tr["entry"] + 0.5 * risk_per_unit:
        leg(tr["qty_left"], c, "خروج زمانی (معامله بعد از ۲۴ ساعت پیشروی نکرد)", True, True)
    elif tr["qty_left"] > 0 and age_h >= CFG.MAX_HOLD_H:
        leg(tr["qty_left"], c, "خروج زمانی (حداکثر مدت نگهداری)", True, True)
    return events


# ============================== رژیم بازار ==============================
class RegimeEngine:
    LEVELS = ["TREND", "CHOP", "RISK_OFF"]

    def __init__(self, saved: Optional[dict] = None):
        s = saved or {}
        self.current: Optional[str] = s.get("current")
        self.pending: Optional[str] = s.get("pending")
        self.pending_since: float = s.get("pending_since", 0)
        self.bias: int = s.get("bias", 0)
        self.bias_until: float = s.get("bias_until", 0)
        self.ai_note: str = s.get("ai_note", "")
        self.last_ai_ms: float = s.get("last_ai_ms", 0)
        self.last_raw: Optional[int] = s.get("last_raw")

    def dump(self) -> dict:
        return {k: getattr(self, k) for k in
                ("current", "pending", "pending_since", "bias", "bias_until", "ai_note", "last_ai_ms", "last_raw")}

    @staticmethod
    def raw_level(f: dict) -> int:
        if f["btc_t4"] == "DOWN" or f["breadth"] < 0.25 or f["btc_vol_pctl"] >= 97:
            return 2
        if f["btc_t4"] == "FLAT" or f["breadth"] < 0.45 or (f["btc_adx4"] or 0) < 15:
            return 1
        return 0

    @staticmethod
    def perf_stats(records: List[dict], now_ms: float) -> dict:
        recent = [r for r in records if now_ms - r["close_ts"] <= CFG.PERF_WINDOW_DAYS * 86_400_000]
        if not recent:
            return {"n": 0, "avg_r": None, "win_rate": None}
        return {"n": len(recent), "avg_r": round(float(np.mean([r["r"] for r in recent])), 2),
                "win_rate": round(sum(1 for r in recent if r["r"] > 0) / len(recent) * 100, 1)}

    def set_bias(self, bias: int, now_ms: float, note: str):
        self.bias, self.bias_until, self.ai_note, self.last_ai_ms = bias, now_ms + 8 * 3_600_000, note, now_ms

    def update(self, f: dict, records: List[dict], now_ms: float):
        raw = self.raw_level(f)
        self.last_raw = raw
        st = self.perf_stats(records, now_ms)
        lvl = raw
        if st["n"] >= CFG.PERF_MIN_TRADES and st["avg_r"] <= CFG.PERF_BAD_AVG_R:
            lvl += 1       # عملکرد واقعی اخیر واقعاً بد بوده (نه فقط چند ضرر پشت هم)
        if now_ms < self.bias_until:
            lvl += self.bias
        lvl = max(0, min(2, lvl))
        target = self.LEVELS[lvl]

        if self.current is None:
            self.current = target
            return self.current, True
        if target == self.current:
            self.pending = None
            return self.current, False
        if self.pending != target:
            self.pending, self.pending_since = target, now_ms
        going_defensive = lvl > self.LEVELS.index(self.current)
        need_ms = (0.9 if going_defensive else 3.9) * 3_600_000    # ورود به حالت دفاعی سریع، خروج از آن کند
        if now_ms - self.pending_since >= need_ms:
            self.current, self.pending = target, None
            return self.current, True
        return self.current, False


def regime_features(rows: Dict[str, pd.Series]) -> Optional[dict]:
    btc = rows.get("BTC/USDT")
    if btc is None:
        return None
    breadth = float(np.mean([1.0 if r["t4"] == "UP" else 0.0 for r in rows.values()]))
    return {"btc_t4": btc["t4"], "btc_adx4": None if pd.isna(btc["adx4"]) else round(float(btc["adx4"]), 1),
            "btc_vol_pctl": round(float(btc["atr_pctl"]), 1), "breadth": round(breadth, 2),
            "btc_rsi_1h": round(float(btc["rsi"]), 1) if not pd.isna(btc["rsi"]) else None}


# ============================== AI: تحلیل‌گر رژیم ==============================
class AIAnalyst:
    """
    AI دیگر پارامتر عددی تنظیم نمی‌کند و تک‌تک معاملات را وتو نمی‌کند (آن کار نویز تصادفی اضافه می‌کرد).
    فقط هر چند ساعت یک‌بار رژیم کلی را بررسی می‌کند و می‌تواند حداکثر «یک پله» از رژیم عددی فاصله بگیرد:
      - دفاعی‌تر شدن: اطمینان >= 60
      - تهاجمی‌تر شدن: اطمینان >= 80 (و هرگز از RISK_OFF مستقیم به TREND نمی‌رود)
    اگر AI در دسترس نباشد سیستم دقیقاً با قوانین عددی ادامه می‌دهد.
    """
    def advise(self, features: dict, det_regime: str, stats: dict) -> Optional[dict]:
        if not CFG.GROQ_API_KEY or not CFG.AI_ENABLED:
            return None
        prompt = (
            "You are a cautious crypto market-regime analyst for a LONG-ONLY spot trend-following system "
            "(1h entries, 4h trend filter). Pick the regime that best fits the data. Prefer capital preservation; "
            "only choose a LESS defensive regime than the rule-based one if the evidence is strong.\n"
            f"Market features: {json.dumps(features)}\n"
            f"Rule-based regime: {det_regime}\n"
            f"Bot recent live performance (last {CFG.PERF_WINDOW_DAYS} days): {json.dumps(stats)}\n"
            "Regimes: TREND (healthy uptrend, pullbacks and breakouts work), CHOP (mixed/range, only pullbacks, "
            "smaller size), RISK_OFF (downtrend/panic, no new longs).\n"
            'Reply ONLY with JSON: {"regime":"TREND|CHOP|RISK_OFF","confidence":0-100,"reason":"one short sentence in Persian"}'
        )
        body = {"model": CFG.GROQ_MODEL, "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1, "reasoning_effort": "low", "max_tokens": 900}
        headers = {"Authorization": f"Bearer {CFG.GROQ_API_KEY}", "Content-Type": "application/json"}
        for attempt in range(3):
            try:
                r = requests.post("https://api.groq.com/openai/v1/chat/completions", json=body, headers=headers, timeout=25)
                if r.status_code == 429:
                    time.sleep(3 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    logger.warning(f"AI رژیم: پاسخ {r.status_code}")
                    return None
                txt = r.json()["choices"][0]["message"]["content"].strip()
                res = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
                reg, conf = str(res.get("regime", "")).upper(), int(res.get("confidence", 0))
                if reg not in RegimeEngine.LEVELS:
                    return None
                diff = RegimeEngine.LEVELS.index(reg) - RegimeEngine.LEVELS.index(det_regime)
                bias = 0
                if diff > 0 and conf >= 60:
                    bias = 1
                elif diff < 0 and conf >= 80 and det_regime != "RISK_OFF":
                    bias = -1
                return {"bias": bias, "regime": reg, "confidence": conf, "reason": str(res.get("reason", ""))[:250]}
            except Exception as e:
                logger.warning(f"AI رژیم: خطا {e}")
                time.sleep(2)
        return None


# ============================== تلگرام ==============================
class Telegram:
    def __init__(self):
        self.base = f"https://api.telegram.org/bot{CFG.TELEGRAM_BOT_TOKEN}"

    def _send(self, chat_id: str, text: str):
        try:
            r = requests.post(f"{self.base}/sendMessage",
                              json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
            if r.status_code != 200:
                logger.error(f"تلگرام {r.status_code}: {r.text[:150]}")
        except Exception as e:
            logger.error(f"تلگرام: {e}")

    def signal(self, text: str):
        self._send(CFG.TELEGRAM_CHAT_ID, text)

    def personal(self, text: str):
        self._send(CFG.PERSONAL_CHAT_ID or CFG.TELEGRAM_CHAT_ID, text)


def fmt(p: float) -> str:
    return f"{p:.6g}"


# ============================== ژورنال ==============================
class Journal:
    def __init__(self, path: str):
        self.path = path
        self.records: List[dict] = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.records = json.load(f)
            except Exception:
                self.records = []

    def add(self, rec: dict):
        self.records.append(rec)
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.records, f)
        except Exception as e:
            logger.error(f"ذخیره ژورنال: {e}")

    def summary(self, day: str) -> Optional[str]:
        rs = [r for r in self.records if datetime.fromtimestamp(r["close_ts"] / 1000, timezone.utc).strftime("%Y-%m-%d") == day]
        if not rs:
            return None
        wins = [r for r in rs if r["r"] > 0]
        return (f"📊 <b>گزارش روزانه {day}</b>\nمعاملات بسته‌شده: {len(rs)} | برد: {len(wins)} | باخت: {len(rs) - len(wins)}\n"
                f"نرخ برد: {len(wins) / len(rs) * 100:.0f}% | میانگین R: {np.mean([r['r'] for r in rs]):+.2f}\n"
                f"سود/زیان: {sum(r['pnl'] for r in rs):+.2f} USDT")


# ============================== معامله‌ی مجازی (لایو) ==============================
class PaperTrader:
    def __init__(self, tg: Telegram, journal: Journal, on_close):
        self.tg, self.journal, self.on_close = tg, journal, on_close
        self.lock = threading.RLock()
        self.trades: Dict[str, dict] = {}
        if os.path.exists(CFG.TRADES_FILE):
            try:
                with open(CFG.TRADES_FILE, "r", encoding="utf-8") as f:
                    self.trades = json.load(f)
            except Exception:
                self.trades = {}

    def _save(self):
        try:
            with open(CFG.TRADES_FILE, "w", encoding="utf-8") as f:
                json.dump(self.trades, f)
        except Exception as e:
            logger.error(f"ذخیره معاملات: {e}")

    def snapshot(self) -> Dict[str, dict]:
        with self.lock:
            return {k: dict(v) for k, v in self.trades.items()}

    def open(self, tr: dict):
        with self.lock:
            self.trades[tr["symbol"]] = tr     # حداکثر یک معامله‌ی باز برای هر نماد
            self._save()

    def _report(self, tr: dict, ev: dict):
        emoji = "✅" if ev["pnl"] > 0 else "❌"
        self.tg.personal(
            f"{emoji} <b>گزارش معامله</b>\n📌 {tr['symbol']} ({SETUP_LABEL.get(tr['setup'], tr['setup'])})\n"
            f"📎 علت: {html.escape(ev['reason'])}\n"
            f"📈 این مرحله (بعد از کارمزد/اسلیپیج): {ev['pnl']:+.2f} USDT ({ev['r']:+.2f}R)\n"
            f"📦 درصد بسته‌شده: {ev['pct']:.0f}%" + (f"\n🧾 جمع کل معامله: {tr['pnl']:+.2f} USDT" if ev["final"] else ""))

    def monitor_once(self):
        with self.lock:
            items = list(self.trades.items())
        for sym, tr in items:
            try:
                while True:
                    df = klines(sym, "1m", 1000, start=tr["last_ts"] + 1)
                    df = closed_only(df)
                    df = df[df["open_time"] > tr["open_ts"]]      # فقط کندل‌های بعد از لحظه‌ی ورود
                    if df.empty:
                        break
                    finished = False
                    for row in df.itertuples():
                        with self.lock:
                            evs = advance(tr, int(row.open_time), row.open, row.high, row.low, row.close, 60_000)
                        for ev in evs:
                            self._report(tr, ev)
                            if ev["final"]:
                                finished = True
                        if finished:
                            break
                    if finished:
                        rec = {"symbol": sym, "setup": tr["setup"], "regime": tr["regime"], "open_ts": tr["open_ts"],
                               "close_ts": int(time.time() * 1000), "pnl": round(tr["pnl"], 2),
                               "r": round(tr["pnl"] / tr["risk_usdt"], 3), "reason": evs[-1]["reason"]}
                        self.journal.add(rec)
                        with self.lock:
                            self.trades.pop(sym, None)
                            self._save()
                        self.on_close(rec)
                        break
                    with self.lock:
                        self._save()
                    if len(df) < 900:
                        break
            except Exception as e:
                logger.error(f"مانیتور {sym}: {e}")


# ============================== سیستم اصلی ==============================
class Bot:
    def __init__(self):
        CFG.validate()
        self.tg = Telegram()
        self.journal = Journal(CFG.JOURNAL_FILE)
        self.state = self._load_state()
        self.engine = RegimeEngine(self.state.get("regime"))
        self.ai = AIAnalyst()
        self.paper = PaperTrader(self.tg, self.journal, self._on_trade_closed)
        self.cooldown: Dict[str, float] = self.state.get("cooldown", {})
        self.last_scan_key: int = self.state.get("last_scan_key", 0)
        self.last_summary_day: str = self.state.get("last_summary_day", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        self.running = True

    def _load_state(self) -> dict:
        if os.path.exists(CFG.STATE_FILE):
            try:
                with open(CFG.STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_state(self):
        self.state = {"regime": self.engine.dump(), "cooldown": self.cooldown,
                      "last_scan_key": self.last_scan_key, "last_summary_day": self.last_summary_day}
        try:
            with open(CFG.STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
        except Exception as e:
            logger.error(f"ذخیره state: {e}")

    def _on_trade_closed(self, rec: dict):
        hours = CFG.LOSS_COOLDOWN_H if rec["r"] <= 0 else CFG.REENTRY_COOLDOWN_H
        self.cooldown[rec["symbol"]] = time.time() + hours * 3600
        self._save_state()

    def _notify_regime(self, changed: bool, feats: dict, stats: dict):
        if not changed:
            return
        self.tg.personal(
            f"🧭 <b>رژیم بازار: {REGIME_LABEL[self.engine.current]}</b>\n"
            f"BTC روند ۴h: {feats['btc_t4']} | ADX: {feats['btc_adx4']} | پهنای بازار: {feats['breadth'] * 100:.0f}% نمادها صعودی\n"
            f"عملکرد {CFG.PERF_WINDOW_DAYS} روز اخیر: {stats['n']} معامله، میانگین R: {stats['avg_r']}\n"
            + (f"🧠 AI: {html.escape(self.engine.ai_note)}" if self.engine.ai_note and time.time() * 1000 < self.engine.bias_until else ""))

    def scan(self):
        now_ms = time.time() * 1000
        prep: Dict[str, pd.DataFrame] = {}
        for s in CFG.SYMBOLS:
            try:
                df1 = closed_only(klines(s, "1h", 300))
                df4 = closed_only(klines(s, "4h", 300))
                if len(df1) >= 120 and len(df4) >= 210:
                    prep[s] = prepare(df1, df4)
            except Exception as e:
                logger.warning(f"{s}: دریافت داده ناموفق ({e})")
            time.sleep(0.25)
        if "BTC/USDT" not in prep:
            logger.error("داده‌ی BTC در دسترس نبود؛ این چرخه رد شد.")
            return False

        rows = {s: d.iloc[-1] for s, d in prep.items()}
        feats = regime_features(rows)
        regime, changed = self.engine.update(feats, self.journal.records, now_ms)

        # بررسی کم‌تکرار AI: هر چند ساعت یک‌بار، یا وقتی رژیم عددی عوض شده باشد
        det_idx = self.engine.last_raw
        due = now_ms - self.engine.last_ai_ms >= CFG.AI_REGIME_INTERVAL_H * 3_600_000
        if CFG.AI_ENABLED and CFG.GROQ_API_KEY and (due or changed):
            stats = RegimeEngine.perf_stats(self.journal.records, now_ms)
            advice = self.ai.advise(feats, RegimeEngine.LEVELS[det_idx], stats)
            if advice:
                self.engine.set_bias(advice["bias"], now_ms, advice["reason"])
                logger.info(f"AI رژیم: {advice}")
                regime, ch2 = self.engine.update(feats, self.journal.records, now_ms)
                changed = changed or ch2
            else:
                self.engine.last_ai_ms = now_ms - (CFG.AI_REGIME_INTERVAL_H - 1) * 3_600_000   # ۱ ساعت بعد دوباره
        stats = RegimeEngine.perf_stats(self.journal.records, now_ms)
        self._notify_regime(changed, feats, stats)
        logger.info(f"رژیم: {regime} | {feats}")

        prof = PROFILES[regime]
        open_trades = self.paper.snapshot()
        if prof["max_open"] == 0 or len(open_trades) >= prof["max_open"]:
            self._save_state()
            return True

        cands = []
        for s, d in prep.items():
            i = len(d) - 1
            if now_ms - d["close_time"].iloc[i] > 2 * 3_600_000:
                continue    # داده‌ی کهنه
            if s in open_trades or self.cooldown.get(s, 0) > time.time():
                continue
            plan = find_setup(d, i, prof["setups"])
            if plan:
                mom = float(d["close"].iloc[i] / d["close"].iloc[i - 24] - 1)
                cands.append((mom, s, plan, d["close_time"].iloc[i]))
        cands.sort(reverse=True)    # قوی‌ترین مومنتوم ۲۴ ساعته اول

        for mom, s, plan, _ in cands:
            open_trades = self.paper.snapshot()
            if len(open_trades) >= prof["max_open"]:
                break
            grp = CFG.GROUPS.get(s, "other")
            if sum(1 for t in open_trades.values() if CFG.GROUPS.get(t["symbol"], "other") == grp) >= CFG.MAX_PER_GROUP:
                logger.info(f"{s}: سقف گروه {grp} پر است")
                continue
            try:
                price = last_price(s)
            except Exception as e:
                logger.warning(f"{s}: قیمت لحظه‌ای در دسترس نبود ({e})")
                continue
            entry = price * (1 + CFG.SLIPPAGE_PCT / 100)
            tr = build_trade(s, plan, entry, int(time.time() * 1000), regime, prof["risk_mult"])
            if not tr:
                logger.info(f"{s}: سیگنال {plan['setup']} به‌خاطر فاصله‌ی قیمت/استاپ اجرا نشد")
                continue
            self.paper.open(tr)
            risk_pct = (tr["entry"] - tr["sl"]) / tr["entry"] * 100
            self.tg.signal(
                f"🟢 <b>سیگنال خرید (Long): {s}</b>\n"
                f"🧩 الگو: {SETUP_LABEL[plan['setup']]}\n🧭 رژیم: {REGIME_LABEL[regime]}\n\n"
                f"💵 ورود: {fmt(tr['entry'])}\n🛑 حد ضرر: {fmt(tr['sl'])} ({risk_pct:.2f}% فاصله)\n"
                f"🎯 TP1 ({int(CFG.TP1_FRACTION * 100)}% حجم، {CFG.TP1_R}R): {fmt(tr['tp1'])}\n"
                f"↪️ بعد از TP1: حد ضرر به سر‌به‌سر + تریلینگ {CFG.TRAIL_ATR} ATR برای بقیه\n"
                f"⏳ اگر تا {CFG.TIME_STOP_H} ساعت پیشروی نکند، خروج زمانی\n\n"
                f"💰 حجم: {tr['qty']:.6g} | ارزش: {tr['qty'] * tr['entry']:,.0f} USDT\n"
                f"⚠️ ریسک: {tr['risk_usdt']:.2f} USDT ({tr['risk_usdt'] / CFG.CAPITAL * 100:.2f}% سرمایه)\n"
                f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}")
            logger.info(f"سیگنال {s} {plan['setup']} ارسال شد")
        self._save_state()
        return True

    def _daily_summary(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.last_summary_day:
            msg = self.journal.summary(self.last_summary_day)
            if msg:
                self.tg.personal(msg + f"\n🧭 رژیم فعلی: {REGIME_LABEL.get(self.engine.current, '-')}")
            self.last_summary_day = today
            self._save_state()

    def _monitor_loop(self):
        while self.running:
            try:
                self.paper.monitor_once()
            except Exception as e:
                logger.error(f"مانیتور: {e}")
            time.sleep(CFG.MONITOR_SECONDS)

    def start(self):
        logger.info("Bot v2 شروع شد")
        self.tg.personal(
            "🛡 <b>نسخه ۲ فعال شد</b>\nاستراتژی: Long اسپات، ورود ۱h با فیلتر روند ۴h، فقط روی کندل بسته‌شده\n"
            f"ریسک پایه هر معامله: {CFG.RISK_PCT}% | کارمزد هر طرف: {CFG.FEE_PCT}% | اسلیپیج: {CFG.SLIPPAGE_PCT}%\n"
            "رژیم بازار: 🟢 روندی / 🟡 رنج / 🔴 ریسک‌گریز (در حالت آخر ورود جدید انجام نمی‌شود)")
        threading.Thread(target=self._monitor_loop, daemon=True, name="Monitor").start()
        while self.running:
            try:
                now = time.time()
                key = int(now // 3600)
                if key != self.last_scan_key and now % 3600 >= 20:    # ۲۰ ثانیه بعد از بسته‌شدن کندل ساعتی
                    if self.scan():
                        self.last_scan_key = key
                        self._save_state()
                self._daily_summary()
            except Exception as e:
                logger.error(f"خطای چرخه‌ی اصلی: {e}")
                self.tg.personal(f"🚨 خطا در چرخه‌ی اصلی: {html.escape(str(e))}")
                time.sleep(60)
            gc.collect()
            time.sleep(30)


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"bot alive")

    do_HEAD = do_GET

    def log_message(self, *a):
        return


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        HTTPServer(("0.0.0.0", port), _Health).serve_forever()
    except Exception as e:
        logger.error(f"وب‌سرور: {e}")


# ============================== بک‌تستر ==============================
def run_backtest(days: int = 365, symbols: Optional[List[str]] = None):
    """
    همان find_setup / build_trade / advance / RegimeEngine لایو را روی تاریخچه‌ی واقعی اجرا می‌کند
    (با کارمزد و اسلیپیج). سیگنال در close کندل، ورود در open کندل بعد.
    AI در بک‌تست غیرفعاله؛ فقط رژیم عددی.
    اجرا:  python _bot_.py backtest 365
    """
    symbols = symbols or CFG.SYMBOLS
    print(f"دانلود تاریخچه {days} روز برای {len(symbols)} نماد ...")
    data: Dict[str, pd.DataFrame] = {}
    for s in symbols:
        try:
            df1 = history(s, "1h", days * 24 + 300)
            df4 = history(s, "4h", days * 6 + 300)
            data[s] = prepare(df1, df4)
        except Exception as e:
            print(f"  {s}: رد شد ({e})")
    if "BTC/USDT" not in data:
        print("BTC لازمه.")
        return
    idx = {s: {int(t): k for k, t in enumerate(d["open_time"])} for s, d in data.items()}
    timeline = [int(t) for t in data["BTC/USDT"]["open_time"].iloc[260:]]

    engine = RegimeEngine()
    records: List[dict] = []
    open_tr: Dict[str, dict] = {}
    cooldown: Dict[str, int] = {}
    pending: List[tuple] = []
    HOUR = 3_600_000

    for t in timeline:
        # 1) ورود معامله‌های سیگنال‌خورده در کندل قبل: قیمت open همین کندل
        for s, plan, regime, risk_mult in pending:
            if s in open_tr or s not in idx or t not in idx[s]:
                continue
            row = data[s].iloc[idx[s][t]]
            entry = float(row["open"]) * (1 + CFG.SLIPPAGE_PCT / 100)
            tr = build_trade(s, plan, entry, t, regime, risk_mult)
            if tr:
                open_tr[s] = tr
        pending = []

        # 2) مدیریت معامله‌های باز با کندل جاری
        for s in list(open_tr):
            if t not in idx.get(s, {}):
                continue
            row = data[s].iloc[idx[s][t]]
            tr = open_tr[s]
            evs = advance(tr, t, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), HOUR)
            if any(e["final"] for e in evs):
                close_ts = t + HOUR
                records.append({"symbol": s, "setup": tr["setup"], "regime": tr["regime"], "open_ts": tr["open_ts"],
                                "close_ts": close_ts, "pnl": tr["pnl"], "r": tr["pnl"] / tr["risk_usdt"]})
                cooldown[s] = close_ts + (CFG.LOSS_COOLDOWN_H if records[-1]["r"] <= 0 else CFG.REENTRY_COOLDOWN_H) * HOUR
                del open_tr[s]

        # 3) رژیم در close این کندل
        now_close = t + HOUR
        rows = {s: data[s].iloc[idx[s][t]] for s in data if t in idx[s]}
        feats = regime_features(rows)
        if not feats:
            continue
        regime, _ = engine.update(feats, records, now_close)
        prof = PROFILES[regime]
        if prof["max_open"] == 0 or len(open_tr) >= prof["max_open"]:
            continue

        cands = []
        for s in rows:
            if s in open_tr or cooldown.get(s, 0) > now_close:
                continue
            i = idx[s][t]
            plan = find_setup(data[s], i, prof["setups"])
            if plan:
                mom = float(data[s]["close"].iloc[i] / data[s]["close"].iloc[i - 24] - 1)
                cands.append((mom, s, plan))
        cands.sort(reverse=True)
        slots = prof["max_open"] - len(open_tr)
        grp_count: Dict[str, int] = {}
        for tr in open_tr.values():
            g = CFG.GROUPS.get(tr["symbol"], "o")
            grp_count[g] = grp_count.get(g, 0) + 1
        for mom, s, plan in cands:
            if slots <= 0:
                break
            g = CFG.GROUPS.get(s, "o")
            if grp_count.get(g, 0) >= CFG.MAX_PER_GROUP:
                continue
            pending.append((s, plan, regime, prof["risk_mult"]))
            grp_count[g] = grp_count.get(g, 0) + 1
            slots -= 1

    report_backtest(records, days)
    return records


def report_backtest(records: List[dict], days: int):
    if not records:
        print("هیچ معامله‌ای ثبت نشد.")
        return
    r = np.array([x["r"] for x in records])
    wins, losses = r[r > 0], r[r <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    cum = np.cumsum(r)
    dd = float((np.maximum.accumulate(cum) - cum).max())
    print("\n=========== نتیجه‌ی بک‌تست (با کارمزد و اسلیپیج) ===========")
    print(f"تعداد معاملات: {len(r)}  ({len(r) / days * 30:.1f} در ماه)")
    print(f"نرخ برد: {len(wins) / len(r) * 100:.1f}%  | میانگین برد: {wins.mean() if len(wins) else 0:+.2f}R  | میانگین باخت: {losses.mean() if len(losses) else 0:+.2f}R")
    print(f"امید ریاضی هر معامله: {r.mean():+.3f}R  | Profit Factor: {pf:.2f}")
    print(f"جمع R: {r.sum():+.1f}R  | بیشینه افت (R): {dd:.1f}R")
    half = len(r) // 2
    print(f"ثبات: نیمه‌ی اول {r[:half].mean():+.3f}R | نیمه‌ی دوم {r[half:].mean():+.3f}R")
    by_month: Dict[str, float] = {}
    for x in records:
        k = datetime.fromtimestamp(x["close_ts"] / 1000, timezone.utc).strftime("%Y-%m")
        by_month[k] = by_month.get(k, 0) + x["r"]
    good = sum(1 for v in by_month.values() if v > 0)
    print(f"ماه‌های سودده: {good} از {len(by_month)}")
    for k in sorted(by_month):
        print(f"  {k}: {by_month[k]:+.1f}R")
    for key in ("setup", "regime"):
        print(f"\nبه تفکیک {key}:")
        for v in sorted({x[key] for x in records}):
            sub = np.array([x["r"] for x in records if x[key] == v])
            print(f"  {v}: {len(sub)} معامله | میانگین {sub.mean():+.3f}R | برد {np.mean(sub > 0) * 100:.0f}%")
    print("\nنکته: اگر امید ریاضی منفی یا ماه‌های سودده کمتر از نیمه بود، قبل از پول واقعی/ادامه‌ی ربات، استراتژی را اصلاح کنید.")


# ============================== ورود ==============================
if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "backtest":
        run_backtest(int(sys.argv[2]) if len(sys.argv) > 2 else 365)
    else:
        threading.Thread(target=start_health_server, daemon=True).start()
        bot = Bot()
        try:
            bot.start()
        except KeyboardInterrupt:
            bot.running = False
