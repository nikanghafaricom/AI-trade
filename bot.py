# ==============================================
# Hybrid Signal Bot - نسخه فوق‌العاده سبک (No Pandas / No CCXT)
# ==============================================

import os
import time
import logging
import requests
from datetime import datetime
from typing import Dict, Optional, List
from openai import OpenAI
from dotenv import load_dotenv

# بارگذاری فایل .env
load_dotenv()

# ==================== تنظیمات ====================
class Config:
    # ---------- هوش مصنوعی (Groq / xAI) ----------
    AI_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("AI_API_KEY")
    AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1")
    AI_MODEL = os.getenv("AI_MODEL", "llama-3.3-70b-versatile")

    # ---------- تلگرام ----------
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    # ---------- ارزها ----------
    SYMBOLS = [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
        "XRPUSDT",
    ]

    TIMEFRAME = "15m"
    CHECK_INTERVAL = 300          # هر ۵ دقیقه
    MIN_CONFIDENCE_AI = 0.65

    def validate(self):
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

# ==================== لایه دریافت داده از بایننس ====================
class DataLayer:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = "https://api.binance.com/api/v3/klines"

    def fetch_ohlcv(self, symbol: str, limit: int = 100) -> List[Dict]:
        params = {
            'symbol': symbol,
            'interval': self.config.TIMEFRAME,
            'limit': limit
        }
        try:
            res = requests.get(self.base_url, params=params, timeout=10)
            res.raise_for_status()
            data = res.json()
            candles = []
            for item in data:
                candles.append({
                    'timestamp': item[0],
                    'open': float(item[1]),
                    'high': float(item[2]),
                    'low': float(item[3]),
                    'close': float(item[4]),
                    'volume': float(item[5])
                })
            return candles
        except Exception as e:
            logger.error(f"خطا در دریافت کندل‌های {symbol}: {e}")
            return []

# ==================== توابع ریاضی سبک اندیکاتورها ====================
def calc_rsi(closes: List[float], length: int = 14) -> List[float]:
    if len(closes) < length + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(change if change > 0 else 0.0)
        losses.append(abs(change) if change < 0 else 0.0)

    rsi = [0.0] * len(closes)
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length

    for i in range(length, len(closes)):
        if i > length:
            avg_gain = (avg_gain * (length - 1) + gains[i - 1]) / length
            avg_loss = (avg_loss * (length - 1) + losses[i - 1]) / length

        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))

    return rsi

def calc_ema(closes: List[float], length: int) -> List[float]:
    if len(closes) < length:
        return []
    k = 2.0 / (length + 1)
    ema = [0.0] * len(closes)
    ema[length - 1] = sum(closes[:length]) / length

    for i in range(length, len(closes)):
        ema[i] = (closes[i] * k) + (ema[i - 1] * (1 - k))

    return ema

# ==================== لایه تحلیل ====================
class AnalysisLayer:
    def __init__(self, config: Config):
        self.config = config
        self.client = OpenAI(
            api_key=config.AI_API_KEY,
            base_url=config.AI_BASE_URL
        )

    def calculate_indicators(self, candles: List[Dict]) -> Dict:
        closes = [c['close'] for c in candles]
        rsi_list = calc_rsi(closes, length=14)
        ema20_list = calc_ema(closes, length=20)
        ema50_list = calc_ema(closes, length=50)

        latest_candle = candles[-1]
        return {
            'close': latest_candle['close'],
            'rsi': rsi_list[-1] if rsi_list else 0.0,
            'ema_fast': ema20_list[-1] if ema20_list else 0.0,
            'ema_slow': ema50_list[-1] if ema50_list else 0.0,
        }

    def get_ai_confirmation(self, symbol: str, side: str, latest: Dict) -> Dict:
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

    def get_rule_signal(self, latest: Dict) -> Optional[str]:
        if not latest['rsi'] or not latest['ema_fast']:
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

    def send_signal(self, symbol: str, side: str, latest: Dict, confidence: float):
        side_fa = "خرید" if side == "BUY" else "فروش"
        display_symbol = f"{symbol[:-4]}/USDT" if symbol.endswith("USDT") else symbol
        message = f"""
🔔 **سیگنال جدید**

[{display_symbol}]
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
        self.config.validate()
        self.data = DataLayer(self.config)
        self.analysis = AnalysisLayer(self.config)
        self.signal_engine = SignalEngine(self.config)
        self.telegram = TelegramSender(self.config)
        self.running = True

    def process_symbol(self, symbol: str):
        try:
            candles = self.data.fetch_ohlcv(symbol)
            if not candles:
                return

            latest = self.analysis.calculate_indicators(candles)
            rule_signal = self.signal_engine.get_rule_signal(latest)
            if not rule_signal:
                return

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
            time.sleep(1)

    def start(self):
        logger.info("بات سیگنال چند ارزی (نسخه سبک) شروع شد")
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
