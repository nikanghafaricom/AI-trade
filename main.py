# ==============================================
# Hybrid Signal Bot - نسخه حرفه‌ای (Anti-Loss + Risk Management + Market Structure)
# اصول به‌کاررفته: ریسک ثابت درصدی، محدودیت اکسپوژر همبسته، کلید قطع ضرر روزانه،
# ساختار بازار، تایید چندتایم‌فریمی، فیلتر رژیم نوسان، تریلینگ استاپ واقعی، ژورنال معاملات
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
import pandas as pd
import ccxt
from dotenv import load_dotenv

load_dotenv()

# ==================== وب‌سرور استاندارد ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Anti-Loss Bot is alive and running at Peak Performance!")

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

# ==================== تنظیمات ====================
class Config:
    EXCHANGE_ID = "coinex"
    API_KEY = os.getenv("EXCHANGE_API_KEY", "")
    SECRET = os.getenv("EXCHANGE_SECRET", "")
    PASSWORD = os.getenv("EXCHANGE_PASSWORD", "")

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    PERSONAL_CHAT_ID = os.getenv("PERSONAL_CHAT_ID")

    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

    SYMBOLS = [
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "BNB/USDT",
        "XRP/USDT",
        "AVAX/USDT",
        "NEAR/USDT",
        "ADA/USDT",
        "DOGE/USDT",
        "LINK/USDT",
        # --- دو ارز اضافه‌شده ---
        # PAXG (توکن پشتوانه‌ی طلا): تنها دارایی که واقعاً همبستگی پایین‌تری با کل بازار
        # کریپتو داره و در ریزش‌های ریسک‌آف کلی بازار معمولاً بهتر از آلت‌کوین‌ها رفتار می‌کنه.
        # نوسانش خیلی کمتره، برای همین پارامترهای مخصوص خودش پایین‌تر تنظیم شده (به بخش
        # AIParameterOptimizer نگاه کن).
        "PAXG/USDT",
        # LTC: نقدشوندگی بالا و تاریخچه‌ی طولانی، به‌عنوان یک "میجر" مکمل BTC/ETH/BNB اضافه شد
        # تا فرصت‌های بیشتری برای سیگنال long در گروه میجرها فراهم بشه.
        "LTC/USDT",
    ]

    # گروه‌بندی همبستگی - برای جلوگیری از باز کردن چند معامله‌ی عملاً یکسان هم‌زمان
    SYMBOL_GROUPS = {
        "BTC/USDT": "majors",
        "ETH/USDT": "majors",
        "BNB/USDT": "majors",
        "LTC/USDT": "majors",
        "SOL/USDT": "L1_alt",
        "AVAX/USDT": "L1_alt",
        "NEAR/USDT": "L1_alt",
        "ADA/USDT": "L1_alt",
        "XRP/USDT": "payments",
        "DOGE/USDT": "meme",
        "LINK/USDT": "oracle",
        # گروه جداگانه چون رفتار PAXG (طلا) از بقیه‌ی گروه‌ها متفاوته و نباید با اون‌ها
        # در یک سقف اکسپوژر مشترک محاسبه بشه
        "PAXG/USDT": "defensive_gold",
    }

    ENTRY_TIMEFRAME = "15m"
    CONFIRM_TIMEFRAME = "1h"
    TREND_TIMEFRAME = "4h"
    CHECK_INTERVAL = 300

    # حداقل امتیاز لازم برای صدور سیگنال (سقف تئوریک امتیاز ~11.75)
    MIN_SIGNAL_SCORE = 7.5
    # حداکثر پرسنتایل ATR مجاز - بالاتر از این یعنی کندل پارابولیک/خبری، رد می‌شه
    ATR_PERCENTILE_MAX = 92

    # ---- مدیریت سرمایه (Fixed Fractional Risk - روش استاندارد تریدرهای حرفه‌ای) ----
    VIRTUAL_CAPITAL_USDT = float(os.getenv("VIRTUAL_CAPITAL_USDT", 10000))
    RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", 1.5))       # ریسک هر معامله از کل سرمایه
    MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES", 4))
    MAX_TRADES_PER_GROUP = int(os.getenv("MAX_TRADES_PER_GROUP", 2))
    MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", 3.0))       # کلید قطع ضرر روزانه

    # حداقل اطمینان لایه‌ی قضاوت هوشمند (discretionary AI judge) برای تایید نهایی معامله
    MIN_JUDGE_CONFIDENCE = int(os.getenv("MIN_JUDGE_CONFIDENCE", 55))

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
        logging.FileHandler("trading_signals.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== لایه داده ====================
class DataLayer:
    def __init__(self, config: Config):
        self.config = config
        exchange_class = getattr(ccxt, config.EXCHANGE_ID)
        self.exchange = exchange_class({
            'apiKey': config.API_KEY,
            'secret': config.SECRET,
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 150) -> pd.DataFrame:
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception as e:
            logger.error(f"خطا در دریافت داده {symbol} در تایم‌فریم {timeframe}: {e}")
            return pd.DataFrame()

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """
        نرخ فاندینگ بازار پرپچوال همون ارز - نشون‌دهنده‌ی ازدحام معامله‌گران لانگ/شورت.
        کاملاً best-effort: چون این نمونه‌ی exchange روی حالت spot تنظیم شده، ممکنه این
        صرافی/نسخه‌ی ccxt از fetch_funding_rate برای این سیمبل پشتیبانی نکنه - هر خطایی
        بی‌صدا نادیده گرفته می‌شه و None برمی‌گرده (یعنی هیچ تاثیری روی امتیاز سیگنال نداره).
        """
        try:
            funding_symbol = symbol.replace("/USDT", "/USDT:USDT")
            data = self.exchange.fetch_funding_rate(funding_symbol)
            rate = data.get("fundingRate") if data else None
            return float(rate) if rate is not None else None
        except Exception:
            return None

    def fetch_spread_pct(self, symbol: str) -> Optional[float]:
        """درصد اسپرد بید/اسک لحظه‌ای - برای شناسایی نقدینگی غیرعادی نازک. best-effort."""
        try:
            ob = self.exchange.fetch_order_book(symbol, limit=5)
            best_bid = ob['bids'][0][0] if ob.get('bids') else None
            best_ask = ob['asks'][0][0] if ob.get('asks') else None
            if not best_bid or not best_ask:
                return None
            mid = (best_bid + best_ask) / 2
            if mid <= 0:
                return None
            return float((best_ask - best_bid) / mid * 100)
        except Exception:
            return None

# ==================== منابع داده‌ی کلان/فرابازاری (مستقل از هر ارز خاص) ====================
class MacroDataLayer:
    """
    داده‌ی سنتیمنت و رژیم کلی بازار که روی جهت‌گیری کلی همه‌ی ارزها اثر می‌ذاره، نه فقط
    یکی. طوری طراحی شده که در بدترین حالت (قطعی اینترنت، خطای API) کاملاً بی‌خطر
    fallback کنه: هیچ‌وقت باعث توقف بات یا رد نامعتبر یه سیگنال نمی‌شه، فقط اون بخش از
    تعدیل امتیاز غیرفعال می‌مونه.
    """
    def __init__(self):
        self._fng_value: Optional[int] = None
        self._fng_last_fetch: float = 0.0
        self._fng_cache_seconds = 3600  # شاخص ترس‌وطمع روزانه‌ست؛ هر ۱ ساعت رفرش کافیه

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
            logger.warning(f"دریافت شاخص ترس‌وطمع بازار ناموفق بود (نادیده گرفته می‌شه): {e}")
        return self._fng_value

# ==================== لایه تحلیل، ساختار بازار و رژیم نوسان ====================
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
        df['atr'] = tr.rolling(window=14).mean()

        df['vol_sma'] = df['volume'].rolling(window=20).mean()
        df['support'] = df['low'].rolling(window=15).min()
        df['resistance'] = df['high'].rolling(window=15).max()

        return df

    def is_market_tradable(self, df_15m: pd.DataFrame) -> bool:
        if df_15m.empty or len(df_15m) < 30:
            return False
        latest = df_15m.iloc[-1]
        if pd.isna(latest['volume']) or pd.isna(latest['vol_sma']) or latest['vol_sma'] == 0:
            return False
        volume_ratio = latest['volume'] / latest['vol_sma']
        if volume_ratio < 0.6:
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
        """تایید چندتایم‌فریمی: تایم‌فریم ۱ ساعته باید هم‌جهت با سیگنال ۱۵ دقیقه‌ای باشه"""
        if df_1h.empty or len(df_1h) < 60:
            return False
        latest = df_1h.iloc[-1]
        if pd.isna(latest.get('ema_fast')) or pd.isna(latest.get('ema_slow')):
            return False
        if side == "BUY":
            return bool(latest['ema_fast'] > latest['ema_slow'])
        return bool(latest['ema_fast'] < latest['ema_slow'])

    def market_structure(self, df: pd.DataFrame, lookback: int = 40) -> str:
        """تشخیص ساختار بازار با فرکتال ۵کندلی: HH+HL = صعودی، LH+LL = نزولی"""
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
        """چند درصد کندل‌های اخیر نوسان کمتری از کندل فعلی داشتن - برای تشخیص جهش‌های پارابولیک/خبری"""
        if df.empty or 'atr' not in df or len(df) < 30:
            return 50.0
        recent = df['atr'].tail(window).dropna()
        if recent.empty or pd.isna(df['atr'].iloc[-1]):
            return 50.0
        current = df['atr'].iloc[-1]
        return float((recent < current).mean() * 100)

# ==================== هوش مصنوعی پیشرفته اختصاصی و ضد ضرر ====================
class AIParameterOptimizer:
    def __init__(self, config):
        self.config = config
        self.groq_api_key = config.GROQ_API_KEY
        self.groq_endpoint = "https://api.groq.com/openai/"

        self.blacklist: Dict[str, datetime] = {}

        # سقف‌های محافظه‌کارانه برای retry روی خطای ۴۲۹ (rate limit)
        self.MAX_RETRIES_429 = 2
        self.MAX_BACKOFF_SECONDS = 8

        # محدودکننده‌ی نرخ دقیق: پلن رایگان Groq حدود ۳۰ درخواست در دقیقه می‌ده.
        # هدف رو روی ۲۵ درخواست در دقیقه می‌ذاریم (کمی زیر سقف، برای حاشیه‌ی امن)
        # و بین *هر دو* فراخوانی Groq (چه بهینه‌سازی پارامتر، چه لایه‌ی قضاوت) این
        # فاصله رو رعایت می‌کنیم. این کار مستقل از تعداد سیگنال‌ها یا تایمینگ حلقه‌ی
        # اصلی، تضمین می‌کنه که هیچ‌وقت به ۴۲۹ برنمی‌خوریم - نه اینکه صرفاً حدس بزنیم.
        self.GROQ_TARGET_RPM = 25
        self.groq_min_interval_seconds = 60.0 / self.GROQ_TARGET_RPM
        self._last_groq_call_ts = 0.0

        # پارامترهای پیش‌فرض مشترک (نقطه‌ی شروع). موتور AI هر ۶ ساعت طبق همون منطق قبلی
        # (optimize_symbol_parameters) این‌ها رو به‌روزرسانی می‌کنه - این بخش فقط نقطه‌ی
        # شروعِ هر ارز رو مشخص می‌کنه، نه فرمول امتیازدهی یا آستانه‌ها را.
        default_params = {
            "rsi_buy_min": 42,
            "rsi_buy_max_range_start": 48,
            "rsi_buy_max_range_end": 65,
            "rsi_sell_max": 58,
            "rsi_sell_min_range_start": 35,
            "rsi_sell_min_range_end": 52,
            "volume_mult": 1.0,
            "atr_min_filter": 0.0015,
            "cooldown_minutes": 90,
            "sl_atr_mult": 1.5,
            "tp1_mult": 1.5,
            "tp2_mult": 2.5,
            "tp3_mult": 4.0,
            "trailing_mult": 1.0
        }

        # تنظیم اولیه‌ی مختص هر ارز: فقط نقطه‌ی شروع رو بر اساس شخصیت/نوسان طبیعی هر دارایی
        # جابه‌جا می‌کنه (مثلاً PAXG که طلاست خیلی کم‌نوسان‌تر از DOGE هست) - نه شرط ورود
        # جدید و نه سخت‌گیری اضافه. همه‌ی مقادیر از فیلتر validate_and_clamp_params رد
        # می‌شن، پس در همون محدوده‌ی امن قبلی باقی می‌مونن.
        SYMBOL_PARAM_OVERRIDES = {
            "BTC/USDT":  {"atr_min_filter": 0.0010, "sl_atr_mult": 1.3},
            "ETH/USDT":  {"atr_min_filter": 0.0012, "sl_atr_mult": 1.4},
            "BNB/USDT":  {"atr_min_filter": 0.0012, "sl_atr_mult": 1.4},
            "LTC/USDT":  {"atr_min_filter": 0.0013, "sl_atr_mult": 1.4},
            "SOL/USDT":  {"atr_min_filter": 0.0018, "sl_atr_mult": 1.7, "rsi_buy_max_range_end": 68},
            "AVAX/USDT": {"atr_min_filter": 0.0018, "sl_atr_mult": 1.7},
            "NEAR/USDT": {"atr_min_filter": 0.0018, "sl_atr_mult": 1.7},
            "ADA/USDT":  {"atr_min_filter": 0.0015},
            "XRP/USDT":  {"atr_min_filter": 0.0020, "cooldown_minutes": 110},
            "DOGE/USDT": {"atr_min_filter": 0.0025, "sl_atr_mult": 1.9, "cooldown_minutes": 120},
            "LINK/USDT": {"atr_min_filter": 0.0016, "sl_atr_mult": 1.6},
            # PAXG خیلی کم‌نوسان‌تره؛ فیلتر حداقل ATR و ضریب SL پایین‌تر می‌ذاریم تا
            # حرکات طبیعی (هرچند کوچیک) این دارایی هم بتونن سیگنال معتبر تولید کنن
            "PAXG/USDT": {"atr_min_filter": 0.0008, "sl_atr_mult": 1.2, "tp1_mult": 1.3, "rsi_buy_max_range_end": 62},
        }

        self.symbol_states = {}
        for sym in config.SYMBOLS:
            merged_params = {**default_params, **SYMBOL_PARAM_OVERRIDES.get(sym, {})}
            self.symbol_states[sym] = {
                "last_optimized_time": None,
                "consecutive_losses": 0,
                "params": self.validate_and_clamp_params(merged_params)
            }
        self.optimization_interval = timedelta(hours=6)

    def is_blacklisted(self, symbol: str) -> bool:
        if symbol in self.blacklist:
            if datetime.now() < self.blacklist[symbol]:
                return True
            else:
                del self.blacklist[symbol]
        return False

    def register_loss(self, symbol: str):
        state = self.symbol_states[symbol]
        state["consecutive_losses"] += 1
        penalty_hours = min(1.5 * state["consecutive_losses"], 6)
        self.blacklist[symbol] = datetime.now() + timedelta(hours=penalty_hours)
        logger.warning(f"سیستم ضد ضرر: ارز {symbol} به دلیل ضرر متوالی به مدت {penalty_hours:.1f} ساعت مسدود شد.")

    def register_win(self, symbol: str):
        state = self.symbol_states[symbol]
        state["consecutive_losses"] = 0
        if symbol in self.blacklist:
            del self.blacklist[symbol]

    def validate_and_clamp_params(self, new_params: dict) -> dict:
        clamped = {}
        clamped["rsi_buy_min"] = max(30, min(float(new_params.get("rsi_buy_min", 42)), 50))
        clamped["rsi_buy_max_range_start"] = max(40, min(float(new_params.get("rsi_buy_max_range_start", 48)), 55))
        clamped["rsi_buy_max_range_end"] = max(55, min(float(new_params.get("rsi_buy_max_range_end", 65)), 75))

        clamped["rsi_sell_max"] = max(50, min(float(new_params.get("rsi_sell_max", 58)), 70))
        clamped["rsi_sell_min_range_start"] = max(25, min(float(new_params.get("rsi_sell_min_range_start", 35)), 45))
        clamped["rsi_sell_min_range_end"] = max(40, min(float(new_params.get("rsi_sell_min_range_end", 52)), 60))

        clamped["volume_mult"] = max(0.7, min(float(new_params.get("volume_mult", 1.0)), 1.6))
        clamped["atr_min_filter"] = max(0.0008, min(float(new_params.get("atr_min_filter", 0.0015)), 0.004))
        clamped["cooldown_minutes"] = max(45, min(int(new_params.get("cooldown_minutes", 90)), 240))

        # tp1_mult پایینش ۱.۲ نگه داشته شده تا R:R هر معامله ساختاراً حداقل ۱.۲:۱ باشه
        clamped["sl_atr_mult"] = max(1.2, min(float(new_params.get("sl_atr_mult", 1.5)), 2.5))
        clamped["tp1_mult"] = max(1.2, min(float(new_params.get("tp1_mult", 1.5)), 3.0))
        clamped["tp2_mult"] = max(2.0, min(float(new_params.get("tp2_mult", 2.5)), 5.0))
        clamped["tp3_mult"] = max(3.0, min(float(new_params.get("tp3_mult", 4.0)), 8.0))
        clamped["trailing_mult"] = max(0.8, min(float(new_params.get("trailing_mult", 1.0)), 2.0))
        return clamped

    def _wait_for_groq_slot(self):
        """قبل از هر فراخوانی Groq صدا زده می‌شه؛ اگه از آخرین فراخوانی کمتر از حداقل
        فاصله‌ی مجاز گذشته باشه، دقیقاً همون مقدار باقی‌مونده رو صبر می‌کنه."""
        elapsed = time.time() - self._last_groq_call_ts
        remaining = self.groq_min_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _post_with_retry(self, url: str, payload: dict, headers: dict, timeout: int, label: str) -> Optional[requests.Response]:
        """
        یه wrapper سبک روی requests.post که دو کار می‌کنه:
        ۱. قبل از هر تلاش، فاصله‌ی زمانی امن نسبت به آخرین فراخوانی Groq رو رعایت می‌کنه
           (self._wait_for_groq_slot) تا اصلاً به ۴۲۹ نخوریم.
        ۲. اگه بازم ۴۲۹ گرفتیم (مثلاً به‌خاطر مصرف هم‌زمان از جای دیگه)، هدر Retry-After
           رو می‌خونه و دقیقاً همون مقدار صبر و تلاش مجدد می‌کنه (حداکثر self.MAX_RETRIES_429 بار).
        روی بقیه‌ی خطاها (404، 500، تایم‌اوت و ...) بدون تاخیر برمی‌گرده تا رفتار قبلی حفظ بشه.
        """
        for attempt in range(self.MAX_RETRIES_429 + 1):
            self._wait_for_groq_slot()
            try:
                response = requests.post(url, json=payload, headers=headers, timeout=timeout)
            finally:
                self._last_groq_call_ts = time.time()

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

            logger.warning(f"Groq API برای {label} پاسخ 429 داد؛ {wait_seconds:.1f} ثانیه صبر و تلاش مجدد ({attempt + 1}/{self.MAX_RETRIES_429})...")
            time.sleep(wait_seconds)

        return response

    def should_optimize(self, symbol: str) -> bool:
        state = self.symbol_states[symbol]
        if state["last_optimized_time"] is None:
            return True
        return datetime.now() - state["last_optimized_time"] >= self.optimization_interval

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

        # آمار جداگانه‌ی عملکرد اخیر BUY و SELL این ارز - صرفاً اطلاعاتیه، خودش هیچ
        # فیلتری اعمال نمی‌کنه؛ فقط به AI کمک می‌کنه بفهمه مشکل اخیر مال کدوم سمته
        side_perf_note = ""
        if journal is not None:
            buy_perf = journal.get_side_performance(symbol, "BUY")
            sell_perf = journal.get_side_performance(symbol, "SELL")
            market_metrics["recent_buy_performance"] = buy_perf
            market_metrics["recent_sell_performance"] = sell_perf
            side_perf_note = """
IMPORTANT - side-specific tuning guidance:
"recent_buy_performance" and "recent_sell_performance" show this symbol's last trades broken down by side (win_rate, avg_r, count; null/0 means not enough data yet - ignore in that case).
If BUY has recently underperformed while SELL has not (or vice versa), prefer adjusting the side-specific keys (rsi_buy_min, rsi_buy_max_range_start, rsi_buy_max_range_end for BUY; rsi_sell_max, rsi_sell_min_range_start, rsi_sell_min_range_end for SELL) rather than the shared risk keys (sl_atr_mult, tp1_mult, tp2_mult, tp3_mult, atr_min_filter, volume_mult, cooldown_minutes, trailing_mult), since those shared keys affect both sides and unnecessarily reducing them would also cut down the healthy side's signal frequency.
Never make changes so aggressive that they would effectively stop signals from being generated at all - stay within reasonable, moderate adjustments.
"""

        prompt = f"""
You are an advanced quantitative trading AI. First, analyze the following key market data and indicators for asset {symbol}:
{json.dumps(market_metrics, indent=2)}
{side_perf_note}
Based on these specific conditions, dynamically tune the trading parameters to adapt to the current market regime.
Keep risk management strict to prevent losses, but allow reasonable flexibility so the bot can capture valid opportunities within safe logical boundaries.
Return ONLY valid JSON with the exact same keys as these default parameters:
{json.dumps(state["params"], indent=2)}
No markdown formatting, no extra text.
"""

        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": "openai/gpt-oss-120b",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2
        }

        try:
            response = self._post_with_retry(f"{self.groq_endpoint}v1/chat/completions", payload, headers, timeout=25, label=symbol)
            if response.status_code == 200:
                res_data = response.json()
                content = res_data['choices'][0]['message']['content'].strip()

                if content.startswith("```"):
                    content = content.strip("`").replace("json\n", "").strip()

                raw_params = json.loads(content)
                state["params"] = self.validate_and_clamp_params(raw_params)
                state["last_optimized_time"] = datetime.now()
                logger.info(f"پارامترهای ضد ضرر و پویای {symbol} بر اساس داده‌های روز بروزرسانی شد.")
            else:
                logger.warning(f"Groq API برای {symbol} پاسخ {response.status_code} داد؛ پارامترهای قبلی حفظ شدن.")
        except Exception as e:
            logger.error(f"خطا در بهینه‌سازی هوش مصنوعی برای {symbol}: {e}")

    def get_params(self, symbol: str) -> dict:
        return self.symbol_states[symbol]["params"]

    def evaluate_trade_candidate(self, symbol: str, side: str, context: dict) -> Dict:
        """
        لایه‌ی قضاوت discretionary: کاری که یک تریدر باتجربه‌ی انسانی انجام می‌ده -
        روی سیگنالی که شرط‌های کمی/ثابت تاییدش کردن، با درک کلی و غیرقابل‌کدنویسی از
        شرایط لحظه‌ای بازار (خستگی حرکت، تناقض بین سیگنال‌ها، فضای کلی ریسک) تصمیم نهایی می‌گیره.
        این لایه فقط می‌تونه معامله رو رد کنه یا تایید کنه؛ هیچ‌وقت اندازه‌ی پوزیشن یا SL رو تغییر نمی‌ده
        (اون‌ها همیشه قطعی و بر پایه‌ی فرمول ریسک ثابت باقی می‌مونن).
        """
        default = {"approve": True, "confidence": 50, "reason": "بدون دسترسی به AI - تایید صرفاً بر پایه امتیاز کمی"}
        if not self.groq_api_key:
            return default

        prompt = f"""You are a veteran discretionary crypto trader with 15+ years of experience. You deeply understand that markets are not static: regimes shift, correlations break down, momentum exhausts, and no fixed rule set can fully capture that. You are reviewing a trade candidate that ALREADY passed a strict quantitative multi-factor scoring system (trend, RSI momentum, MACD, volume, market structure, multi-timeframe alignment, volatility regime).

Your only job now is the kind of contextual judgment an elite human trader adds on top of a systematic setup: given everything below, does the broader picture actually support taking this trade right now, or is there something about the current context (exhaustion, conflicting signals, thin/erratic volume, the symbol's recent losing streak, over-extension) that says skip it even though the numbers look fine?

Trade candidate:
Symbol: {symbol}
Side: {side}
Full context: {json.dumps(context, indent=2, ensure_ascii=False)}

Respond ONLY with valid JSON, no markdown, no extra text, in exactly this shape:
{{"approve": true or false, "confidence": integer 0-100, "reason": "one concise sentence in Persian explaining the judgment"}}
"""
        headers = {"Authorization": f"Bearer {self.groq_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": "openai/gpt-oss-120b",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3
        }
        try:
            response = self._post_with_retry(f"{self.groq_endpoint}v1/chat/completions", payload, headers, timeout=20, label=symbol)
            if response.status_code != 200:
                logger.warning(f"لایه‌ی قضاوت AI برای {symbol} پاسخ {response.status_code} داد؛ به تصمیم کمی اکتفا می‌شه.")
                return default
            content = response.json()['choices'][0]['message']['content'].strip()
            if content.startswith("```"):
                content = content.strip("`").replace("json\n", "").strip()
            result = json.loads(content)
            return {
                "approve": bool(result.get("approve", True)),
                "confidence": int(result.get("confidence", 50)),
                "reason": str(result.get("reason", ""))[:300]
            }
        except Exception as e:
            logger.error(f"خطا در لایه‌ی قضاوت AI برای {symbol}: {e}")
            return default

# ==================== موتور سیگنال امتیازی چندلایه ====================
class SignalEngine:
    def __init__(self, config: Config, ai_optimizer: AIParameterOptimizer, analysis: AnalysisLayer):
        self.config = config
        self.ai_optimizer = ai_optimizer
        self.analysis = analysis

    def _score_buy(self, latest, prev, p) -> float:
        score = 0.0
        if latest['ema_fast'] > latest['ema_slow']:
            score += 2.0
        rsi_cross = latest['rsi'] > p["rsi_buy_min"] and prev['rsi'] <= p["rsi_buy_min"]
        rsi_zone = p["rsi_buy_max_range_start"] <= latest['rsi'] <= p["rsi_buy_max_range_end"] and latest['rsi'] > prev['rsi']
        if rsi_cross:
            score += 2.5
        elif rsi_zone:
            score += 1.5
        if not pd.isna(latest.get('macd_hist', float('nan'))):
            if latest['macd_hist'] > 0:
                score += 1.0
            elif latest['macd_hist'] > prev.get('macd_hist', 0):
                score += 0.5
        vol_ratio = latest['volume'] / latest['vol_sma'] if latest['vol_sma'] else 0
        if vol_ratio >= p["volume_mult"]:
            score += 1.5
        elif vol_ratio >= p["volume_mult"] * 0.8:
            score += 0.75
        if latest['support'] > 0 and (latest['close'] - latest['support']) / latest['close'] < 0.02:
            score += 0.75
        return score

    def _score_sell(self, latest, prev, p) -> float:
        score = 0.0
        if latest['ema_fast'] < latest['ema_slow']:
            score += 2.0
        rsi_cross = latest['rsi'] < p["rsi_sell_max"] and prev['rsi'] >= p["rsi_sell_max"]
        rsi_zone = p["rsi_sell_min_range_start"] <= latest['rsi'] <= p["rsi_sell_min_range_end"] and latest['rsi'] < prev['rsi']
        if rsi_cross:
            score += 2.5
        elif rsi_zone:
            score += 1.5
        if not pd.isna(latest.get('macd_hist', float('nan'))):
            if latest['macd_hist'] < 0:
                score += 1.0
            elif latest['macd_hist'] < prev.get('macd_hist', 0):
                score += 0.5
        vol_ratio = latest['volume'] / latest['vol_sma'] if latest['vol_sma'] else 0
        if vol_ratio >= p["volume_mult"]:
            score += 1.5
        elif vol_ratio >= p["volume_mult"] * 0.8:
            score += 0.75
        if latest['resistance'] > 0 and (latest['resistance'] - latest['close']) / latest['close'] < 0.02:
            score += 0.75
        return score

    def get_rule_signal(self, symbol: str, df_15m: pd.DataFrame, df_1h: pd.DataFrame, trend_4h: str,
                         macro_context: Optional[dict] = None) -> Tuple[Optional[str], dict]:
        if df_15m.empty or len(df_15m) < 30:
            return None, {}
        if self.ai_optimizer.is_blacklisted(symbol):
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
        fng = macro_context.get("fear_greed")
        btc_trend_4h = macro_context.get("btc_trend_4h")
        btc_structure = macro_context.get("btc_structure")
        funding_rate = macro_context.get("funding_rate")
        spread_pct = macro_context.get("spread_pct")

        # فیلتر اجرایی (نه امتیازی): اسپرد بید/اسک خیلی گشاد یعنی نقدینگی لحظه‌ای غیرعادیه
        # و قیمت واقعی پرشده می‌تونه به‌شدت با قیمت تحلیل‌شده فرق کنه. آستانه عمداً خیلی
        # بازه (۰.۸٪) که فقط شرایط واقعاً غیرعادی رو می‌گیره، نه نوسان معمولی بازار.
        if spread_pct is not None and spread_pct > 0.8:
            logger.info(f"{symbol}: اسپرد لحظه‌ای غیرعادی ({spread_pct:.2f}%) - سیگنال رد شد")
            return None, {}

        # فیلتر رژیم نوسان: از کندل‌های پارابولیک/جهش خبری عبور می‌کنیم
        pctl = self.analysis.atr_percentile(df_15m)
        if pctl > self.config.ATR_PERCENTILE_MAX:
            logger.info(f"{symbol}: نوسان غیرعادی (پرسنتایل {pctl:.0f}) - رد شد")
            return None, {}

        structure = self.analysis.market_structure(df_15m)

        buy_score = self._score_buy(latest, prev, p) if trend_4h in ["BULLISH", "NEUTRAL"] else 0.0
        sell_score = self._score_sell(latest, prev, p) if trend_4h in ["BEARISH", "NEUTRAL"] else 0.0

        if trend_4h == "BULLISH":
            buy_score += 1.0
        elif trend_4h == "BEARISH":
            sell_score += 1.0

        if structure == "BULLISH":
            buy_score += 1.5
        elif structure == "BEARISH":
            sell_score += 1.5

        if buy_score > 0 and self.analysis.is_mtf_aligned(df_1h, "BUY"):
            buy_score += 1.5
        if sell_score > 0 and self.analysis.is_mtf_aligned(df_1h, "SELL"):
            sell_score += 1.5

        # تعدیل امتیاز بر اساس رژیم کلان بیت‌کوین (لیدر بازار). جمع‌جبریه، نه یه رد
        # یک‌طرفه: در ریزش هماهنگ کل بازار از BUY آلت‌کوین‌ها کم می‌کنه، در صعود هماهنگ
        # بهش اضافه می‌کنه، در حالت خنثی هیچ اثری نداره.
        if symbol != "BTC/USDT" and btc_trend_4h and btc_structure:
            if btc_trend_4h == "BEARISH" and btc_structure == "BEARISH":
                buy_score -= 1.0
                sell_score += 0.5
            elif btc_trend_4h == "BULLISH" and btc_structure == "BULLISH":
                buy_score += 1.0
                sell_score -= 0.5

        # تعدیل امتیاز بر اساس شاخص ترس‌وطمع بازار (سنتیمنت کلی)
        if fng is not None:
            if fng <= 20:
                buy_score += 0.5
            elif fng >= 80:
                buy_score -= 0.5

        # تعدیل امتیاز بر اساس نرخ فاندینگ (ازدحام معامله‌گران در بازار فیوچرز)
        if funding_rate is not None:
            if funding_rate > 0.0005:
                buy_score -= 0.5
            elif funding_rate < -0.0005:
                buy_score += 0.5

        threshold = self.config.MIN_SIGNAL_SCORE
        vol_ratio = latest['volume'] / latest['vol_sma'] if latest['vol_sma'] else 0
        diagnostics = {
            "structure": structure,
            "trend_4h": trend_4h,
            "rsi": round(float(latest['rsi']), 1),
            "macd_hist": round(float(latest['macd_hist']), 6) if not pd.isna(latest.get('macd_hist', float('nan'))) else None,
            "volume_vs_avg_ratio": round(float(vol_ratio), 2),
            "atr_percentile_100candles": round(pctl, 1),
            "mtf_1h_aligned": self.analysis.is_mtf_aligned(df_1h, "BUY" if buy_score >= sell_score else "SELL"),
            "consecutive_losses_this_symbol": self.ai_optimizer.symbol_states[symbol]["consecutive_losses"],
            "fear_greed_index": fng,
            "btc_macro_trend_4h": btc_trend_4h,
            "btc_macro_structure": btc_structure,
            "funding_rate": funding_rate,
            "spread_pct": round(spread_pct, 3) if spread_pct is not None else None,
        }
        if buy_score >= threshold and buy_score > sell_score:
            diagnostics["quant_score"] = round(buy_score, 2)
            logger.info(f"{symbol}: امتیاز خرید {buy_score:.2f} (آستانه {threshold}) | ساختار: {structure}")
            return "BUY", diagnostics
        if sell_score >= threshold and sell_score > buy_score:
            diagnostics["quant_score"] = round(sell_score, 2)
            logger.info(f"{symbol}: امتیاز فروش {sell_score:.2f} (آستانه {threshold}) | ساختار: {structure}")
            return "SELL", diagnostics

        return None, {}

# ==================== مدیریت ریسک و سرمایه (روش تریدرهای حرفه‌ای) ====================
class RiskManager:
    def __init__(self, config: Config):
        self.config = config

    def calculate_position_size(self, entry: float, stop: float) -> float:
        """ریسک ثابت درصدی: حجم پوزیشن طوری محاسبه می‌شه که ضرر احتمالی دقیقاً برابر RISK_PER_TRADE_PCT سرمایه باشه"""
        risk_amount = self.config.VIRTUAL_CAPITAL_USDT * (self.config.RISK_PER_TRADE_PCT / 100)
        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0:
            return 0.0
        return risk_amount / risk_per_unit

    def can_open_trade(self, symbol: str, active_trades: Dict) -> Tuple[bool, str]:
        if len(active_trades) >= self.config.MAX_CONCURRENT_TRADES:
            return False, "به سقف تعداد معاملات هم‌زمان رسیدیم"
        group = self.config.SYMBOL_GROUPS.get(symbol, "other")
        group_count = sum(
            1 for t in active_trades.values()
            if self.config.SYMBOL_GROUPS.get(t['symbol'], "other") == group
        )
        if group_count >= self.config.MAX_TRADES_PER_GROUP:
            return False, f"به سقف اکسپوژر گروه {group} رسیدیم (ریسک همبستگی)"
        return True, ""

    def kill_switch_triggered(self, journal: "TradeJournal") -> bool:
        today_pnl = journal.get_today_realized_pnl_usdt()
        loss_limit = -(self.config.VIRTUAL_CAPITAL_USDT * self.config.MAX_DAILY_LOSS_PCT / 100)
        return today_pnl <= loss_limit

# ==================== ژورنال معاملات: آمار واقعی Win-rate و Expectancy ====================
class TradeJournal:
    def __init__(self, path: str = "trade_history.json"):
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
            logger.error(f"خطا در ذخیره ژورنال معاملات: {e}")

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
        """
        آمار برد/باخت جداگانه برای یک سمت مشخص (BUY یا SELL) روی یک ارز، محدود به
        N رخداد اخیر. این متد فقط اطلاعات برمی‌گردونه - هیچ سیگنالی رو رد یا تایید
        نمی‌کنه و هیچ فیلتری اعمال نمی‌کنه.
        """
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
        """آمار جداگانه‌ی BUY/SELL برای یک روز مشخص - برای گزارش روزانه"""
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
        buy_s, sell_s = side_stats["BUY"], side_stats["SELL"]

        return f"""
📊 **گزارش عملکرد روزانه ({for_date})**

🔢 تعداد رخدادهای بسته‌شده: {len(day_records)}
✅ برد: {len(wins)} | ❌ باخت: {len(losses)}
🎯 نرخ برد کل: {win_rate:.1f}%
📈 سود/زیان کل: {total_pnl:+.2f} USDT
📐 میانگین R به‌ازای هر رخداد: {avg_r:+.2f}R

🟢 **BUY (Long):** {buy_s['count']} رخداد | نرخ برد {buy_s['win_rate']:.1f}% | میانگین R {buy_s['avg_r']:+.2f} | PnL {buy_s['total_pnl']:+.2f} USDT
🔴 **SELL (Short):** {sell_s['count']} رخداد | نرخ برد {sell_s['win_rate']:.1f}% | میانگین R {sell_s['avg_r']:+.2f} | PnL {sell_s['total_pnl']:+.2f} USDT
"""

# ==================== ماژول معامله مجازی: حجم واقعی، پله‌ای، تریلینگ واقعی ====================
class PaperTrader:
    def __init__(self, config: Config, telegram_sender, ai_optimizer: AIParameterOptimizer, journal: TradeJournal):
        self.config = config
        self.telegram = telegram_sender
        self.ai_optimizer = ai_optimizer
        self.journal = journal
        self.file_path = "paper_trades.json"
        self.active_trades = self._load_trades()

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

    def open_virtual_trade(self, symbol: str, side: str, entry_price: float, tp1: float, tp2: float, tp3: float,
                            sl: float, qty: float, atr_at_entry: float):
        trade_id = f"{symbol}_{int(time.time())}"
        self.active_trades[trade_id] = {
            "symbol": symbol,
            "side": side,
            "entry": entry_price,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "sl": sl,
            "original_sl": sl,
            "qty": qty,
            "atr_at_entry": atr_at_entry,
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

        emoji = "✅" if pnl_usdt > 0 else ("⚪" if pnl_usdt == 0 else "❌")
        msg = f"""
{emoji} **گزارش معامله محافظت‌شده**

📌 **ارز:** {trade['symbol']} ({side})
📎 **علت:** {reason}
📈 **سود/زیان این مرحله:** {pnl_pct:+.2f}% ({pnl_usdt:+.2f} USDT)
📐 **R Multiple:** {r_multiple:+.2f}R
📦 **درصد بسته‌شده:** {closed_pct}%
"""
        self.telegram.send_personal_message(msg)

    def update_and_check_trades(self, data_layer: DataLayer):
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

                # حد ضرر
                hit_sl = (side == "BUY" and latest_low <= trade['sl']) or (side == "SELL" and latest_high >= trade['sl'])
                if hit_sl:
                    is_breakeven = trade['tp1_hit'] and abs(trade['sl'] - trade['entry']) / trade['entry'] < 0.001
                    reason = "بسته‌شدن با سود قفل‌شده (Break-even)" if is_breakeven else "برخورد به حد ضرر"
                    self._close_partial(trade, trade['sl'], reason, trade['remaining_pct'], register_result=not is_breakeven)
                    del self.active_trades[trade_id]
                    continue

                # TP3 - خروج کامل
                hit_tp3 = (side == "BUY" and latest_high >= trade['tp3']) or (side == "SELL" and latest_low <= trade['tp3'])
                if hit_tp3:
                    self._close_partial(trade, trade['tp3'], "برخورد به TP3 (خروج کامل)", trade['remaining_pct'])
                    del self.active_trades[trade_id]
                    continue

                # TP2 - بستن جزئی ۳۰٪
                hit_tp2 = (side == "BUY" and latest_high >= trade['tp2']) or (side == "SELL" and latest_low <= trade['tp2'])
                if hit_tp2 and not trade['tp2_hit']:
                    self._close_partial(trade, trade['tp2'], "برخورد به TP2 (بستن جزئی ۳۰٪)", 30)
                    trade['remaining_pct'] -= 30
                    trade['tp2_hit'] = True

                # TP1 - بستن جزئی ۵۰٪ + انتقال SL به سر به سر
                hit_tp1 = (side == "BUY" and latest_high >= trade['tp1']) or (side == "SELL" and latest_low <= trade['tp1'])
                if hit_tp1 and not trade['tp1_hit']:
                    self._close_partial(trade, trade['tp1'], "برخورد به TP1 (بستن جزئی ۵۰٪ + SL به سر به سر)", 50)
                    trade['remaining_pct'] -= 50
                    trade['tp1_hit'] = True
                    trade['sl'] = trade['entry']

                # تریلینگ استاپ واقعی (Chandelier Exit) - فقط بعد از قفل‌شدن سود در TP1 فعال می‌شه
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
                logger.error(f"خطا در بررسی معامله مجازی {trade_id}: {e}")

# ==================== ارسال تلگرام ====================
class TelegramSender:
    def __init__(self, config: Config, ai_optimizer: AIParameterOptimizer, risk_manager: RiskManager):
        self.config = config
        self.ai_optimizer = ai_optimizer
        self.risk_manager = risk_manager
        # !! باگ اصلی نسخه‌ی قبل اینجا بود: یک لینک مارک‌داون خام به‌جای URL واقعی
        self.base_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

    def test_connection(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/getMe", timeout=10)
            if resp.status_code == 200 and resp.json().get("ok"):
                bot_name = resp.json()["result"].get("username", "?")
                logger.info(f"✅ اتصال تلگرام تایید شد. ربات: @{bot_name}")
                return True
            logger.error(f"❌ توکن تلگرام معتبر نیست یا پاسخ غیرمنتظره: {resp.text}")
            return False
        except Exception as e:
            logger.error(f"❌ خطا در تست اتصال تلگرام: {e}")
            return False

    def send_system_status(self, text: str):
        try:
            r = requests.post(f"{self.base_url}/sendMessage",
                               json={"chat_id": self.config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
                               timeout=10)
            if r.status_code != 200:
                logger.error(f"ارسال پیام وضعیت ناموفق بود: {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"خطای ارسال پیام به تلگرام: {e}")

    def send_personal_message(self, text: str):
        target_id = self.config.PERSONAL_CHAT_ID or self.config.TELEGRAM_CHAT_ID
        try:
            r = requests.post(f"{self.base_url}/sendMessage",
                               json={"chat_id": target_id, "text": text, "parse_mode": "Markdown"},
                               timeout=10)
            if r.status_code != 200:
                logger.error(f"ارسال پیام شخصی ناموفق بود: {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"خطای ارسال پیام شخصی به تلگرام: {e}")

    def send_signal(self, symbol: str, side: str, latest: pd.Series, trend_4h: str, timeframe: str,
                     judge_reason: str = "", judge_confidence: int = 0) -> Optional[Dict]:
        emoji = "🟢" if side == "BUY" else "🔴"
        direction = "LONG" if side == "BUY" else "SHORT"
        price = float(latest['close'])
        atr = float(latest['atr']) if not pd.isna(latest['atr']) else price * 0.01

        p = self.ai_optimizer.get_params(symbol)

        if side == "BUY":
            stop_loss = min(float(latest['support']), price - (p["sl_atr_mult"] * atr))
            risk = price - stop_loss
            tp1 = round(price + (p["tp1_mult"] * risk), 4)
            tp2 = round(price + (p["tp2_mult"] * risk), 4)
            tp3 = round(price + (p["tp3_mult"] * risk), 4)
            stop_loss = round(stop_loss, 4)
        else:
            stop_loss = max(float(latest['resistance']), price + (p["sl_atr_mult"] * atr))
            risk = stop_loss - price
            tp1 = round(price - (p["tp1_mult"] * risk), 4)
            tp2 = round(price - (p["tp2_mult"] * risk), 4)
            tp3 = round(price - (p["tp3_mult"] * risk), 4)
            stop_loss = round(stop_loss, 4)

        qty = self.risk_manager.calculate_position_size(price, stop_loss)
        notional = qty * price
        rr_ratio = p["tp1_mult"]  # با طراحی سیستم، R:R تا TP1 همیشه برابر tp1_mult (حداقل ۱.۲:۱) است

        message = f"""
{emoji} **ANTI-LOSS ULTRA SIGNAL: {side} / {direction}**

📍 **Symbol:** {symbol}
⏱ **Timeframe:** {timeframe} (Trend 4H: {trend_4h})

💵 **Entry Price:** {price:,}

🎯 **Dynamic Targets (مدیریت پله‌ای):**
  1️⃣ TP1 (بستن ۵۰٪ + SL به سر به سر): {tp1:,}
  2️⃣ TP2 (بستن ۳۰٪ دیگر): {tp2:,}
  3️⃣ TP3 (خروج کامل، تریلینگ فعال): {tp3:,}

🛑 **Stop-Loss:** {stop_loss:,}
⚖️ **R:R تا TP1:** 1:{rr_ratio:.2f}

💰 **پیشنهاد حجم (ریسک {self.config.RISK_PER_TRADE_PCT}% سرمایه):**
  مقدار: {qty:.6f} | ارزش: {notional:,.2f} USDT

📊 **Metrics:** RSI: {latest['rsi']:.1f} | Market Guardrails Active
🧠 **قضاوت AI (اطمینان {judge_confidence}%):** {judge_reason if judge_reason else '—'}
⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
        try:
            r = requests.post(f"{self.base_url}/sendMessage",
                               json={"chat_id": self.config.TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
                               timeout=10)
            if r.status_code != 200:
                logger.error(f"ارسال سیگنال ناموفق بود: {r.status_code} {r.text}")
                return None
            logger.info(f"سیگنال ضد ضرر {side} برای {symbol} ارسال شد")
            return {"price": price, "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": stop_loss, "qty": qty, "atr": atr}
        except Exception as e:
            logger.error(f"خطای ارسال تلگرام: {e}")
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
        self.paper_trader = PaperTrader(self.config, self.telegram, self.ai_optimizer, self.journal)
        self.running = True
        self.last_signal_time: Dict[str, datetime] = {}
        self.last_summary_date: Optional[str] = date_cls.today().isoformat()
        self.kill_switch_warned_today = False

    def _build_macro_context(self) -> dict:
        """
        زمینه‌ی کلان بازار که یک‌بار در هر چرخه ساخته می‌شه (نه به‌ازای هر ارز، برای صرفه‌جویی
        در تعداد فراخوانی). هر بخشش کاملاً مستقل و fail-safe هست: اگه یکی خطا بده، فقط
        همون مقدار None می‌مونه و بقیه‌ی سیستم عادی کار می‌کنه.
        """
        context = {"fear_greed": None, "btc_trend_4h": None, "btc_structure": None}
        try:
            context["fear_greed"] = self.macro_data.get_fear_greed_index()
        except Exception as e:
            logger.warning(f"خطا در دریافت شاخص ترس‌وطمع: {e}")

        try:
            btc_df_4h = self.data.fetch_ohlcv("BTC/USDT", timeframe=self.config.TREND_TIMEFRAME)
            btc_df_4h = self.analysis.calculate_indicators(btc_df_4h)
            context["btc_trend_4h"] = self.analysis.get_major_trend(btc_df_4h)

            btc_df_15m = self.data.fetch_ohlcv("BTC/USDT", timeframe=self.config.ENTRY_TIMEFRAME)
            btc_df_15m = self.analysis.calculate_indicators(btc_df_15m)
            context["btc_structure"] = self.analysis.market_structure(btc_df_15m)
        except Exception as e:
            logger.warning(f"خطا در ساخت زمینه‌ی کلان بیت‌کوین: {e}")

        return context

    def process_symbol(self, symbol: str, macro_context: Optional[dict] = None):
        try:
            df_15m = self.data.fetch_ohlcv(symbol, timeframe=self.config.ENTRY_TIMEFRAME)
            df_15m = self.analysis.calculate_indicators(df_15m)
            if df_15m.empty:
                return

            if self.ai_optimizer.should_optimize(symbol):
                logger.info(f"بروزرسانی پارامترهای ضد ضرر هوش مصنوعی برای {symbol}...")
                self.ai_optimizer.optimize_symbol_parameters(symbol, df_15m, journal=self.journal)

            df_1h = self.data.fetch_ohlcv(symbol, timeframe=self.config.CONFIRM_TIMEFRAME)
            df_1h = self.analysis.calculate_indicators(df_1h)

            df_4h = self.data.fetch_ohlcv(symbol, timeframe=self.config.TREND_TIMEFRAME)
            df_4h = self.analysis.calculate_indicators(df_4h)
            trend_4h = self.analysis.get_major_trend(df_4h)

            # داده‌ی فرابازاری مختص همین ارز (best-effort، هر کدوم می‌تونه None باشه)
            symbol_macro_context = dict(macro_context or {})
            symbol_macro_context["funding_rate"] = self.data.fetch_funding_rate(symbol)
            symbol_macro_context["spread_pct"] = self.data.fetch_spread_pct(symbol)

            rule_signal, diagnostics = self.signal_engine.get_rule_signal(symbol, df_15m, df_1h, trend_4h, symbol_macro_context)
            if not rule_signal:
                return

            now = datetime.now()
            p = self.ai_optimizer.get_params(symbol)
            cooldown = p.get("cooldown_minutes", 90)
            if symbol in self.last_signal_time:
                if now - self.last_signal_time[symbol] < timedelta(minutes=cooldown):
                    return

            # کلید قطع ضرر روزانه
            if self.risk_manager.kill_switch_triggered(self.journal):
                if not self.kill_switch_warned_today:
                    logger.warning("کلید قطع ضرر روزانه فعال شد - تا فردا سیگنال جدیدی صادر نمی‌شه")
                    self.telegram.send_system_status("🛑 **کلید قطع ضرر روزانه فعال شد.** برای امروز دیگه معامله‌ی جدیدی باز نمی‌شه.")
                    self.kill_switch_warned_today = True
                return

            # محدودیت تعداد هم‌زمان و اکسپوژر همبسته
            can_open, reason = self.risk_manager.can_open_trade(symbol, self.paper_trader.active_trades)
            if not can_open:
                logger.info(f"{symbol}: سیگنال {rule_signal} رد شد - {reason}")
                return

            # لایه‌ی قضاوت discretionary شبیه‌ به تریدر انسانی باتجربه، روی سیگنال کمی
            diagnostics["open_positions_count"] = len(self.paper_trader.active_trades)
            diagnostics["today_realized_pnl_usdt"] = round(self.journal.get_today_realized_pnl_usdt(), 2)
            judge = self.ai_optimizer.evaluate_trade_candidate(symbol, rule_signal, diagnostics)
            if not judge["approve"] or judge["confidence"] < self.config.MIN_JUDGE_CONFIDENCE:
                logger.info(f"{symbol}: سیگنال {rule_signal} توسط لایه‌ی قضاوت AI رد شد (اطمینان {judge['confidence']}%) - {judge['reason']}")
                return

            latest = df_15m.iloc[-1]
            trade_data = self.telegram.send_signal(symbol, rule_signal, latest, trend_4h, self.config.ENTRY_TIMEFRAME,
                                                     judge_reason=judge["reason"], judge_confidence=judge["confidence"])

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
                    atr_at_entry=trade_data["atr"]
                )
                self.last_signal_time[symbol] = now

        except Exception as e:
            logger.error(f"خطا در پردازش {symbol}: {e}")

    def _check_daily_rollover(self):
        today = date_cls.today().isoformat()
        if self.last_summary_date and today != self.last_summary_date:
            summary = self.journal.build_daily_summary(self.last_summary_date)
            if summary:
                self.telegram.send_system_status(summary)
            self.last_summary_date = today
            self.kill_switch_warned_today = False

    def run_once(self):
        logger.info("----- شروع آنالیز ایمن و ضد ضرر بازار -----")
        self._check_daily_rollover()
        macro_context = self._build_macro_context()
        for symbol in self.config.SYMBOLS:
            self.process_symbol(symbol, macro_context)
            time.sleep(1.5)
        self.paper_trader.update_and_check_trades(self.data)

    def start(self):
        logger.info("بات حرفه‌ای با مدیریت ریسک و ژورنال معاملات فعال شد")

        if not self.telegram.test_connection():
            logger.error("اتصال تلگرام برقرار نشد! توکن یا chat_id رو چک کن.")

        start_message = f"""🛡 **نسخه حرفه‌ای فعال شد.**

باگ ارسال پیام برطرف شد. امکانات جدید:
• مدیریت سرمایه ریسک‌محور (ریسک {self.config.RISK_PER_TRADE_PCT}% در هر معامله)
• محدودیت اکسپوژر همبسته و حداکثر {self.config.MAX_CONCURRENT_TRADES} معامله هم‌زمان
• کلید قطع ضرر روزانه در {self.config.MAX_DAILY_LOSS_PCT}%
• تایید ساختار بازار + چندتایم‌فریمی (15m/1h/4h)
• فیلتر رژیم نوسان + تریلینگ استاپ واقعی
• ژورنال معاملات و گزارش روزانه Win-rate/Expectancy
• لایه‌ی قضاوت discretionary AI روی هر سیگنال (شبیه تریدر انسانی باتجربه، حداقل اطمینان {self.config.MIN_JUDGE_CONFIDENCE}%)
• داده‌ی فرابازاری: شاخص ترس‌وطمع، رژیم کلان بیت‌کوین، فاندینگ ریت و اسپرد لحظه‌ای (best-effort)
"""
        self.telegram.send_system_status(start_message)

        while self.running:
            self.run_once()
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
