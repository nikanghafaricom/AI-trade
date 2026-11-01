#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hybrid Signal Bot - نسخه معاملات مجازی (Paper Trading)
=====================================================
نسخه بهینه‌شده بر اساس بک‌تست ۶ ماهه (مه تا اکتبر ۲۰۲۶)

ویژگی‌ها:
- فقط معاملات مجازی (هیچ پولی واقعی جابه‌جا نمی‌شود)
- نمادهای انتخاب‌شده: SOL, NEAR, AVAX, XRP
- مدیریت ریسک درصدی + محدودیت تعداد معاملات همزمان
- ورود بر اساس امتیازدهی چندعاملی + پولبک + تأیید چندتایم‌فریم
- خروج پلکانی (TP1 / TP2 / TP3) + تریلینگ استاپ
- کاهش خودکار حجم بعد از ضررهای متوالی
- ذخیره تاریخچه معاملات و وضعیت پوزیشن‌ها در فایل JSON

نحوه اجرا:
    python paper_trading_bot.py

برای توقف: Ctrl+C
"""

import os
import time
import json
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from pathlib import Path

# ==================== تنظیمات اصلی ====================
class Config:
    # نمادهایی که در بک‌تست بهترین عملکرد را داشتند
    SYMBOLS = ["SOL/USDT", "NEAR/USDT", "AVAX/USDT", "XRP/USDT"]

    SYMBOL_GROUPS = {
        "SOL/USDT": "L1_alt",
        "NEAR/USDT": "L1_alt",
        "AVAX/USDT": "L1_alt",
        "XRP/USDT": "payments",
    }

    # تایم‌فریم‌ها
    ENTRY_TIMEFRAME = "15m"
    CONFIRM_TIMEFRAME = "1h"
    TREND_TIMEFRAME = "4h"

    # سرمایه مجازی و ریسک
    VIRTUAL_CAPITAL = 10000.0          # سرمایه اولیه مجازی (USDT)
    RISK_PER_TRADE_PCT = 3.2           # درصد ریسک هر معامله
    MAX_CONCURRENT_TRADES = 3
    MAX_TRADES_PER_GROUP = 2

    # فیلتر سیگنال
    MIN_SIGNAL_SCORE = 9.8
    ATR_PERCENTILE_MAX = 91
    COOLDOWN_MINUTES = 60

    # پارامترهای پیش‌فرض معاملات
    DEFAULT_PARAMS = {
        "rsi_buy_min": 40,
        "rsi_buy_max_range_start": 46,
        "rsi_buy_max_range_end": 68,
        "volume_mult": 1.05,
        "atr_min_filter": 0.0016,
        "sl_atr_mult": 1.55,
        "tp1_mult": 1.8,
        "tp2_mult": 3.0,
        "tp3_mult": 5.0,
        "trailing_mult": 0.75,
    }

    SYMBOL_OVERRIDES = {
        "SOL/USDT":  {"atr_min_filter": 0.0015, "sl_atr_mult": 1.5, "rsi_buy_max_range_end": 70},
        "AVAX/USDT": {"atr_min_filter": 0.0015, "sl_atr_mult": 1.5},
        "NEAR/USDT": {"atr_min_filter": 0.0015, "sl_atr_mult": 1.5},
        "XRP/USDT":  {"atr_min_filter": 0.0017, "sl_atr_mult": 1.55},
    }

    # کارمزد و اسپرد تقریبی (برای محاسبه واقعی‌تر سود/زیان)
    TAKER_FEE_PCT = 0.05
    SPREAD_PCT = 0.05

    # فاصله بررسی (ثانیه)
    CHECK_INTERVAL = 300          # هر ۵ دقیقه سیگنال جدید
    MONITOR_INTERVAL = 30         # هر ۳۰ ثانیه پوزیشن‌های باز چک شوند

    # فایل‌های ذخیره
    TRADES_FILE = "paper_trades.json"
    HISTORY_FILE = "trade_history.json"
    LOG_FILE = "paper_bot.log"


# ==================== لاگ ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(Config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("PaperBot")


# ==================== لایه داده (بایننس) ====================
class DataLayer:
    HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]

    def _get(self, path: str, params: dict):
        last_err = None
        for host in self.HOSTS:
            try:
                r = requests.get(f"{host}{path}", params=params, timeout=12)
                if r.status_code == 200:
                    return r.json()
                last_err = f"{host} → {r.status_code}"
            except Exception as e:
                last_err = str(e)
        raise RuntimeError(last_err)

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        try:
            raw = self._get("/api/v3/klines", {
                "symbol": symbol.replace("/", ""),
                "interval": timeframe,
                "limit": limit
            })
            rows = [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in raw]
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            return df
        except Exception as e:
            logger.error(f"خطا در دریافت داده {symbol} {timeframe}: {e}")
            return pd.DataFrame()


# ==================== اندیکاتورها و تحلیل ====================
class Analysis:
    @staticmethod
    def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < 50:
            return df
        df = df.copy()

        # RSI
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / (loss + 1e-12)
        df["rsi"] = 100 - (100 / (1 + rs))

        # EMA
        df["ema_fast"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=50, adjust=False).mean()
        df["ema_trend"] = df["close"].ewm(span=200, adjust=False).mean()

        # MACD
        ema12 = df["close"].ewm(span=12, adjust=False).mean()
        ema26 = df["close"].ewm(span=26, adjust=False).mean()
        df["macd"] = ema12 - ema26
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]

        # ATR
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df["true_range"] = tr
        df["atr"] = tr.rolling(14).mean()

        # حجم و سطوح
        df["vol_sma"] = df["volume"].rolling(20).mean()
        df["support"] = df["low"].rolling(15).min()
        df["resistance"] = df["high"].rolling(15).max()

        # ADX
        plus_dm = df["high"].diff()
        minus_dm = -df["low"].diff()
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
        atr14 = df["true_range"].rolling(14).mean()
        plus_di = 100 * (plus_dm.rolling(14).mean() / (atr14 + 1e-12))
        minus_di = 100 * (minus_dm.rolling(14).mean() / (atr14 + 1e-12))
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)) * 100
        df["adx"] = dx.rolling(14).mean()

        return df

    @staticmethod
    def atr_percentile(df: pd.DataFrame, window: int = 100) -> float:
        if df.empty or "true_range" not in df or len(df) < 30:
            return 50.0
        recent = df["true_range"].tail(window).dropna()
        if recent.empty:
            return 50.0
        return float((recent < df["true_range"].iloc[-1]).mean() * 100)

    @staticmethod
    def get_major_trend(df_4h: pd.DataFrame) -> str:
        if df_4h.empty or len(df_4h) < 100:
            return "NEUTRAL"
        latest = df_4h.iloc[-1]
        if latest["close"] > latest["ema_trend"] and latest["ema_fast"] > latest["ema_slow"]:
            return "BULLISH"
        if latest["close"] < latest["ema_trend"] and latest["ema_fast"] < latest["ema_slow"]:
            return "BEARISH"
        return "NEUTRAL"

    @staticmethod
    def is_mtf_aligned(df_1h: pd.DataFrame) -> bool:
        if df_1h.empty or len(df_1h) < 60:
            return False
        latest = df_1h.iloc[-1]
        if pd.isna(latest.get("ema_fast")) or pd.isna(latest.get("ema_slow")):
            return False
        return bool(latest["ema_fast"] > latest["ema_slow"])

    @staticmethod
    def market_structure(df: pd.DataFrame, lookback: int = 40) -> str:
        if df.empty or len(df) < lookback + 4:
            return "NEUTRAL"
        window = df.tail(lookback).reset_index(drop=True)
        swing_highs, swing_lows = [], []
        for i in range(2, len(window) - 2):
            h, l = window["high"], window["low"]
            if h[i] > h[i-1] and h[i] > h[i-2] and h[i] > h[i+1] and h[i] > h[i+2]:
                swing_highs.append(h[i])
            if l[i] < l[i-1] and l[i] < l[i-2] and l[i] < l[i+1] and l[i] < l[i+2]:
                swing_lows.append(l[i])
        if len(swing_highs) >= 2 and len(swing_lows) >= 2:
            if swing_highs[-1] > swing_highs[-2] and swing_lows[-1] > swing_lows[-2]:
                return "BULLISH"
            if swing_highs[-1] < swing_highs[-2] and swing_lows[-1] < swing_lows[-2]:
                return "BEARISH"
        return "NEUTRAL"

    @staticmethod
    def is_pullback_buy(latest, prev) -> bool:
        close = float(latest["close"])
        ema20 = float(latest["ema_fast"]) if not pd.isna(latest.get("ema_fast")) else None
        support = float(latest["support"]) if not pd.isna(latest.get("support")) else None

        near_ema = False
        if ema20 and ema20 > 0:
            dist = abs(close - ema20) / close * 100
            near_ema = dist < 1.2 and close >= ema20 * 0.992

        near_support = False
        if support and support > 0:
            dist_s = (close - support) / close * 100
            near_support = 0 <= dist_s < 1.8

        body = close - float(latest["open"])
        prev_body = float(prev["close"]) - float(prev["open"])
        bullish = body > 0 and (body > abs(prev_body) * 0.55 or body / close > 0.0025)

        return (near_ema or near_support) and bullish


# ==================== موتور سیگنال ====================
class SignalEngine:
    def __init__(self):
        self.params = {}
        for sym in Config.SYMBOLS:
            p = dict(Config.DEFAULT_PARAMS)
            p.update(Config.SYMBOL_OVERRIDES.get(sym, {}))
            self.params[sym] = p

    def _score_buy(self, latest, prev, p: dict) -> float:
        score = 0.0
        if latest["ema_fast"] > latest["ema_slow"]:
            score += 1.5

        rsi = float(latest["rsi"])
        if rsi > p["rsi_buy_min"] and float(prev["rsi"]) <= p["rsi_buy_min"]:
            score += 1.8
        elif p["rsi_buy_max_range_start"] <= rsi <= p["rsi_buy_max_range_end"] and rsi > float(prev["rsi"]):
            score += 1.2

        if not pd.isna(latest.get("macd_hist", np.nan)):
            if latest["macd_hist"] > 0 and latest["macd_hist"] > prev.get("macd_hist", 0):
                score += 1.2
            elif latest["macd_hist"] > 0:
                score += 0.6

        vol_ratio = latest["volume"] / latest["vol_sma"] if latest["vol_sma"] else 0
        if vol_ratio >= p["volume_mult"] * 1.15:
            score += 1.8
        elif vol_ratio >= p["volume_mult"]:
            score += 1.2
        elif vol_ratio >= p["volume_mult"] * 0.85:
            score += 0.5

        if Analysis.is_pullback_buy(latest, prev):
            score += 2.2

        adx = latest.get("adx")
        if not pd.isna(adx):
            if adx >= 25:
                score += 1.2
            elif adx >= 18:
                score += 0.7

        if latest["support"] > 0 and (latest["close"] - latest["support"]) / latest["close"] < 0.015:
            score += 0.6

        return score

    def get_signal(self, symbol: str, df_15m: pd.DataFrame, df_1h: pd.DataFrame,
                   df_4h: pd.DataFrame, btc_trend: str, btc_structure: str) -> Tuple[Optional[str], dict]:
        if df_15m.empty or len(df_15m) < 60:
            return None, {}

        p = self.params[symbol]
        latest = df_15m.iloc[-1]
        prev = df_15m.iloc[-2]

        if pd.isna(latest["rsi"]) or pd.isna(latest["ema_fast"]) or pd.isna(latest["atr"]):
            return None, {}
        if latest["atr"] < latest["close"] * p["atr_min_filter"]:
            return None, {}

        pctl = Analysis.atr_percentile(df_15m)
        if pctl > Config.ATR_PERCENTILE_MAX:
            return None, {}
        if latest["vol_sma"] and latest["volume"] / latest["vol_sma"] < 0.6:
            return None, {}

        # فیلتر رژیم بیت‌کوین
        if btc_trend == "BEARISH":
            return None, {}

        trend_4h = Analysis.get_major_trend(df_4h)
        structure = Analysis.market_structure(df_15m)

        if trend_4h != "BULLISH":
            return None, {}

        score = self._score_buy(latest, prev, p) + 1.8

        if structure == "BULLISH":
            score += 2.2
        elif structure == "NEUTRAL":
            score -= 1.0
        else:
            return None, {}

        if not Analysis.is_mtf_aligned(df_1h):
            return None, {}
        score += 1.8

        if btc_trend == "BULLISH" and btc_structure == "BULLISH":
            score += 1.5
        elif btc_structure == "BEARISH":
            score -= 1.5

        if not Analysis.is_pullback_buy(latest, prev):
            return None, {}

        if score < Config.MIN_SIGNAL_SCORE:
            return None, {}

        diagnostics = {
            "score": round(score, 2),
            "trend_4h": trend_4h,
            "structure": structure,
            "rsi": round(float(latest["rsi"]), 1),
            "adx": round(float(latest["adx"]), 1) if not pd.isna(latest.get("adx")) else None,
            "atr_pctl": round(pctl, 1),
            "btc_trend": btc_trend,
        }
        return "BUY", diagnostics


# ==================== مدیریت ریسک ====================
class RiskManager:
    def __init__(self, capital: float):
        self.capital = capital
        self.consecutive_losses = 0

    def update_capital(self, pnl: float):
        self.capital += pnl
        if pnl <= 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def can_open(self, symbol: str, active: Dict) -> Tuple[bool, str]:
        if len(active) >= Config.MAX_CONCURRENT_TRADES:
            return False, "سقف تعداد معاملات همزمان"
        group = Config.SYMBOL_GROUPS.get(symbol, "other")
        cnt = sum(1 for t in active.values() if Config.SYMBOL_GROUPS.get(t["symbol"], "other") == group)
        if cnt >= Config.MAX_TRADES_PER_GROUP:
            return False, f"سقف گروه {group}"
        return True, ""

    def calc_qty(self, entry: float, stop: float) -> float:
        size_mult = 1.0
        if self.consecutive_losses >= 3:
            size_mult = 0.45
        elif self.consecutive_losses >= 2:
            size_mult = 0.65

        risk_amount = self.capital * (Config.RISK_PER_TRADE_PCT / 100) * size_mult
        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0:
            return 0.0
        qty = risk_amount / risk_per_unit
        # سقف اکسپوژر ۳۰٪ سرمایه
        if qty * entry > self.capital * 0.30:
            qty = (self.capital * 0.30) / entry
        return qty


# ==================== معامله‌گر مجازی ====================
class PaperTrader:
    def __init__(self, risk_manager: RiskManager):
        self.risk = risk_manager
        self.active: Dict[str, dict] = self._load(Config.TRADES_FILE)
        self.history: List[dict] = self._load_history()
        self.last_signal_time: Dict[str, datetime] = {}

    def _load(self, path: str) -> dict:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _load_history(self) -> list:
        if os.path.exists(Config.HISTORY_FILE):
            try:
                with open(Config.HISTORY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save(self):
        with open(Config.TRADES_FILE, "w", encoding="utf-8") as f:
            json.dump(self.active, f, indent=2, ensure_ascii=False, default=str)
        with open(Config.HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(self.history, f, indent=2, ensure_ascii=False, default=str)

    def open_trade(self, symbol: str, entry: float, atr: float, support: float, diagnostics: dict):
        p = Config.DEFAULT_PARAMS.copy()
        p.update(Config.SYMBOL_OVERRIDES.get(symbol, {}))

        stop = min(support, entry - p["sl_atr_mult"] * atr)
        risk = entry - stop
        if risk <= 0:
            return False

        qty = self.risk.calc_qty(entry, stop)
        if qty <= 0:
            return False

        tp1 = entry + p["tp1_mult"] * risk
        tp2 = entry + p["tp2_mult"] * risk
        tp3 = entry + p["tp3_mult"] * risk

        trade_id = f"{symbol}_{int(time.time())}"
        self.active[trade_id] = {
            "symbol": symbol,
            "side": "BUY",
            "entry": round(entry, 6),
            "sl": round(stop, 6),
            "original_sl": round(stop, 6),
            "tp1": round(tp1, 6),
            "tp2": round(tp2, 6),
            "tp3": round(tp3, 6),
            "qty": qty,
            "atr": atr,
            "remaining_pct": 100,
            "tp1_hit": False,
            "tp2_hit": False,
            "highest": entry,
            "lowest": entry,
            "open_time": datetime.now(timezone.utc).isoformat(),
            "diagnostics": diagnostics,
        }
        self.last_signal_time[symbol] = datetime.now(timezone.utc)
        self._save()

        logger.info(
            f"✅ باز شدن معامله مجازی | {symbol} | Entry={entry:.4f} | SL={stop:.4f} | "
            f"TP1={tp1:.4f} | Qty={qty:.4f} | Score={diagnostics.get('score')}"
        )
        return True

    def _close_partial(self, trade_id: str, trade: dict, exit_price: float, reason: str, closed_pct: float):
        entry = trade["entry"]
        price_diff = exit_price - entry
        cost = (entry + exit_price) * ((Config.TAKER_FEE_PCT + Config.SPREAD_PCT / 2) / 100)
        price_diff -= cost

        risk = abs(entry - trade["original_sl"])
        r_mult = price_diff / risk if risk > 0 else 0.0
        qty_closed = trade["qty"] * (closed_pct / 100)
        pnl = price_diff * qty_closed

        self.risk.update_capital(pnl)

        record = {
            "symbol": trade["symbol"],
            "side": "BUY",
            "entry": entry,
            "exit": exit_price,
            "pnl": round(pnl, 2),
            "r_multiple": round(r_mult, 2),
            "closed_pct": closed_pct,
            "reason": reason,
            "open_time": trade["open_time"],
            "close_time": datetime.now(timezone.utc).isoformat(),
            "capital_after": round(self.risk.capital, 2),
        }
        self.history.append(record)
        trade["remaining_pct"] -= closed_pct

        emoji = "✅" if pnl > 0 else "❌"
        logger.info(
            f"{emoji} بسته شدن جزئی/کامل | {trade['symbol']} | {reason} | "
            f"PnL={pnl:+.2f} USDT ({r_mult:+.2f}R) | باقیمانده={trade['remaining_pct']}%"
        )
        self._save()

    def update_positions(self, data: DataLayer):
        to_delete = []
        for trade_id, trade in list(self.active.items()):
            try:
                df = data.fetch_ohlcv(trade["symbol"], "1m", limit=5)
                if df.empty:
                    continue
                high = float(df["high"].max())
                low = float(df["low"].min())
                close = float(df["close"].iloc[-1])

                trade["highest"] = max(trade["highest"], high)
                trade["lowest"] = min(trade["lowest"], low)

                # حد ضرر
                if low <= trade["sl"]:
                    self._close_partial(trade_id, trade, trade["sl"], "حد ضرر / تریلینگ", trade["remaining_pct"])
                    to_delete.append(trade_id)
                    continue

                # TP3
                if high >= trade["tp3"]:
                    self._close_partial(trade_id, trade, trade["tp3"], "TP3 (خروج کامل)", trade["remaining_pct"])
                    to_delete.append(trade_id)
                    continue

                # TP2
                if not trade["tp2_hit"] and high >= trade["tp2"]:
                    self._close_partial(trade_id, trade, trade["tp2"], "TP2 (۳۰٪)", 30)
                    trade["tp2_hit"] = True

                # TP1
                if not trade["tp1_hit"] and high >= trade["tp1"]:
                    self._close_partial(trade_id, trade, trade["tp1"], "TP1 (۵۰٪ + قفل سود)", 50)
                    trade["tp1_hit"] = True
                    risk = abs(trade["entry"] - trade["original_sl"])
                    trade["sl"] = trade["entry"] + 0.5 * risk   # قفل ۰.۵R سود

                # تریلینگ بعد از TP1
                if trade["tp1_hit"] and trade["remaining_pct"] > 0:
                    p = Config.DEFAULT_PARAMS.copy()
                    p.update(Config.SYMBOL_OVERRIDES.get(trade["symbol"], {}))
                    new_trail = trade["highest"] - p["trailing_mult"] * trade["atr"]
                    trade["sl"] = max(trade["sl"], new_trail)

                if trade["remaining_pct"] <= 0:
                    to_delete.append(trade_id)

            except Exception as e:
                logger.error(f"خطا در به‌روزرسانی {trade_id}: {e}")

        for tid in to_delete:
            if tid in self.active:
                del self.active[tid]
        if to_delete:
            self._save()


# ==================== سیستم اصلی ====================
class PaperTradingSystem:
    def __init__(self):
        self.data = DataLayer()
        self.signal_engine = SignalEngine()
        self.risk = RiskManager(Config.VIRTUAL_CAPITAL)
        self.trader = PaperTrader(self.risk)
        self.running = True

        # بازیابی سرمایه از تاریخچه (اگر قبلاً اجرا شده)
        if self.trader.history:
            last_cap = self.trader.history[-1].get("capital_after")
            if last_cap:
                self.risk.capital = float(last_cap)
                logger.info(f"سرمایه بازیابی‌شده از تاریخچه: {self.risk.capital:.2f} USDT")

    def _get_btc_macro(self) -> Tuple[str, str]:
        try:
            df4 = self.data.fetch_ohlcv("BTC/USDT", Config.TREND_TIMEFRAME, limit=300)
            df4 = Analysis.calculate_indicators(df4)
            trend = Analysis.get_major_trend(df4)

            df15 = self.data.fetch_ohlcv("BTC/USDT", Config.ENTRY_TIMEFRAME, limit=100)
            df15 = Analysis.calculate_indicators(df15)
            structure = Analysis.market_structure(df15)
            return trend, structure
        except Exception as e:
            logger.warning(f"خطا در دریافت وضعیت BTC: {e}")
            return "NEUTRAL", "NEUTRAL"

    def process_symbol(self, symbol: str, btc_trend: str, btc_struct: str):
        # کول‌داون
        last = self.trader.last_signal_time.get(symbol)
        if last and (datetime.now(timezone.utc) - last).total_seconds() < Config.COOLDOWN_MINUTES * 60:
            return

        # آیا قبلاً پوزیشن باز دارد؟
        if any(t["symbol"] == symbol for t in self.trader.active.values()):
            return

        can, reason = self.risk.can_open(symbol, self.trader.active)
        if not can:
            return

        df15 = self.data.fetch_ohlcv(symbol, Config.ENTRY_TIMEFRAME, limit=150)
        df15 = Analysis.calculate_indicators(df15)
        if df15.empty:
            return

        df1h = self.data.fetch_ohlcv(symbol, Config.CONFIRM_TIMEFRAME, limit=100)
        df1h = Analysis.calculate_indicators(df1h)

        df4h = self.data.fetch_ohlcv(symbol, Config.TREND_TIMEFRAME, limit=300)
        df4h = Analysis.calculate_indicators(df4h)

        signal, diag = self.signal_engine.get_signal(symbol, df15, df1h, df4h, btc_trend, btc_struct)
        if signal != "BUY":
            return

        latest = df15.iloc[-1]
        self.trader.open_trade(
            symbol=symbol,
            entry=float(latest["close"]),
            atr=float(latest["atr"]),
            support=float(latest["support"]),
            diagnostics=diag
        )

    def run_once(self):
        logger.info("----- شروع چرخه بررسی -----")
        btc_trend, btc_struct = self._get_btc_macro()
        logger.info(f"وضعیت کلان BTC → روند: {btc_trend} | ساختار: {btc_struct}")

        for sym in Config.SYMBOLS:
            try:
                self.process_symbol(sym, btc_trend, btc_struct)
            except Exception as e:
                logger.error(f"خطا در پردازش {sym}: {e}")
            time.sleep(1.0)

        # خلاصه وضعیت
        open_count = len(self.trader.active)
        logger.info(
            f"وضعیت فعلی | سرمایه مجازی: {self.risk.capital:.2f} USDT | "
            f"پوزیشن باز: {open_count} | ضرر متوالی: {self.risk.consecutive_losses}"
        )

    def start(self):
        logger.info("=" * 55)
        logger.info("ربات معاملات مجازی (Paper Trading) راه‌اندازی شد")
        logger.info(f"سرمایه اولیه: {Config.VIRTUAL_CAPITAL} USDT")
        logger.info(f"نمادها: {', '.join(Config.SYMBOLS)}")
        logger.info(f"ریسک هر معامله: {Config.RISK_PER_TRADE_PCT}%")
        logger.info("=" * 55)

        # ترد مانیتورینگ پوزیشن‌ها
        import threading
        def monitor_loop():
            while self.running:
                try:
                    self.trader.update_positions(self.data)
                except Exception as e:
                    logger.error(f"خطا در مانیتورینگ: {e}")
                time.sleep(Config.MONITOR_INTERVAL)

        threading.Thread(target=monitor_loop, daemon=True, name="Monitor").start()

        while self.running:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"خطای چرخه اصلی: {e}")
            time.sleep(Config.CHECK_INTERVAL)

    def stop(self):
        self.running = False
        logger.info("ربات متوقف شد.")


# ==================== اجرا ====================
if __name__ == "__main__":
    bot = PaperTradingSystem()
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.stop()
        print("\nربات با موفقیت متوقف شد.")
