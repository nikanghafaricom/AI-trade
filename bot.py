# ==============================================
# Hybrid Signal Bot - نسخه بسیار سبک (Ultra-Light Memory)
# ==============================================

import os
import time
import logging
import requests
from datetime import datetime
from typing import Dict, Optional, List
import pandas as pd
import ccxt
from openai import OpenAI
from dotenv import load_dotenv

# بارگذاری فایل .env (فقط برای محیط محلی)
load_dotenv()

# ==================== تنظیمات ====================
class Config:
    # ---------- صرافی ----------
    EXCHANGE_ID = "binance"
    API_KEY = os.getenv("EXCHANGE_API_KEY", "")
    SECRET = os.getenv("EXCHANGE_SECRET", "")
    PASSWORD = os.getenv("EXCHANGE_PASSWORD", "")

    # ---------- هوش مصنوعی (xAI / Grok / Groq) ----------
    AI_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("AI_API_KEY")
    AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1")
    AI_MODEL = os.getenv("AI_MODEL", "llama-3.3-70b-versatile")

    # ---------- تلگرام ----------
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    # ---------- ارزها ----------
    SYMBOLS = [
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "BNB/USDT",
        "XRP/USDT",
    ]

    TIMEFRAME = "15m"
    CHECK_INTERVAL = 300          # هر ۵ دقیقه

    MIN_CONFIDENCE_AI = 0.65

    def validate(self):
        """چک کردن وجود کلیدهای ضروری"""
        required = {
            "AI_API_KEY": self.AI_API_KEY,
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

    def fetch_ohlcv(self, symbol: str, limit: int = 100) -> pd.DataFrame:
        ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe=self.config.TIMEFRAME, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df

# ==================== توابع محاسباتی سبک (جایگزین pandas-ta) ====================
def calc_rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/length, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/length, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calc_ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()

def calc_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high = df['high']
    low = df['low']
    close = df['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1/length, adjust=False).mean()

# ==================== لایه تحلیل ====================
class AnalysisLayer:
    def __init__(self, config: Config):
        self.config = config
        self.client = OpenAI(
            api_key=config.AI_API_KEY,
            base_url=config.AI_BASE_URL
        )

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df['rsi'] = calc_rsi(df['close'], length=14)
        df['ema_fast'] = calc_ema(df['close'], length=20)
        df['ema_slow'] = calc_ema(df['close'], length=50)
        df['atr'] = calc_atr(df, length=14)
        return df

    def get_ai_confirmation(self, symbol: str, side: str, latest: pd.Series) -> Dict:
        prompt = f"""
تو یک تحلیل‌گر کوتاه‌مدت بازار کریپتو هستی.
سیگنال قوانین تکنیکال: {side}
ارز: {symbol}
قیمت فعلی: {latest['close']}
RSI: {latest['rsi']:.2f}
EMA20: {latest['ema_fast']:.2f}
EMA50: {latest['ema_slow']:.2f}

فقط یکی از این دو جواب را بده:
CONFIRM
REJECT

قوانین:
- اگر با سیگنال موافقی CONFIRM بگو
- اگر مخالفی یا مطمئن نیستی REJECT بگو
- هیچ متن دیگری ننویس
"""
        try:
            response = self.client.chat.completions.create(
                model=self.config.AI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=8
            )
            answer = response.choices[0].message.content.strip().upper()
            confirmed = "CONFIRM" in answer
            return {
                "confirmed": confirmed,
                "confidence": 0.78 if confirmed else 0.3,
                "raw": answer
            }
        except Exception as e:
            logger.error(f"خطای AI برای {symbol}: {e}")
            return {"confirmed": False, "confidence": 0.0, "raw": "ERROR"}

# ==================== موتور سیگنال ====================
class SignalEngine:
    def __init__(self, config: Config):
        self.config = config

    def get_rule_signal(self, df: pd.DataFrame) -> Optional[str]:
        latest = df.iloc[-1]
        if pd.isna(latest['rsi']) or pd.isna(latest['ema_fast']):
            return None

        if latest['ema_fast'] > latest['ema_slow'] and latest['rsi'] < 35:
            return "BUY"
        if latest['ema_fast'] < latest['ema_slow'] and latest['rsi'] > 65:
            return "SELL"
        return None

# ==================== ارسال تلگرام ====================
class TelegramSender:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

    def send_signal(self, symbol: str, side: str, latest: pd.Series, confidence: float):
        side_fa = "خرید" if side == "BUY" else "فروش"
        message = f"""
🔔 **سیگنال جدید**

[{symbol}]
الان: **{side_fa}**

قیمت: `{latest['close']}`
RSI: `{latest['rsi']:.2f}`
اطمینان: `{confidence:.0%}`

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.config.TELEGRAM_CHAT_ID,
                    "text": message,
                    "parse_mode": "Markdown"
                },
                timeout=10
            )
            if response.status_code == 200:
                logger.info(f"سیگنال {side} برای {symbol} ارسال شد")
            else:
                logger.error(f"خطای تلگرام: {response.text}")
        except Exception as e:
            logger.error(f"خطای ارسال تلگرام: {e}")

# ==================== سیستم اصلی ====================
class HybridTradingSystem:
    def __init__(self):
        self.config = Config()
        self.config.validate()          # چک کردن کلیدها
        self.data = DataLayer(self.config)
        self.analysis = AnalysisLayer(self.config)
        self.signal_engine = SignalEngine(self.config)
        self.telegram = TelegramSender(self.config)
        self.running = True

    def process_symbol(self, symbol: str):
        try:
            df = self.data.fetch_ohlcv(symbol)
            df = self.analysis.calculate_indicators(df)

            rule_signal = self.signal_engine.get_rule_signal(df)
            if not rule_signal:
                return

            latest = df.iloc[-1]
            logger.info(f"{symbol} | قانون گفت: {rule_signal} → در حال تأیید با AI...")

            ai_result = self.analysis.get_ai_confirmation(symbol, rule_signal, latest)

            if ai_result["confirmed"] and ai_result["confidence"] >= self.config.MIN_CONFIDENCE_AI:
                self.telegram.send_signal(symbol, rule_signal, latest, ai_result["confidence"])
            else:
                logger.info(f"{symbol} | AI رد کرد")

        except Exception as e:
            logger.error(f"خطا در پردازش {symbol}: {e}")

    def run_once(self):
        logger.info("----- شروع بررسی همه ارزها -----")
        for symbol in self.config.SYMBOLS:
            self.process_symbol(symbol)
            time.sleep(1.5)

    def start(self):
        logger.info("بات سیگنال چند ارزی شروع شد")
        logger.info(f"ارزها: {', '.join(self.config.SYMBOLS)}")
        while self.running:
            self.run_once()
            time.sleep(self.config.CHECK_INTERVAL)

    def stop(self):
        self.running = False
        logger.info("بات متوقف شد")

# ==================== اجرا ====================
if __name__ == "__main__":
    bot = HybridTradingSystem()
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.stop()
