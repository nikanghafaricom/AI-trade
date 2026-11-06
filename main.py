# ==============================================
# Hybrid Spot Bot - Best Config (Paper Trading)
# نسخه نهایی بهینه شده بر اساس بک‌تست یک‌ساله
# نتیجه بک‌تست: حدود +۱۸۶٪ | MaxDD ≈ ۲۳.۷٪ | WR ≈ ۶۵٪ | PF ≈ ۱.۲۸
# ==============================================
import os
import time
import logging
import requests
import gc
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, date as date_cls
from typing import Dict, Optional, List, Tuple
from collections import defaultdict
import pandas as pd
import numpy as np
from dotenv import load_dotenv

load_dotenv()

# ==================== وب‌سرور سلامت ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Hybrid Spot Bot BEST is alive!")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_check_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        server.serve_forever()
    except Exception as e:
        logger.error(f"خطا در اجرای وب‌سرور: {e}")


threading.Thread(target=start_health_check_server, daemon=True).start()

# ==================== تنظیمات بهترین نسخه ====================
class Config:
    EXCHANGE_ID = "binance"
    API_KEY = os.getenv("EXCHANGE_API_KEY", "")
    SECRET = os.getenv("EXCHANGE_SECRET", "")
    PASSWORD = os.getenv("EXCHANGE_PASSWORD", "")

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    PERSONAL_CHAT_ID = os.getenv("PERSONAL_CHAT_ID")

    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

    # نمادهای منتخب بهترین عملکرد
    SYMBOLS = [
        "NEAR/USDT", "PAXG/USDT", "LTC/USDT",
        "BNB/USDT", "XRP/USDT", "LINK/USDT",
    ]

    SYMBOL_GROUPS = {
        "NEAR/USDT": "L1",
        "PAXG/USDT": "gold",
        "LTC/USDT": "majors",
        "BNB/USDT": "majors",
        "XRP/USDT": "payments",
        "LINK/USDT": "oracle",
    }

    ENTRY_TIMEFRAME = "15m"
    CONFIRM_TIMEFRAME = "1h"
    TREND_TIMEFRAME = "4h"
    CHECK_INTERVAL = 180  # ۳ دقیقه

    # آستانه‌ها (نسخه تهاجمی بهینه)
    MIN_SIGNAL_SCORE = 6.4
    ATR_PERCENTILE_MAX = 93

    VIRTUAL_CAPITAL_USDT = float(os.getenv("VIRTUAL_CAPITAL_USDT", 10000))
    RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", 1.55))
    MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES", 5))
    MAX_TRADES_PER_GROUP = int(os.getenv("MAX_TRADES_PER_GROUP", 2))

    MIN_JUDGE_CONFIDENCE = int(os.getenv("MIN_JUDGE_CONFIDENCE", 50))

    EXCHANGE_TAKER_FEE_PCT = float(os.getenv("EXCHANGE_TAKER_FEE_PCT", 0.04))
    DEFAULT_SPREAD_PCT_FALLBACK = 0.03

    TRADE_MONITOR_INTERVAL_SECONDS = int(os.getenv("TRADE_MONITOR_INTERVAL_SECONDS", 20))

    CORRELATION_LOOKBACK_CANDLES = 100
    CORRELATION_REFRESH_HOURS = 2
    CORRELATION_THRESHOLD_MIN = 0.55
    CORRELATION_THRESHOLD_MAX = 0.78
    MAX_CORRELATED_TRADES = int(os.getenv("MAX_CORRELATED_TRADES", 3))

    TREND_WARMUP_CANDLES = 300

    PORTFOLIO_ROLLING_WINDOW = int(os.getenv("PORTFOLIO_ROLLING_WINDOW", 12))
    PORTFOLIO_MIN_SAMPLES = int(os.getenv("PORTFOLIO_MIN_SAMPLES", 5))
    PORTFOLIO_CONSECUTIVE_LOSS_THRESHOLD = int(os.getenv("PORTFOLIO_CONSECUTIVE_LOSS_THRESHOLD", 3))

    MARKET_REGIME_AI_INTERVAL_HOURS = int(os.getenv("MARKET_REGIME_AI_INTERVAL_HOURS", 4))

    GROQ_DAILY_TOKEN_CAP = int(os.getenv("GROQ_DAILY_TOKEN_CAP", 250000))
    GROQ_DAILY_REQUEST_CAP = int(os.getenv("GROQ_DAILY_REQUEST_CAP", 1200))
    GROQ_JUDGE_RESERVED_SHARE = float(os.getenv("GROQ_JUDGE_RESERVED_SHARE", 0.65))
    GROQ_REGIME_RESERVED_SHARE = float(os.getenv("GROQ_REGIME_RESERVED_SHARE", 0.04))
    GROQ_BUDGET_HARD_STOP_FRACTION = float(os.getenv("GROQ_BUDGET_HARD_STOP_FRACTION", 0.95))
    GROQ_OPTIMIZER_MIN_INTERVAL_HOURS = float(os.getenv("GROQ_OPTIMIZER_MIN_INTERVAL_HOURS", 1.5))
    GROQ_OPTIMIZER_MAX_INTERVAL_HOURS = float(os.getenv("GROQ_OPTIMIZER_MAX_INTERVAL_HOURS", 5.0))

    # Partial close (بهینه بک‌تست)
    TP1_CLOSE_PCT = 32
    TP2_CLOSE_PCT = 28

    def validate(self):
        required = {
            "TELEGRAM_BOT_TOKEN": self.TELEGRAM_BOT_TOKEN,
            "TELEGRAM_CHAT_ID": self.TELEGRAM_CHAT_ID,
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ValueError(f"این متغیرهای محیطی تنظیم نشدن: {', '.join(missing)}")


# ==================== لاگ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler("trading_signals_best.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== لایه داده ====================
class DataLayer:
    def __init__(self, config: Config):
        self.config = config
        try:
            import ccxt
            exchange_class = getattr(ccxt, config.EXCHANGE_ID)
            self.exchange = exchange_class({
                'apiKey': config.API_KEY,
                'secret': config.SECRET,
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'}
            })
        except Exception:
            self.exchange = None

    BINANCE_PUBLIC_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]

    def _binance_get(self, path: str, params: dict):
        last_error = None
        for host in self.BINANCE_PUBLIC_HOSTS:
            try:
                resp = requests.get(f"{host}{path}", params=params, timeout=10)
                if resp.status_code == 200:
                    return resp.json()
                last_error = f"{host} -> HTTP {resp.status_code}: {resp.text[:120]}"
            except Exception as e:
                last_error = f"{host} -> {e}"
        raise RuntimeError(last_error)

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        try:
            raw = self._binance_get("/api/v3/klines", {
                "symbol": symbol.replace("/", ""), "interval": timeframe, "limit": limit
            })
            ohlcv = [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in raw]
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception as e:
            logger.error(f"خطا در دریافت داده {symbol} در تایم‌فریم {timeframe}: {e}")
            return pd.DataFrame()

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        if not self.exchange:
            return None
        try:
            funding_symbol = symbol.replace("/USDT", "/USDT:USDT")
            data = self.exchange.fetch_funding_rate(funding_symbol)
            rate = data.get("fundingRate") if data else None
            return float(rate) if rate is not None else None
        except Exception:
            return None

    def fetch_spread_pct(self, symbol: str) -> Optional[float]:
        try:
            ob = self._binance_get("/api/v3/depth", {"symbol": symbol.replace("/", ""), "limit": 5})
            bids = [[float(x[0]), float(x[1])] for x in ob.get("bids", [])]
            asks = [[float(x[0]), float(x[1])] for x in ob.get("asks", [])]
            best_bid = bids[0][0] if bids else None
            best_ask = asks[0][0] if asks else None
            if not best_bid or not best_ask:
                return None
            mid = (best_bid + best_ask) / 2
            if mid <= 0:
                return None
            return float((best_ask - best_bid) / mid * 100)
        except Exception:
            return None


# ==================== داده‌ی کلان ====================
class MacroDataLayer:
    def __init__(self):
        self._fng_value: Optional[int] = None
        self._fng_last_fetch: float = 0.0
        self._fng_cache_seconds = 3600

    def get_fear_greed_index(self) -> Optional[int]:
        now = time.time()
        if self._fng_value is not None and (now - self._fng_last_fetch) < self._fng_cache_seconds:
            return self._fng_value
        try:
            resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                value = int(data["data"][0]["value"])
                self._fng_value = value
                self._fng_last_fetch = now
                return value
        except Exception as e:
            logger.warning(f"دریافت شاخص ترس‌وطمع ناموفق: {e}")
        return self._fng_value


# ==================== لایه تحلیل ====================
class AnalysisLayer:
    def __init__(self, config: Config):
        self.config = config

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < 30:
            return df
        df = df.copy()
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        df['ema_fast'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=50, adjust=False).mean()
        df['ema_trend'] = df['close'].ewm(span=200, adjust=False).mean()
        ema12 = df['close'].ewm(span=12, adjust=False).mean()
        ema26 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = ema12 - ema26
        df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        df['macd_hist'] = df['macd'] - df['macd_signal']
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close = (df['low'] - df['close'].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['true_range'] = tr
        df['atr'] = tr.rolling(window=14).mean()
        df['vol_sma'] = df['volume'].rolling(window=20).mean()
        df['support'] = df['low'].rolling(window=20).min()
        df['resistance'] = df['high'].rolling(window=20).max()
        return df

    def is_market_tradable(self, df_15m: pd.DataFrame) -> bool:
        if df_15m.empty or len(df_15m) < 30:
            return False
        latest = df_15m.iloc[-1]
        if pd.isna(latest['volume']) or pd.isna(latest['vol_sma']) or latest['vol_sma'] == 0:
            return False
        volume_ratio = latest['volume'] / latest['vol_sma']
        if volume_ratio < 0.75:
            return False
        return True

    def get_major_trend(self, df_4h: pd.DataFrame) -> str:
        if df_4h.empty or len(df_4h) < 100:
            return "NEUTRAL"
        latest = df_4h.iloc[-1]
        if latest['close'] > latest['ema_trend'] and latest['ema_fast'] > latest['ema_slow']:
            return "BULLISH"
        elif latest['close'] < latest['ema_trend'] and latest['ema_fast'] < latest['ema_slow']:
            return "BEARISH"
        return "NEUTRAL"

    def is_mtf_aligned(self, df_1h: pd.DataFrame, side: str) -> bool:
        if df_1h.empty or len(df_1h) < 60:
            return False
        latest = df_1h.iloc[-1]
        if pd.isna(latest.get('ema_fast')) or pd.isna(latest.get('ema_slow')):
            return False
        if side == "BUY":
            return bool(latest['ema_fast'] > latest['ema_slow'])
        return bool(latest['ema_fast'] < latest['ema_slow'])

    def market_structure(self, df: pd.DataFrame, lookback: int = 40) -> str:
        if df.empty or len(df) < lookback + 4:
            return "NEUTRAL"
        window = df.tail(lookback).reset_index(drop=True)
        swing_highs, swing_lows = [], []
        for i in range(2, len(window) - 2):
            h = window['high']
            l = window['low']
            if h[i] > h[i - 1] and h[i] > h[i - 2] and h[i] > h[i + 1] and h[i] > h[i + 2]:
                swing_highs.append(h[i])
            if l[i] < l[i - 1] and l[i] < l[i - 2] and l[i] < l[i + 1] and l[i] < l[i + 2]:
                swing_lows.append(l[i])
        if len(swing_highs) >= 2 and len(swing_lows) >= 2:
            higher_high = swing_highs[-1] > swing_highs[-2]
            higher_low = swing_lows[-1] > swing_lows[-2]
            lower_high = swing_highs[-1] < swing_highs[-2]
            lower_low = swing_lows[-1] < swing_lows[-2]
            if higher_high and higher_low:
                return "BULLISH"
            if lower_high and lower_low:
                return "BEARISH"
        return "NEUTRAL"

    def atr_percentile(self, df: pd.DataFrame, window: int = 100) -> float:
        if df.empty or 'true_range' not in df or len(df) < 30:
            return 50.0
        recent = df['true_range'].tail(window).dropna()
        if recent.empty or pd.isna(df['true_range'].iloc[-1]):
            return 50.0
        current = df['true_range'].iloc[-1]
        return float((recent < current).mean() * 100)


# ==================== ردیاب سهمیه Groq ====================
class GroqQuotaTracker:
    def __init__(self):
        self.remaining_requests: Optional[int] = None
        self.remaining_tokens: Optional[int] = None
        self.last_updated: Optional[datetime] = None
        self.OPTIMIZER_MIN_REMAINING_REQUESTS = 50
        self.OPTIMIZER_MIN_REMAINING_TOKENS = 2500
        self.JUDGE_MIN_REMAINING_REQUESTS = 4
        self.JUDGE_MIN_REMAINING_TOKENS = 600

    def update_from_headers(self, headers) -> None:
        try:
            if "x-ratelimit-remaining-requests" in headers:
                self.remaining_requests = int(float(headers["x-ratelimit-remaining-requests"]))
            if "x-ratelimit-remaining-tokens" in headers:
                self.remaining_tokens = int(float(headers["x-ratelimit-remaining-tokens"]))
            self.last_updated = datetime.now()
        except (ValueError, TypeError):
            pass

    def can_optimize(self) -> bool:
        if self.remaining_requests is None or self.remaining_tokens is None:
            return True
        return (self.remaining_requests > self.OPTIMIZER_MIN_REMAINING_REQUESTS and
                self.remaining_tokens > self.OPTIMIZER_MIN_REMAINING_TOKENS)

    def can_judge(self) -> bool:
        if self.remaining_requests is None or self.remaining_tokens is None:
            return True
        return (self.remaining_requests > self.JUDGE_MIN_REMAINING_REQUESTS and
                self.remaining_tokens > self.JUDGE_MIN_REMAINING_TOKENS)


class GroqDailyBudget:
    def __init__(self, config: Config):
        self.config = config
        self._day = date_cls.today()
        self.tokens_used_today = 0
        self.requests_used_today = 0
        self.used_by_consumer = {"judge": 0, "regime": 0, "optimizer": 0}

    def _roll_day_if_needed(self):
        today = date_cls.today()
        if today != self._day:
            self._day = today
            self.tokens_used_today = 0
            self.requests_used_today = 0
            self.used_by_consumer = {"judge": 0, "regime": 0, "optimizer": 0}

    def record_usage(self, consumer: str, response_json: Optional[dict], fallback_tokens: int = 700):
        self._roll_day_if_needed()
        used = fallback_tokens
        try:
            usage = (response_json or {}).get("usage") or {}
            total = usage.get("total_tokens")
            if total:
                used = int(total)
        except Exception:
            pass
        self.tokens_used_today += used
        self.requests_used_today += 1
        self.used_by_consumer[consumer] = self.used_by_consumer.get(consumer, 0) + used

    def _hard_cap_reached(self) -> bool:
        if self.tokens_used_today >= self.config.GROQ_DAILY_TOKEN_CAP * self.config.GROQ_BUDGET_HARD_STOP_FRACTION:
            return True
        if self.requests_used_today >= self.config.GROQ_DAILY_REQUEST_CAP * self.config.GROQ_BUDGET_HARD_STOP_FRACTION:
            return True
        return False

    def can_consume(self, consumer: str) -> bool:
        self._roll_day_if_needed()
        if self._hard_cap_reached():
            return False
        if consumer == "judge":
            return True
        if consumer == "regime":
            reserved = self.config.GROQ_DAILY_TOKEN_CAP * self.config.GROQ_REGIME_RESERVED_SHARE
            if self.used_by_consumer.get("regime", 0) < reserved:
                return True
            return self._spare_capacity_tokens() > 0
        if consumer == "optimizer":
            optimizer_share = self.config.GROQ_DAILY_TOKEN_CAP * (
                1 - self.config.GROQ_JUDGE_RESERVED_SHARE - self.config.GROQ_REGIME_RESERVED_SHARE
            )
            if self.used_by_consumer.get("optimizer", 0) < optimizer_share:
                return True
            return self._spare_capacity_tokens() > 0
        return True

    def _spare_capacity_tokens(self) -> float:
        cap = self.config.GROQ_DAILY_TOKEN_CAP * self.config.GROQ_BUDGET_HARD_STOP_FRACTION
        return max(0.0, cap - self.tokens_used_today)

    def get_optimizer_pace_ratio(self) -> float:
        self._roll_day_if_needed()
        now = datetime.now()
        seconds_since_midnight = (now - datetime.combine(now.date(), datetime.min.time())).total_seconds()
        day_fraction = max(seconds_since_midnight / 86400.0, 0.01)
        optimizer_share_tokens = self.config.GROQ_DAILY_TOKEN_CAP * (
            1 - self.config.GROQ_JUDGE_RESERVED_SHARE - self.config.GROQ_REGIME_RESERVED_SHARE
        )
        expected_used = optimizer_share_tokens * day_fraction
        if expected_used <= 0:
            return 1.0
        return self.used_by_consumer.get("optimizer", 0) / expected_used

    def get_dynamic_optimizer_interval(self) -> timedelta:
        pace = self.get_optimizer_pace_ratio()
        lo = self.config.GROQ_OPTIMIZER_MIN_INTERVAL_HOURS
        hi = self.config.GROQ_OPTIMIZER_MAX_INTERVAL_HOURS
        if pace > 1.3:
            return timedelta(hours=hi)
        if pace < 0.5:
            return timedelta(hours=lo)
        return timedelta(hours=(lo + hi) / 2)

    def get_status_summary(self) -> str:
        self._roll_day_if_needed()
        return (f"مصرف امروز: {self.tokens_used_today:,}/{self.config.GROQ_DAILY_TOKEN_CAP:,} توکن "
                f"({self.requests_used_today}/{self.config.GROQ_DAILY_REQUEST_CAP} درخواست) | "
                f"قضاوت: {self.used_by_consumer.get('judge', 0):,} | "
                f"رژیم: {self.used_by_consumer.get('regime', 0):,} | "
                f"تنظیم پارامتر: {self.used_by_consumer.get('optimizer', 0):,}")


# ==================== هوش مصنوعی ====================
class AIParameterOptimizer:
    def __init__(self, config):
        self.config = config
        self.groq_api_key = config.GROQ_API_KEY
        self.groq_endpoint = "https://api.groq.com/openai/"
        self.blacklist: Dict[str, dict] = {}
        self.BLACKLIST_MIN_COOLDOWN_MINUTES = 15
        self.BLACKLIST_EARLY_RELEASE_PCTL = 68
        self.MAX_RETRIES_429 = 2
        self.MAX_BACKOFF_SECONDS = 8
        self.GROQ_TARGET_RPM = 12
        self.groq_min_interval_seconds = 60.0 / self.GROQ_TARGET_RPM
        self._last_groq_call_ts = 0.0
        self.quota = GroqQuotaTracker()
        self.budget = GroqDailyBudget(config)
        self.on_quota_exhausted_callback: Optional[callable] = None
        self._judge_quota_alert_sent = False
        self._judge_error_alert_sent = False
        self.CONNECTION_FAILURE_ALERT_THRESHOLD = 3
        self._consecutive_connection_failures = 0
        self._connection_alert_sent = False

        # پارامترهای بهینه بهترین بک‌تست
        default_params = {
            "rsi_buy_min": 36,
            "rsi_buy_max_range_start": 38,
            "rsi_buy_max_range_end": 72,
            "volume_mult": 0.90,
            "atr_min_filter": 0.0010,
            "cooldown_minutes": 45,
            "sl_atr_mult": 1.25,
            "tp1_mult": 1.55,
            "tp2_mult": 2.60,
            "tp3_mult": 4.30,
            "trailing_mult": 1.15,
            "stress_pctl_threshold": 86,
            "stress_size_mult": 0.52
        }

        SYMBOL_PARAM_OVERRIDES = {
            "NEAR/USDT": {"atr_min_filter": 0.0014, "sl_atr_mult": 1.30, "tp1_mult": 1.60, "cooldown_minutes": 40},
            "PAXG/USDT": {"atr_min_filter": 0.0006, "sl_atr_mult": 1.15, "tp1_mult": 1.50, "rsi_buy_max_range_end": 65},
            "LTC/USDT":  {"atr_min_filter": 0.0011, "sl_atr_mult": 1.28},
            "BNB/USDT":  {"atr_min_filter": 0.0010, "sl_atr_mult": 1.25},
            "XRP/USDT":  {"atr_min_filter": 0.0013, "sl_atr_mult": 1.30, "cooldown_minutes": 50},
            "LINK/USDT": {"atr_min_filter": 0.0012, "sl_atr_mult": 1.28},
        }

        self.symbol_states = {}
        for sym in config.SYMBOLS:
            merged_params = {**default_params, **SYMBOL_PARAM_OVERRIDES.get(sym, {})}
            self.symbol_states[sym] = {
                "last_optimized_time": None,
                "consecutive_losses": 0,
                "params": self.validate_and_clamp_params(merged_params)
            }
        self.optimization_interval = timedelta(hours=3)

    def is_blacklisted(self, symbol: str, current_pctl: Optional[float] = None) -> bool:
        if symbol not in self.blacklist:
            return False
        entry = self.blacklist[symbol]
        now = datetime.now()
        if now >= entry["hard_release_at"]:
            del self.blacklist[symbol]
            return False
        if now - entry["blocked_at"] < timedelta(minutes=self.BLACKLIST_MIN_COOLDOWN_MINUTES):
            return True
        if current_pctl is not None and current_pctl < self.BLACKLIST_EARLY_RELEASE_PCTL:
            logger.info(f"سیستم ضد ضرر: {symbol} زودتر آزاد شد (پرسنتایل {current_pctl:.0f})")
            del self.blacklist[symbol]
            return False
        return True

    def register_loss(self, symbol: str):
        state = self.symbol_states[symbol]
        state["consecutive_losses"] += 1
        penalty_hours = min(1.3 * state["consecutive_losses"], 5.5)
        now = datetime.now()
        self.blacklist[symbol] = {
            "blocked_at": now,
            "hard_release_at": now + timedelta(hours=penalty_hours)
        }
        logger.warning(f"سیستم ضد ضرر: {symbol} تا حداکثر {penalty_hours:.1f} ساعت مسدود")

    def register_win(self, symbol: str):
        state = self.symbol_states[symbol]
        state["consecutive_losses"] = 0
        if symbol in self.blacklist:
            del self.blacklist[symbol]

    def validate_and_clamp_params(self, new_params: dict) -> dict:
        clamped = {}
        clamped["rsi_buy_min"] = max(28, min(float(new_params.get("rsi_buy_min", 36)), 48))
        clamped["rsi_buy_max_range_start"] = max(35, min(float(new_params.get("rsi_buy_max_range_start", 38)), 55))
        clamped["rsi_buy_max_range_end"] = max(55, min(float(new_params.get("rsi_buy_max_range_end", 72)), 78))
        clamped["volume_mult"] = max(0.7, min(float(new_params.get("volume_mult", 0.90)), 1.4))
        clamped["atr_min_filter"] = max(0.0005, min(float(new_params.get("atr_min_filter", 0.0010)), 0.0035))
        clamped["cooldown_minutes"] = max(25, min(int(new_params.get("cooldown_minutes", 45)), 180))
        clamped["sl_atr_mult"] = max(1.05, min(float(new_params.get("sl_atr_mult", 1.25)), 2.2))
        clamped["tp1_mult"] = max(1.35, min(float(new_params.get("tp1_mult", 1.55)), 2.8))
        clamped["tp2_mult"] = max(2.1, min(float(new_params.get("tp2_mult", 2.60)), 5.0))
        clamped["tp3_mult"] = max(3.2, min(float(new_params.get("tp3_mult", 4.30)), 8.0))
        clamped["trailing_mult"] = max(0.75, min(float(new_params.get("trailing_mult", 1.15)), 2.2))
        clamped["stress_pctl_threshold"] = max(78, min(float(new_params.get("stress_pctl_threshold", 86)), 94))
        clamped["stress_size_mult"] = max(0.35, min(float(new_params.get("stress_size_mult", 0.52)), 0.80))
        return clamped

    def _wait_for_groq_slot(self):
        elapsed = time.time() - self._last_groq_call_ts
        remaining = self.groq_min_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _register_connection_result(self, success: bool, label: str):
        if success:
            self._consecutive_connection_failures = 0
            if self._connection_alert_sent:
                self._connection_alert_sent = False
                if self.on_quota_exhausted_callback:
                    try:
                        self.on_quota_exhausted_callback("✅ اتصال به هوش مصنوعی (Groq) دوباره برقرار شد.")
                    except Exception:
                        pass
        else:
            self._consecutive_connection_failures += 1
            if (self._consecutive_connection_failures >= self.CONNECTION_FAILURE_ALERT_THRESHOLD
                    and not self._connection_alert_sent):
                self._connection_alert_sent = True
                if self.on_quota_exhausted_callback:
                    try:
                        self.on_quota_exhausted_callback(
                            f"⚠️ اتصال به هوش مصنوعی قطع شده (آخرین: {label}).\n"
                            "تا برقراری دوباره هیچ سیگنال جدیدی ارسال نمی‌شود."
                        )
                    except Exception:
                        pass

    def _post_with_retry(self, url: str, payload: dict, headers: dict, timeout: int, label: str) -> Optional[requests.Response]:
        for attempt in range(self.MAX_RETRIES_429 + 1):
            self._wait_for_groq_slot()
            try:
                response = requests.post(url, json=payload, headers=headers, timeout=timeout)
            except requests.exceptions.RequestException:
                self._register_connection_result(False, label)
                raise
            finally:
                self._last_groq_call_ts = time.time()
            self._register_connection_result(True, label)
            self.quota.update_from_headers(response.headers)
            if response.status_code != 429:
                return response
            if attempt >= self.MAX_RETRIES_429:
                return response
            retry_after = response.headers.get("Retry-After") or response.headers.get("retry-after")
            try:
                wait_seconds = float(retry_after) if retry_after is not None else 2 * (attempt + 1)
            except ValueError:
                wait_seconds = 2 * (attempt + 1)
            wait_seconds = min(wait_seconds, self.MAX_BACKOFF_SECONDS)
            logger.warning(f"Groq 429 برای {label}; {wait_seconds:.1f}s صبر...")
            time.sleep(wait_seconds)
        return response

    def should_optimize(self, symbol: str) -> bool:
        if not self.quota.can_optimize() or not self.budget.can_consume("optimizer"):
            return False
        state = self.symbol_states[symbol]
        if state["last_optimized_time"] is None:
            return True
        dynamic_interval = self.budget.get_dynamic_optimizer_interval()
        return datetime.now() - state["last_optimized_time"] >= dynamic_interval

    def optimize_symbol_parameters(self, symbol: str, df_15m: pd.DataFrame, journal: Optional["TradeJournal"] = None):
        if not self.groq_api_key or df_15m.empty:
            return
        state = self.symbol_states[symbol]
        latest = df_15m.iloc[-1]
        market_metrics = {
            "symbol": symbol,
            "close_price": float(latest['close']),
            "rsi": float(latest['rsi']) if not pd.isna(latest['rsi']) else 50,
            "atr_volatility": float(latest['atr']) if not pd.isna(latest['atr']) else 0,
            "current_volume": float(latest['volume']) if not pd.isna(latest['volume']) else 0,
            "volume_sma": float(latest['vol_sma']) if not pd.isna(latest['vol_sma']) else 0,
            "support": float(latest['support']) if not pd.isna(latest['support']) else 0,
            "resistance": float(latest['resistance']) if not pd.isna(latest['resistance']) else 0,
            "consecutive_losses": state["consecutive_losses"]
        }
        side_perf_note = ""
        if journal is not None:
            buy_perf = journal.get_side_performance(symbol, "BUY")
            market_metrics["recent_buy_performance"] = buy_perf
            side_perf_note = """
IMPORTANT: recent_buy_performance shows this symbol's last BUY trades.
Prefer adjusting side-specific keys rather than shared risk keys when one side underperforms.
Never make changes so aggressive that signals stop entirely. Keep tp1_mult >= 1.35.
"""
        prompt = f"""
You are an advanced quantitative trading AI for SPOT LONG only.
Analyze: {json.dumps(market_metrics)}
{side_perf_note}
Tune parameters for current regime. Keep risk strict. Return ONLY valid JSON with exact keys:
{json.dumps(state["params"])}
No markdown, no extra text.
"""
        headers = {"Authorization": f"Bearer {self.groq_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": "openai/gpt-oss-120b",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "reasoning_effort": "low",
            "max_tokens": 600
        }
        try:
            response = self._post_with_retry(f"{self.groq_endpoint}v1/chat/completions", payload, headers, timeout=25, label=symbol)
            if response.status_code == 200:
                res_data = response.json()
                self.budget.record_usage("optimizer", res_data, fallback_tokens=700)
                content = res_data['choices'][0]['message']['content'].strip()
                if content.startswith("```"):
                    content = content.strip("`").replace("json\n", "").strip()
                if not content:
                    raise ValueError("پاسخ خالی از Groq")
                raw_params = json.loads(content)
                state["params"] = self.validate_and_clamp_params(raw_params)
                state["last_optimized_time"] = datetime.now()
                logger.info(f"پارامترهای {symbol} بروزرسانی شد.")
            else:
                logger.warning(f"Groq برای {symbol} پاسخ {response.status_code}")
        except Exception as e:
            logger.error(f"خطا در بهینه‌سازی {symbol}: {e}")

    def get_params(self, symbol: str) -> dict:
        return self.symbol_states[symbol]["params"]

    def assess_market_regime(self, macro_context: dict, portfolio_stats: dict) -> Optional[dict]:
        if not self.groq_api_key:
            return None
        payload_metrics = {
            "btc_trend_4h": macro_context.get("btc_trend_4h"),
            "btc_structure": macro_context.get("btc_structure"),
            "btc_rsi": macro_context.get("btc_rsi"),
            "btc_macd_hist": macro_context.get("btc_macd_hist"),
            "btc_volatility_percentile": macro_context.get("btc_volatility_pctl"),
            "fear_greed_index": macro_context.get("fear_greed"),
            "portfolio_rolling_win_rate_pct": portfolio_stats.get("win_rate"),
            "portfolio_rolling_avg_r": portfolio_stats.get("avg_r"),
            "portfolio_consecutive_losses": portfolio_stats.get("consecutive_losses"),
            "portfolio_sample_count": portfolio_stats.get("sample_count"),
        }
        prompt = f"""You are a senior crypto macro strategist. Assess overall market regime:
{json.dumps(payload_metrics, ensure_ascii=False)}
Respond ONLY with valid JSON:
{{"market_regime": "trending_bullish" or "trending_bearish" or "choppy_range" or "high_volatility" or "uncertain", "aggressiveness_mult": number between 0.5 and 1.20, "reasoning": "one concise sentence in Persian"}}
"""
        headers = {"Authorization": f"Bearer {self.groq_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": "openai/gpt-oss-120b",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "reasoning_effort": "low",
            "max_tokens": 400
        }
        try:
            response = self._post_with_retry(f"{self.groq_endpoint}v1/chat/completions", payload, headers, timeout=20, label="PORTFOLIO_REGIME")
            if response.status_code != 200:
                return None
            res_data = response.json()
            self.budget.record_usage("regime", res_data, fallback_tokens=400)
            content = res_data['choices'][0]['message']['content'].strip()
            if content.startswith("```"):
                content = content.strip("`").replace("json\n", "").strip()
            if not content:
                return None
            result = json.loads(content)
            mult = max(0.5, min(1.20, float(result.get("aggressiveness_mult", 1.0))))
            regime = str(result.get("market_regime", "uncertain"))
            reasoning = str(result.get("reasoning", ""))[:300]
            logger.info(f"رژیم بازار: {regime} | ضریب: {mult:.2f} | {reasoning}")
            return {"market_regime": regime, "aggressiveness_mult": mult, "reasoning": reasoning}
        except Exception as e:
            logger.error(f"خطا در ارزیابی رژیم: {e}")
            return None

    def _alert_judge_error(self, message: str):
        if not self._judge_error_alert_sent:
            self._judge_error_alert_sent = True
            if self.on_quota_exhausted_callback:
                try:
                    self.on_quota_exhausted_callback(f"⚠️ {message}")
                except Exception:
                    pass

    def _reset_judge_error_alert(self):
        if self._judge_error_alert_sent:
            self._judge_error_alert_sent = False
            if self.on_quota_exhausted_callback:
                try:
                    self.on_quota_exhausted_callback("✅ لایه‌ی قضاوت AI دوباره سالم است.")
                except Exception:
                    pass

    def evaluate_trade_candidate(self, symbol: str, side: str, context: dict) -> Dict:
        default = {"approve": False, "confidence": 0, "reason": "بدون دسترسی به AI - سیگنال رد شد"}
        if not self.groq_api_key:
            return default
        if self.quota.can_judge() and self.budget.can_consume("judge"):
            if self._judge_quota_alert_sent:
                self._judge_quota_alert_sent = False
                if self.on_quota_exhausted_callback:
                    try:
                        self.on_quota_exhausted_callback("✅ سهمیه Groq دوباره در دسترس است.")
                    except Exception:
                        pass
        else:
            if not self._judge_quota_alert_sent:
                self._judge_quota_alert_sent = True
                if self.on_quota_exhausted_callback:
                    try:
                        self.on_quota_exhausted_callback(
                            f"⚠️ سهمیه Groq تموم شد.\n{self.budget.get_status_summary()}\n"
                            "تا برگشت سهمیه سیگنال جدید ارسال نمی‌شود."
                        )
                    except Exception:
                        pass
            return {"approve": False, "confidence": 0, "reason": "سهمیه Groq تموم شده"}

        prompt = f"""You are a veteran discretionary crypto trader. Review this SPOT LONG candidate that already passed quantitative filters.
Trade: Symbol={symbol} Side={side}
Context: {json.dumps(context, ensure_ascii=False)}
Respond ONLY valid JSON:
{{"approve": true or false, "confidence": integer 0-100, "reason": "one concise sentence in Persian"}}
"""
        headers = {"Authorization": f"Bearer {self.groq_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": "openai/gpt-oss-120b",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.25,
            "reasoning_effort": "low",
            "max_tokens": 500
        }
        try:
            response = self._post_with_retry(f"{self.groq_endpoint}v1/chat/completions", payload, headers, timeout=20, label=symbol)
            if response.status_code != 200:
                self._alert_judge_error(f"قضاوت AI پاسخ {response.status_code} داد ({symbol})")
                return default
            res_data = response.json()
            self.budget.record_usage("judge", res_data, fallback_tokens=500)
            content = res_data['choices'][0]['message']['content'].strip()
            if content.startswith("```"):
                content = content.strip("`").replace("json\n", "").strip()
            if not content:
                raise ValueError("پاسخ خالی")
            result = json.loads(content)
            self._reset_judge_error_alert()
            return {
                "approve": bool(result.get("approve", True)),
                "confidence": int(result.get("confidence", 50)),
                "reason": str(result.get("reason", ""))[:300]
            }
        except Exception as e:
            logger.error(f"خطا در قضاوت AI {symbol}: {e}")
            self._alert_judge_error(f"خطا در قضاوت AI برای {symbol}: {e}")
            return default


# ==================== وضعیت پرتفوی ====================
class PortfolioStateManager:
    def __init__(self, config: Config, journal: "TradeJournal", telegram_sender):
        self.config = config
        self.journal = journal
        self.telegram = telegram_sender
        self.state = "NORMAL"
        self.last_stats: Dict = {}
        self.ai_regime: Dict = {"market_regime": "uncertain", "aggressiveness_mult": 1.0, "reasoning": ""}
        self._last_ai_regime_time: Optional[datetime] = None

    def _consecutive_losses_global(self) -> int:
        count = 0
        for r in reversed(self.journal.records):
            if r["pnl_usdt"] <= 0:
                count += 1
            else:
                break
        return count

    def get_stats(self) -> Dict:
        recent = self.journal.records[-self.config.PORTFOLIO_ROLLING_WINDOW:]
        if not recent:
            return {"sample_count": 0, "win_rate": None, "avg_r": None, "consecutive_losses": 0}
        wins = [r for r in recent if r["pnl_usdt"] > 0]
        return {
            "sample_count": len(recent),
            "win_rate": round(len(wins) / len(recent) * 100, 1),
            "avg_r": round(sum(r["r_multiple"] for r in recent) / len(recent), 2),
            "consecutive_losses": self._consecutive_losses_global()
        }

    def recompute(self) -> str:
        prev_state = self.state
        stats = self.get_stats()
        self.last_stats = stats
        if stats["sample_count"] < self.config.PORTFOLIO_MIN_SAMPLES:
            new_state = "NORMAL"
        elif (stats["consecutive_losses"] >= self.config.PORTFOLIO_CONSECUTIVE_LOSS_THRESHOLD
              or stats["avg_r"] <= -0.15 or stats["win_rate"] < 38):
            new_state = "DEFENSIVE"
        elif stats["avg_r"] <= 0.12 or stats["win_rate"] < 52:
            new_state = "CAUTION"
        else:
            new_state = "NORMAL"
        self.state = new_state
        if new_state != prev_state:
            self._notify_transition(prev_state, new_state, stats)
        return self.state

    def _notify_transition(self, prev_state: str, new_state: str, stats: Dict):
        labels = {"NORMAL": "🟢 عادی", "CAUTION": "🟡 محتاط", "DEFENSIVE": "🔴 تدافعی"}
        msg = f"""
🧭 **تغییر حالت پرتفوی: {labels.get(prev_state)} ← {labels.get(new_state)}**

بر اساس {stats['sample_count']} رخداد اخیر:
📈 نرخ برد: {stats['win_rate']}%
📐 میانگین R: {stats['avg_r']:+.2f}R
🔻 ضرر متوالی: {stats['consecutive_losses']}
"""
        if new_state == "DEFENSIVE":
            msg += "حالت تدافعی: آستانه بالاتر، حجم کمتر."
        elif new_state == "CAUTION":
            msg += "حالت محتاط: سخت‌گیری کمی بیشتر."
        else:
            msg += "بازگشت به تنظیمات عادی."
        self.telegram.send_personal_message(msg)

    def should_refresh_ai_regime(self) -> bool:
        if self._last_ai_regime_time is None:
            return True
        return datetime.now() - self._last_ai_regime_time >= timedelta(hours=self.config.MARKET_REGIME_AI_INTERVAL_HOURS)

    def set_ai_regime(self, result: Optional[Dict]):
        self._last_ai_regime_time = datetime.now()
        if result:
            self.ai_regime = result

    def get_adjustments(self) -> Dict:
        presets = {
            "NORMAL":    {"score_adjustment": 0.0, "confidence_adjustment": 0,  "size_mult": 1.0,  "cooldown_mult": 1.0},
            "CAUTION":   {"score_adjustment": 0.7, "confidence_adjustment": 7,  "size_mult": 0.68, "cooldown_mult": 1.35},
            "DEFENSIVE": {"score_adjustment": 1.6, "confidence_adjustment": 15, "size_mult": 0.38, "cooldown_mult": 2.0},
        }
        adj = dict(presets[self.state])
        ai_mult = (self.ai_regime or {}).get("aggressiveness_mult", 1.0)
        adj["size_mult"] = max(0.20, min(1.30, adj["size_mult"] * ai_mult))
        adj["defensive"] = (self.state == "DEFENSIVE")
        adj["state"] = self.state
        adj["ai_market_regime"] = (self.ai_regime or {}).get("market_regime")
        return adj


# ==================== موتور سیگنال (بهترین نسخه) ====================
class SignalEngine:
    def __init__(self, config: Config, ai_optimizer: AIParameterOptimizer, analysis: AnalysisLayer):
        self.config = config
        self.ai_optimizer = ai_optimizer
        self.analysis = analysis

    def _score_buy_trend(self, latest, prev, p) -> float:
        score = 0.0
        if latest['ema_fast'] > latest['ema_slow']:
            score += 2.1
        rsi_cross = latest['rsi'] > p["rsi_buy_min"] and prev['rsi'] <= p["rsi_buy_min"]
        rsi_zone = p["rsi_buy_max_range_start"] <= latest['rsi'] <= p["rsi_buy_max_range_end"] and latest['rsi'] > prev['rsi']
        if rsi_cross:
            score += 2.2
        elif rsi_zone:
            score += 1.6
        if not pd.isna(latest.get('macd_hist', float('nan'))):
            if latest['macd_hist'] > 0:
                score += 1.1
            elif latest['macd_hist'] > prev.get('macd_hist', 0):
                score += 0.55
        vol_ratio = latest['volume'] / latest['vol_sma'] if latest['vol_sma'] else 0
        if vol_ratio >= p["volume_mult"]:
            score += 1.4
        elif vol_ratio >= p["volume_mult"] * 0.85:
            score += 0.7
        if latest['support'] > 0 and (latest['close'] - latest['support']) / latest['close'] < 0.015:
            score += 1.1
        return score

    def get_rule_signal(self, symbol: str, df_15m: pd.DataFrame, df_1h: pd.DataFrame, trend_4h: str,
                         macro_context: Optional[dict] = None, portfolio_adjustments: Optional[dict] = None) -> Tuple[Optional[str], dict]:
        if df_15m.empty or len(df_15m) < 30:
            return None, {}

        pctl = self.analysis.atr_percentile(df_15m)
        if self.ai_optimizer.is_blacklisted(symbol, current_pctl=pctl):
            return None, {}
        if not self.analysis.is_market_tradable(df_15m):
            return None, {}

        latest = df_15m.iloc[-1]
        prev = df_15m.iloc[-2]
        p = self.ai_optimizer.get_params(symbol)

        if pd.isna(latest['rsi']) or pd.isna(latest['ema_fast']) or pd.isna(latest['atr']):
            return None, {}
        if latest['atr'] < (latest['close'] * p["atr_min_filter"]):
            return None, {}

        macro_context = macro_context or {}
        portfolio_adjustments = portfolio_adjustments or {}
        score_adjustment = portfolio_adjustments.get("score_adjustment", 0.0)
        defensive_mode = portfolio_adjustments.get("defensive", False)

        fng = macro_context.get("fear_greed")
        btc_trend_4h = macro_context.get("btc_trend_4h")
        btc_structure = macro_context.get("btc_structure")
        funding_rate = macro_context.get("funding_rate")
        spread_pct = macro_context.get("spread_pct")

        if spread_pct is not None and spread_pct > 0.9:
            return None, {}
        if pctl > self.config.ATR_PERCENTILE_MAX:
            return None, {}

        structure = self.analysis.market_structure(df_15m)
        mtf_ok = self.analysis.is_mtf_aligned(df_1h, "BUY")

        # --- مسیر ۱: روند خالص BULLISH ---
        is_trend = False
        buy_score = 0.0
        if trend_4h == "BULLISH" and mtf_ok:
            is_trend = True
            buy_score = self._score_buy_trend(latest, prev, p)
            buy_score += 1.5  # trend bonus
            if structure == "BULLISH":
                buy_score += 1.9
            buy_score += 1.5  # mtf bonus

            # فیلتر BTC
            if symbol != "BTC/USDT" and btc_trend_4h == "BEARISH":
                if btc_structure == "BEARISH":
                    is_trend = False
                else:
                    buy_score -= 1.8
            elif symbol != "BTC/USDT" and btc_trend_4h == "BULLISH" and btc_structure == "BULLISH":
                buy_score += 1.1

        # --- مسیر ۲: پولبک / Mean-Reversion ---
        is_mr = False
        if (trend_4h == "BULLISH" or (trend_4h == "NEUTRAL" and structure == "BULLISH")):
            if 28 < latest['rsi'] < 49 and latest['rsi'] > prev['rsi'] and latest['close'] > latest['ema_slow'] * 0.995:
                near_ema = latest['low'] <= latest['ema_fast'] * 1.008
                near_support = latest['support'] > 0 and (latest['close'] - latest['support']) / latest['close'] < 0.013
                if near_ema or near_support:
                    is_mr = True
                    buy_score = max(buy_score, 8.1)
                    if mtf_ok:
                        buy_score += 1.0
                    if structure == "BULLISH":
                        buy_score += 1.4
                    vol_ratio = latest['volume'] / latest['vol_sma'] if latest['vol_sma'] else 0
                    if vol_ratio > 1.0:
                        buy_score += 0.8

        if not is_trend and not is_mr:
            return None, {}

        if fng is not None:
            if fng <= 22:
                buy_score += 0.4
            elif fng >= 82:
                buy_score -= 0.5

        if funding_rate is not None:
            if funding_rate > 0.0006:
                buy_score -= 0.4
            elif funding_rate < -0.0005:
                buy_score += 0.4

        threshold = self.config.MIN_SIGNAL_SCORE + score_adjustment
        vol_ratio = latest['volume'] / latest['vol_sma'] if latest['vol_sma'] else 0
        diagnostics = {
            "structure": structure,
            "trend_4h": trend_4h,
            "path": "TREND" if is_trend else "PULLBACK",
            "rsi": round(float(latest['rsi']), 1),
            "macd_hist": round(float(latest['macd_hist']), 6) if not pd.isna(latest.get('macd_hist', float('nan'))) else None,
            "volume_vs_avg_ratio": round(float(vol_ratio), 2),
            "atr_percentile_100candles": round(pctl, 1),
            "mtf_1h_aligned": mtf_ok,
            "consecutive_losses_this_symbol": self.ai_optimizer.symbol_states[symbol]["consecutive_losses"],
            "fear_greed_index": fng,
            "btc_macro_trend_4h": btc_trend_4h,
            "btc_macro_structure": btc_structure,
            "funding_rate": funding_rate,
            "spread_pct": round(spread_pct, 3) if spread_pct is not None else None,
            "portfolio_state": portfolio_adjustments.get("state"),
            "portfolio_ai_market_regime": portfolio_adjustments.get("ai_market_regime"),
            "signal_threshold_used": round(threshold, 2),
        }
        if buy_score >= threshold:
            diagnostics["quant_score"] = round(buy_score, 2)
            logger.info(f"{symbol}: امتیاز {buy_score:.2f} (آستانه {threshold}) | مسیر: {diagnostics['path']} | ساختار: {structure}")
            return "BUY", diagnostics
        return None, {}


# ==================== همبستگی ====================
class CorrelationManager:
    def __init__(self, config: Config, data_layer: DataLayer):
        self.config = config
        self.data = data_layer
        self.matrix: Optional[pd.DataFrame] = None
        self.last_computed: Optional[datetime] = None

    def should_refresh(self) -> bool:
        if self.matrix is None or self.last_computed is None:
            return True
        return datetime.now() - self.last_computed >= timedelta(hours=self.config.CORRELATION_REFRESH_HOURS)

    def refresh(self):
        closes = {}
        for symbol in self.config.SYMBOLS:
            df = self.data.fetch_ohlcv(symbol, timeframe=self.config.ENTRY_TIMEFRAME, limit=self.config.CORRELATION_LOOKBACK_CANDLES)
            if df.empty or len(df) < 30:
                continue
            closes[symbol] = df.set_index('timestamp')['close']
        if len(closes) < 2:
            return
        try:
            price_df = pd.DataFrame(closes).dropna()
            if len(price_df) < 20:
                return
            returns = price_df.pct_change().dropna()
            self.matrix = returns.corr()
            self.last_computed = datetime.now()
            logger.info("ماتریس همبستگی بازمحاسبه شد.")
        except Exception as e:
            logger.error(f"خطا در ماتریس همبستگی: {e}")

    def get_dynamic_threshold(self, btc_volatility_pctl: Optional[float]) -> float:
        lo, hi = self.config.CORRELATION_THRESHOLD_MIN, self.config.CORRELATION_THRESHOLD_MAX
        if btc_volatility_pctl is None:
            return (lo + hi) / 2
        frac = max(0.0, min(1.0, btc_volatility_pctl / 100))
        return hi - frac * (hi - lo)

    def correlated_count(self, symbol: str, active_trades: Dict, threshold: float) -> int:
        if self.matrix is None or symbol not in self.matrix:
            group = self.config.SYMBOL_GROUPS.get(symbol, "other")
            return sum(1 for t in active_trades.values() if self.config.SYMBOL_GROUPS.get(t['symbol'], "other") == group)
        count = 0
        for t in active_trades.values():
            other = t['symbol']
            if other == symbol:
                continue
            if other in self.matrix.index:
                corr = self.matrix.loc[symbol, other]
                if pd.notna(corr) and abs(corr) >= threshold:
                    count += 1
        return count


# ==================== ریسک ====================
class RiskManager:
    def __init__(self, config: Config):
        self.config = config

    def calculate_position_size(self, entry: float, stop: float, size_mult: float = 1.0) -> float:
        risk_amount = self.config.VIRTUAL_CAPITAL_USDT * (self.config.RISK_PER_TRADE_PCT / 100) * size_mult
        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0:
            return 0.0
        return risk_amount / risk_per_unit

    def can_open_trade(self, symbol: str, active_trades: Dict, correlation_manager: Optional["CorrelationManager"] = None,
                        btc_volatility_pctl: Optional[float] = None) -> Tuple[bool, str]:
        if correlation_manager is not None:
            threshold = correlation_manager.get_dynamic_threshold(btc_volatility_pctl)
            corr_count = correlation_manager.correlated_count(symbol, active_trades, threshold)
            if corr_count >= self.config.MAX_CORRELATED_TRADES:
                return False, f"سقف اکسپوژر همبسته (آستانه {threshold:.2f})"
        else:
            group = self.config.SYMBOL_GROUPS.get(symbol, "other")
            group_count = sum(
                1 for t in active_trades.values()
                if self.config.SYMBOL_GROUPS.get(t['symbol'], "other") == group
            )
            if group_count >= self.config.MAX_TRADES_PER_GROUP:
                return False, f"سقف گروه {group}"
        return True, ""


# ==================== ژورنال ====================
class TradeJournal:
    def __init__(self, path: str = "trade_history_best.json"):
        self.path = path
        self.records: List[Dict] = self._load()

    def _load(self) -> List[Dict]:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.records, f, indent=2)
        except Exception as e:
            logger.error(f"خطا در ذخیره ژورنال: {e}")

    def record(self, symbol: str, side: str, reason: str, pnl_usdt: float, pnl_pct: float, r_multiple: float, closed_pct: float):
        self.records.append({
            "date": date_cls.today().isoformat(),
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "side": side,
            "reason": reason,
            "pnl_usdt": round(pnl_usdt, 2),
            "pnl_pct": round(pnl_pct, 3),
            "r_multiple": round(r_multiple, 2),
            "closed_pct": closed_pct
        })
        self._save()

    def get_today_realized_pnl_usdt(self) -> float:
        today = date_cls.today().isoformat()
        return sum(r["pnl_usdt"] for r in self.records if r["date"] == today)

    def get_side_performance(self, symbol: str, side: str, lookback: int = 15) -> Dict:
        side_records = [r for r in self.records if r["symbol"] == symbol and r["side"] == side]
        if not side_records:
            return {"count": 0, "win_rate": None, "avg_r": None, "total_pnl_usdt": 0.0}
        recent = side_records[-lookback:]
        wins = [r for r in recent if r["pnl_usdt"] > 0]
        return {
            "count": len(recent),
            "win_rate": round((len(wins) / len(recent)) * 100, 1),
            "avg_r": round(sum(r["r_multiple"] for r in recent) / len(recent), 2),
            "total_pnl_usdt": round(sum(r["pnl_usdt"] for r in recent), 2)
        }

    def get_side_stats_for_date(self, for_date: str) -> Dict[str, Dict]:
        day_records = [r for r in self.records if r["date"] == for_date]
        result = {}
        for side in ["BUY", "SELL"]:
            side_recs = [r for r in day_records if r["side"] == side]
            if not side_recs:
                result[side] = {"count": 0, "win_rate": 0.0, "avg_r": 0.0, "total_pnl": 0.0}
                continue
            wins = [r for r in side_recs if r["pnl_usdt"] > 0]
            result[side] = {
                "count": len(side_recs),
                "win_rate": round((len(wins) / len(side_recs)) * 100, 1),
                "avg_r": round(sum(r["r_multiple"] for r in side_recs) / len(side_recs), 2),
                "total_pnl": round(sum(r["pnl_usdt"] for r in side_recs), 2)
            }
        return result

    def build_daily_summary(self, for_date: str) -> Optional[str]:
        day_records = [r for r in self.records if r["date"] == for_date]
        if not day_records:
            return None
        total_pnl = sum(r["pnl_usdt"] for r in day_records)
        wins = [r for r in day_records if r["pnl_usdt"] > 0]
        losses = [r for r in day_records if r["pnl_usdt"] <= 0]
        win_rate = (len(wins) / len(day_records)) * 100 if day_records else 0
        avg_r = sum(r["r_multiple"] for r in day_records) / len(day_records) if day_records else 0
        side_stats = self.get_side_stats_for_date(for_date)
        buy_s = side_stats["BUY"]
        return f"""
📊 **گزارش روزانه ({for_date}) - BEST Config**

🔢 رخدادها: {len(day_records)}
✅ برد: {len(wins)} | ❌ باخت: {len(losses)}
🎯 نرخ برد: {win_rate:.1f}%
📈 PnL: {total_pnl:+.2f} USDT
📐 میانگین R: {avg_r:+.2f}R

🟢 **BUY:** {buy_s['count']} | WR {buy_s['win_rate']:.1f}% | R {buy_s['avg_r']:+.2f} | PnL {buy_s['total_pnl']:+.2f}
"""


# ==================== معامله مجازی ====================
class PaperTrader:
    def __init__(self, config: Config, telegram_sender, ai_optimizer: AIParameterOptimizer, journal: TradeJournal,
                 portfolio_state: Optional["PortfolioStateManager"] = None):
        self.config = config
        self.telegram = telegram_sender
        self.ai_optimizer = ai_optimizer
        self.journal = journal
        self.portfolio_state = portfolio_state
        self.file_path = "paper_trades_best.json"
        self.active_trades = self._load_trades()
        self.lock = threading.Lock()

    def _load_trades(self) -> Dict:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_trades(self):
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.active_trades, f, indent=4)
        except Exception as e:
            logger.error(f"خطا در ذخیره معاملات مجازی: {e}")

    def snapshot_active_trades(self) -> Dict:
        with self.lock:
            return dict(self.active_trades)

    def open_virtual_trade(self, symbol: str, side: str, entry_price: float, tp1: float, tp2: float, tp3: float,
                            sl: float, qty: float, atr_at_entry: float, spread_pct_at_entry: Optional[float] = None):
        trade_id = f"{symbol}_{int(time.time())}"
        with self.lock:
            self.active_trades[trade_id] = {
                "symbol": symbol,
                "side": side,
                "entry": entry_price,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "sl": sl,
                "original_sl": sl,
                "qty": qty,
                "atr_at_entry": atr_at_entry,
                "spread_pct_at_entry": spread_pct_at_entry,
                "remaining_pct": 100,
                "tp1_hit": False,
                "tp2_hit": False,
                "highest_since_entry": entry_price,
                "lowest_since_entry": entry_price,
                "open_time": datetime.now().strftime('%Y-%m-%d %H:%M')
            }
            self._save_trades()

    def _close_partial(self, trade: Dict, exit_price: float, reason: str, closed_pct: float, register_result: bool = True):
        side = trade['side']
        entry = trade['entry']
        price_diff = (exit_price - entry) if side == "BUY" else (entry - exit_price)
        spread_estimate_pct = trade.get('spread_pct_at_entry')
        if spread_estimate_pct is None:
            spread_estimate_pct = self.config.DEFAULT_SPREAD_PCT_FALLBACK
        cost_pct_per_side = (self.config.EXCHANGE_TAKER_FEE_PCT + (spread_estimate_pct / 2)) / 100
        execution_cost_per_unit = (entry + exit_price) * cost_pct_per_side
        price_diff -= execution_cost_per_unit
        risk_per_unit = abs(entry - trade['original_sl'])
        r_multiple = (price_diff / risk_per_unit) if risk_per_unit > 0 else 0.0
        qty_closed = trade['qty'] * (closed_pct / 100)
        pnl_usdt = price_diff * qty_closed
        pnl_pct = (price_diff / entry) * 100
        if register_result:
            if price_diff > 0:
                self.ai_optimizer.register_win(trade['symbol'])
            else:
                self.ai_optimizer.register_loss(trade['symbol'])
        self.journal.record(trade['symbol'], side, reason, pnl_usdt, pnl_pct, r_multiple, closed_pct)
        if self.portfolio_state is not None:
            try:
                self.portfolio_state.recompute()
            except Exception as e:
                logger.error(f"خطا در بازمحاسبه پرتفوی: {e}")
        emoji = "✅" if pnl_usdt > 0 else ("⚪" if pnl_usdt == 0 else "❌")
        msg = f"""
{emoji} **گزارش معامله مجازی (BEST)**

📌 **ارز:** {trade['symbol']} ({side})
📎 **علت:** {reason}
📈 **PnL (بعد از کارمزد/اسپرد):** {pnl_pct:+.2f}% ({pnl_usdt:+.2f} USDT)
📐 **R:** {r_multiple:+.2f}R
📦 **درصد بسته:** {closed_pct}%
"""
        self.telegram.send_personal_message(msg)

    def update_and_check_trades(self, data_layer: DataLayer):
        with self.lock:
            if not self.active_trades:
                return
            for trade_id, trade in list(self.active_trades.items()):
                try:
                    df = data_layer.fetch_ohlcv(trade['symbol'], timeframe="1m", limit=5)
                    if df.empty:
                        continue
                    latest_high = float(df['high'].max())
                    latest_low = float(df['low'].min())
                    side = trade['side']
                    trade['highest_since_entry'] = max(trade['highest_since_entry'], latest_high)
                    trade['lowest_since_entry'] = min(trade['lowest_since_entry'], latest_low)

                    hit_sl = (side == "BUY" and latest_low <= trade['sl']) or (side == "SELL" and latest_high >= trade['sl'])
                    if hit_sl:
                        is_breakeven = trade['tp1_hit'] and abs(trade['sl'] - trade['entry']) / trade['entry'] < 0.001
                        reason = "Break-even" if is_breakeven else "حد ضرر"
                        self._close_partial(trade, trade['sl'], reason, trade['remaining_pct'], register_result=not is_breakeven)
                        del self.active_trades[trade_id]
                        continue

                    hit_tp3 = (side == "BUY" and latest_high >= trade['tp3']) or (side == "SELL" and latest_low <= trade['tp3'])
                    if hit_tp3:
                        self._close_partial(trade, trade['tp3'], "TP3 (خروج کامل)", trade['remaining_pct'])
                        del self.active_trades[trade_id]
                        continue

                    hit_tp2 = (side == "BUY" and latest_high >= trade['tp2']) or (side == "SELL" and latest_low <= trade['tp2'])
                    if hit_tp2 and not trade['tp2_hit']:
                        self._close_partial(trade, trade['tp2'], f"TP2 (بستن {self.config.TP2_CLOSE_PCT}٪)", self.config.TP2_CLOSE_PCT)
                        trade['remaining_pct'] -= self.config.TP2_CLOSE_PCT
                        trade['tp2_hit'] = True

                    hit_tp1 = (side == "BUY" and latest_high >= trade['tp1']) or (side == "SELL" and latest_low <= trade['tp1'])
                    if hit_tp1 and not trade['tp1_hit']:
                        self._close_partial(trade, trade['tp1'], f"TP1 (بستن {self.config.TP1_CLOSE_PCT}٪ + SL به BE)", self.config.TP1_CLOSE_PCT)
                        trade['remaining_pct'] -= self.config.TP1_CLOSE_PCT
                        trade['tp1_hit'] = True
                        trade['sl'] = trade['entry']

                    if trade['tp1_hit'] and trade['remaining_pct'] > 0:
                        p = self.ai_optimizer.get_params(trade['symbol'])
                        atr = trade['atr_at_entry']
                        if side == "BUY":
                            new_trail = trade['highest_since_entry'] - (p["trailing_mult"] * atr)
                            trade['sl'] = max(trade['sl'], round(new_trail, 6))
                        else:
                            new_trail = trade['lowest_since_entry'] + (p["trailing_mult"] * atr)
                            trade['sl'] = min(trade['sl'], round(new_trail, 6))
                    self._save_trades()
                except Exception as e:
                    logger.error(f"خطا در بررسی معامله {trade_id}: {e}")


# ==================== تلگرام ====================
class TelegramSender:
    def __init__(self, config: Config, ai_optimizer: AIParameterOptimizer, risk_manager: RiskManager):
        self.config = config
        self.ai_optimizer = ai_optimizer
        self.risk_manager = risk_manager
        self.base_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

    def test_connection(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/getMe", timeout=10)
            if resp.status_code == 200 and resp.json().get("ok"):
                bot_name = resp.json()["result"].get("username", "?")
                logger.info(f"✅ تلگرام OK: @{bot_name}")
                return True
            logger.error(f"❌ توکن تلگرام نامعتبر")
            return False
        except Exception as e:
            logger.error(f"❌ خطا در تلگرام: {e}")
            return False

    def send_system_status(self, text: str):
        try:
            r = requests.post(f"{self.base_url}/sendMessage",
                               json={"chat_id": self.config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
                               timeout=10)
            if r.status_code != 200:
                logger.error(f"ارسال وضعیت ناموفق: {r.status_code}")
        except Exception as e:
            logger.error(f"خطای تلگرام: {e}")

    def send_personal_message(self, text: str):
        target_id = self.config.PERSONAL_CHAT_ID or self.config.TELEGRAM_CHAT_ID
        try:
            r = requests.post(f"{self.base_url}/sendMessage",
                               json={"chat_id": target_id, "text": text, "parse_mode": "Markdown"},
                               timeout=10)
            if r.status_code != 200:
                logger.error(f"ارسال شخصی ناموفق: {r.status_code}")
        except Exception as e:
            logger.error(f"خطای تلگرام شخصی: {e}")

    def send_error_alert(self, text: str):
        self.send_personal_message(f"🚨 **هشدار** 🚨\n\n{text}")

    def send_signal(self, symbol: str, side: str, latest: pd.Series, trend_4h: str, timeframe: str,
                     judge_reason: str = "", judge_confidence: int = 0, macro_context: Optional[dict] = None,
                     portfolio_size_mult: float = 1.0, portfolio_state: str = "NORMAL", path: str = "TREND") -> Optional[Dict]:
        emoji = "🟢"
        direction = "LONG"
        price = float(latest['close'])
        atr = float(latest['atr']) if not pd.isna(latest['atr']) else price * 0.01
        p = self.ai_optimizer.get_params(symbol)

        stop_loss = min(float(latest['support']), price - (p["sl_atr_mult"] * atr))
        stop_loss = max(stop_loss, price * 0.975)
        risk = price - stop_loss
        if risk <= 0:
            return None
        tp1 = round(price + (p["tp1_mult"] * risk), 4)
        tp2 = round(price + (p["tp2_mult"] * risk), 4)
        tp3 = round(price + (p["tp3_mult"] * risk), 4)
        stop_loss = round(stop_loss, 4)

        btc_volatility_pctl = (macro_context or {}).get("btc_volatility_pctl")
        stress_active = btc_volatility_pctl is not None and btc_volatility_pctl >= p["stress_pctl_threshold"]
        size_mult = p["stress_size_mult"] if stress_active else 1.0
        size_mult = max(0.18, size_mult * portfolio_size_mult)

        qty = self.risk_manager.calculate_position_size(price, stop_loss, size_mult=size_mult)
        notional = qty * price
        rr_ratio = p["tp1_mult"]

        stress_note = ""
        if stress_active:
            stress_note = f"\n⚠️ استرس بازار (نوسان BTC پرسنتایل {btc_volatility_pctl:.0f}) - حجم کاهش یافت\n"
        portfolio_note = ""
        if portfolio_state != "NORMAL":
            label = "🟡 محتاط" if portfolio_state == "CAUTION" else "🔴 تدافعی"
            portfolio_note = f"\n🧭 حالت پرتفوی: {label}\n"
        path_note = f"\n📍 مسیر: {path}\n"

        message = f"""
{emoji} **SPOT LONG SIGNAL - BEST Config**

📍 **Symbol:** {symbol}
⏱ **TF:** {timeframe} | Trend 4H: {trend_4h}
{path_note}
💵 **Entry:** {price:,}
🎯 **Targets:**
  1️⃣ TP1 ({self.config.TP1_CLOSE_PCT}% + BE): {tp1:,}
  2️⃣ TP2 ({self.config.TP2_CLOSE_PCT}%): {tp2:,}
  3️⃣ TP3 (خروج کامل + تریل): {tp3:,}

🛑 **Stop-Loss:** {stop_loss:,}
⚖️ **R:R تا TP1:** 1:{rr_ratio:.2f}
{stress_note}{portfolio_note}
💰 **حجم (ریسک {self.config.RISK_PER_TRADE_PCT}% × {size_mult:.2f}):**
  {qty:.6f} | ارزش: {notional:,.2f} USDT

📊 RSI: {latest['rsi']:.1f}
🧠 AI ({judge_confidence}%): {judge_reason if judge_reason else '—'}
⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
        try:
            r = requests.post(f"{self.base_url}/sendMessage",
                               json={"chat_id": self.config.TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
                               timeout=10)
            if r.status_code != 200:
                logger.error(f"ارسال سیگنال ناموفق: {r.status_code}")
                return None
            logger.info(f"سیگنال LONG برای {symbol} ارسال شد")
            return {"price": price, "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": stop_loss, "qty": qty, "atr": atr}
        except Exception as e:
            logger.error(f"خطای تلگرام: {e}")
            return None


# ==================== سیستم اصلی ====================
class HybridTradingSystem:
    def __init__(self):
        self.config = Config()
        self.config.validate()
        self.data = DataLayer(self.config)
        self.analysis = AnalysisLayer(self.config)
        self.macro_data = MacroDataLayer()
        self.ai_optimizer = AIParameterOptimizer(self.config)
        self.signal_engine = SignalEngine(self.config, self.ai_optimizer, self.analysis)
        self.risk_manager = RiskManager(self.config)
        self.telegram = TelegramSender(self.config, self.ai_optimizer, self.risk_manager)
        self.journal = TradeJournal()
        self.portfolio_state = PortfolioStateManager(self.config, self.journal, self.telegram)
        self.paper_trader = PaperTrader(self.config, self.telegram, self.ai_optimizer, self.journal, self.portfolio_state)
        self.correlation_manager = CorrelationManager(self.config, self.data)
        self.ai_optimizer.on_quota_exhausted_callback = self._send_crash_alert
        self.running = True
        self.last_signal_time: Dict[str, datetime] = {}
        self.last_summary_date: Optional[str] = date_cls.today().isoformat()
        self.consecutive_full_cycle_failures = 0
        self.data_outage_alert_sent = False

    def _send_crash_alert(self, text: str):
        try:
            self.telegram.send_error_alert(text)
        except Exception:
            logger.error("ارسال هشدار هم ناموفق بود.")

    def _start_trade_monitor_thread(self):
        def monitor_loop():
            while self.running:
                try:
                    self.paper_trader.update_and_check_trades(self.data)
                except Exception as e:
                    logger.error(f"خطا در مانیتور معاملات: {e}")
                time.sleep(self.config.TRADE_MONITOR_INTERVAL_SECONDS)
        threading.Thread(target=monitor_loop, daemon=True, name="TradeMonitor").start()
        logger.info(f"مانیتور معاملات فعال (هر {self.config.TRADE_MONITOR_INTERVAL_SECONDS}s)")

    def _build_macro_context(self) -> dict:
        context = {"fear_greed": None, "btc_trend_4h": None, "btc_structure": None, "btc_volatility_pctl": None,
                   "btc_rsi": None, "btc_macd_hist": None}
        try:
            context["fear_greed"] = self.macro_data.get_fear_greed_index()
        except Exception:
            pass
        try:
            # BTC را برای زمینه کلان نگه می‌داریم حتی اگر در لیست معاملات نباشد
            btc_df_4h = self.data.fetch_ohlcv("BTC/USDT", timeframe=self.config.TREND_TIMEFRAME, limit=self.config.TREND_WARMUP_CANDLES)
            btc_df_4h = self.analysis.calculate_indicators(btc_df_4h)
            context["btc_trend_4h"] = self.analysis.get_major_trend(btc_df_4h)
            btc_df_15m = self.data.fetch_ohlcv("BTC/USDT", timeframe=self.config.ENTRY_TIMEFRAME)
            btc_df_15m = self.analysis.calculate_indicators(btc_df_15m)
            context["btc_structure"] = self.analysis.market_structure(btc_df_15m)
            context["btc_volatility_pctl"] = self.analysis.atr_percentile(btc_df_15m)
            if not btc_df_15m.empty:
                btc_latest = btc_df_15m.iloc[-1]
                if not pd.isna(btc_latest.get('rsi', float('nan'))):
                    context["btc_rsi"] = float(btc_latest['rsi'])
                if not pd.isna(btc_latest.get('macd_hist', float('nan'))):
                    context["btc_macd_hist"] = float(btc_latest['macd_hist'])
        except Exception as e:
            logger.warning(f"خطا در زمینه BTC: {e}")
        return context

    def process_symbol(self, symbol: str, macro_context: Optional[dict] = None) -> bool:
        try:
            df_15m = self.data.fetch_ohlcv(symbol, timeframe=self.config.ENTRY_TIMEFRAME)
            df_15m = self.analysis.calculate_indicators(df_15m)
            if df_15m.empty:
                return False
            if self.ai_optimizer.should_optimize(symbol):
                self.ai_optimizer.optimize_symbol_parameters(symbol, df_15m, journal=self.journal)
            df_1h = self.data.fetch_ohlcv(symbol, timeframe=self.config.CONFIRM_TIMEFRAME)
            df_1h = self.analysis.calculate_indicators(df_1h)
            df_4h = self.data.fetch_ohlcv(symbol, timeframe=self.config.TREND_TIMEFRAME, limit=self.config.TREND_WARMUP_CANDLES)
            df_4h = self.analysis.calculate_indicators(df_4h)
            trend_4h = self.analysis.get_major_trend(df_4h)

            symbol_macro_context = dict(macro_context or {})
            symbol_macro_context["funding_rate"] = self.data.fetch_funding_rate(symbol)
            symbol_macro_context["spread_pct"] = self.data.fetch_spread_pct(symbol)
            portfolio_adjustments = self.portfolio_state.get_adjustments()

            rule_signal, diagnostics = self.signal_engine.get_rule_signal(
                symbol, df_15m, df_1h, trend_4h, symbol_macro_context, portfolio_adjustments
            )
            if not rule_signal:
                return True

            now = datetime.now()
            p = self.ai_optimizer.get_params(symbol)
            cooldown = p.get("cooldown_minutes", 45) * portfolio_adjustments.get("cooldown_mult", 1.0)
            if symbol in self.last_signal_time:
                if now - self.last_signal_time[symbol] < timedelta(minutes=cooldown):
                    return True

            active_trades_snapshot = self.paper_trader.snapshot_active_trades()
            if len(active_trades_snapshot) >= self.config.MAX_CONCURRENT_TRADES:
                return True
            can_open, reason = self.risk_manager.can_open_trade(
                symbol, active_trades_snapshot,
                correlation_manager=self.correlation_manager,
                btc_volatility_pctl=(macro_context or {}).get("btc_volatility_pctl")
            )
            if not can_open:
                logger.info(f"{symbol}: رد - {reason}")
                return True

            diagnostics["open_positions_count"] = len(active_trades_snapshot)
            diagnostics["today_realized_pnl_usdt"] = round(self.journal.get_today_realized_pnl_usdt(), 2)
            diagnostics["portfolio_rolling_stats"] = self.portfolio_state.last_stats

            judge = self.ai_optimizer.evaluate_trade_candidate(symbol, rule_signal, diagnostics)
            required_confidence = min(88, self.config.MIN_JUDGE_CONFIDENCE + portfolio_adjustments.get("confidence_adjustment", 0))
            if not judge["approve"] or judge["confidence"] < required_confidence:
                logger.info(f"{symbol}: رد AI (اطمینان {judge['confidence']}% < {required_confidence}) - {judge['reason']}")
                return True

            latest = df_15m.iloc[-1]
            trade_data = self.telegram.send_signal(
                symbol, rule_signal, latest, trend_4h, self.config.ENTRY_TIMEFRAME,
                judge_reason=judge["reason"], judge_confidence=judge["confidence"],
                macro_context=symbol_macro_context,
                portfolio_size_mult=portfolio_adjustments.get("size_mult", 1.0),
                portfolio_state=portfolio_adjustments.get("state", "NORMAL"),
                path=diagnostics.get("path", "TREND")
            )
            if trade_data:
                self.paper_trader.open_virtual_trade(
                    symbol=symbol,
                    side=rule_signal,
                    entry_price=trade_data["price"],
                    tp1=trade_data["tp1"],
                    tp2=trade_data["tp2"],
                    tp3=trade_data["tp3"],
                    sl=trade_data["sl"],
                    qty=trade_data["qty"],
                    atr_at_entry=trade_data["atr"],
                    spread_pct_at_entry=symbol_macro_context.get("spread_pct")
                )
                self.last_signal_time[symbol] = now
            return True
        except Exception as e:
            logger.error(f"خطا در پردازش {symbol}: {e}")
            return False

    def _check_daily_rollover(self):
        today = date_cls.today().isoformat()
        if self.last_summary_date and today != self.last_summary_date:
            summary = self.journal.build_daily_summary(self.last_summary_date)
            if summary:
                summary += f"\n💳 **سهمیه Groq:** {self.ai_optimizer.budget.get_status_summary()}\n"
                self.telegram.send_system_status(summary)
            self.last_summary_date = today

    def run_once(self):
        logger.info("----- شروع آنالیز اسپات BEST -----")
        self._check_daily_rollover()
        self.portfolio_state.recompute()
        if self.correlation_manager.should_refresh():
            self.correlation_manager.refresh()
        macro_context = self._build_macro_context()
        if self.portfolio_state.should_refresh_ai_regime():
            if self.ai_optimizer.quota.can_optimize() and self.ai_optimizer.budget.can_consume("regime"):
                regime_result = self.ai_optimizer.assess_market_regime(macro_context, self.portfolio_state.get_stats())
                self.portfolio_state.set_ai_regime(regime_result)
        fetch_failures = 0
        for symbol in self.config.SYMBOLS:
            ok = self.process_symbol(symbol, macro_context)
            if not ok:
                fetch_failures += 1
            time.sleep(1.2)
        if fetch_failures == len(self.config.SYMBOLS):
            self.consecutive_full_cycle_failures += 1
        else:
            self.consecutive_full_cycle_failures = 0
            self.data_outage_alert_sent = False
        if self.consecutive_full_cycle_failures >= 2 and not self.data_outage_alert_sent:
            self._send_crash_alert("دریافت داده برای همه نمادها ناموفق - احتمالاً مشکل شبکه/صرافی.")
            self.data_outage_alert_sent = True

    def start(self):
        logger.info("بات اسپات BEST (Paper) فعال شد")
        if not self.telegram.test_connection():
            logger.error("اتصال تلگرام برقرار نشد!")
        start_message = f"""🛡 **Spot Long Bot BEST (Paper Trading) فعال شد**

بهترین پیکربندی بر اساس بک‌تست یک‌ساله:
• بازده تقریبی بک‌تست: **+۱۸۶٪** | MaxDD ≈ ۲۳.۷٪ | WR ≈ ۶۵٪
• نمادها: NEAR, PAXG, LTC, BNB, XRP, LINK
• دو مسیر ورود: روند قوی + پولبک
• ریسک پایه {self.config.RISK_PER_TRADE_PCT}% | حداکثر {self.config.MAX_CONCURRENT_TRADES} معامله همزمان
• خودتنظیمی پرتفوی NORMAL/CAUTION/DEFENSIVE

فقط معاملات مجازی (Paper).
"""
        self.telegram.send_system_status(start_message)
        self._start_trade_monitor_thread()
        while self.running:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"خطای چرخه اصلی: {e}")
                self._send_crash_alert(f"خطای چرخه اصلی:\n`{e}`")
            gc.collect()
            time.sleep(self.config.CHECK_INTERVAL)

    def stop(self):
        self.running = False
        logger.info("بات متوقف شد")


if __name__ == "__main__":
    bot = HybridTradingSystem()
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.stop()
