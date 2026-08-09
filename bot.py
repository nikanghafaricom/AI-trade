# ==============================================
# Hybrid Signal Bot - نسخه فوق پیشرفته و هوشمند
# ==============================================
import os
import time
import logging
import requests
import gc  # جهت مدیریت و پاکسازی RAM
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from typing import Dict, Optional, List
import pandas as pd
import ccxt
from openai import OpenAI
from dotenv import load_dotenv

# بارگذاری فایل .env (فقط برای محیط محلی)
load_dotenv()

# ==================== وب‌سرور استاندارد ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        return  # خاموش کردن لاگ‌های اضافه وب‌سرور

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

    AI_API_KEY = os.getenv("AI_API_KEY")
    AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.x.ai/v1")
    AI_MODEL = os.getenv("AI_MODEL", "grok-3")

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    SYMBOLS = [
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "BNB/USDT",
        "XRP/USDT",
    ]

    TIMEFRAME = "15m"
    CHECK_INTERVAL = 300          # هر ۵ دقیقه
    MIN_CONFIDENCE_AI = 0.70      # افزایش حد نصاب اطمینان AI برای بالابردن دقت

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
        
        # محاسبه سبک و بومی RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # محاسبه سبک EMA
        df['ema_fast'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=50, adjust=False).mean()

        # محاسبه سبک ATR
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close = (df['low'] - df['close'].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['atr'] = tr.rolling(window=14).mean()

        # محاسبه حجم میانگین (Volume SMA20) برای فیلتر حجم
        df['vol_sma'] = df['volume'].rolling(window=20).mean()

        # محاسبه Pivot High و Pivot Low (حمایت و مقاومت محلی ۱۰ کندل گذشته)
        df['support'] = df['low'].rolling(window=10).min()
        df['resistance'] = df['high'].rolling(window=10).max()

        return df

    def get_ai_confirmation(self, symbol: str, side: str, df: pd.DataFrame) -> Dict:
        latest = df.iloc[-1]
        prev = df.iloc[-2]

        prompt = f"""
You are an expert crypto price-action trader.
Signal Context:
- Symbol: {symbol}
- Proposed Side: {side}
- Current Close: {latest['close']} (Prev Close: {prev['close']})
- High/Low Range: High={latest['high']}, Low={latest['low']}
- RSI (14): {latest['rsi']:.2f} (Prev RSI: {prev['rsi']:.2f})
- EMA20: {latest['ema_fast']:.2f} | EMA50: {latest['ema_slow']:.2f}
- Volume: {latest['volume']:.2f} vs Avg Volume: {latest['vol_sma']:.2f}
- Support Level: {latest['support']} | Resistance Level: {latest['resistance']}

Analyze price action, volume confirmation, and momentum.
Strict Rule: Respond ONLY with "CONFIRM" or "REJECT". No other words or punctuation.
"""
        try:
            response = self.client.chat.completions.create(
                model=self.config.AI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=6
            )
            answer = response.choices[0].message.content.strip().upper()
            confirmed = "CONFIRM" in answer
            return {
                "confirmed": confirmed,
                "confidence": 0.85 if confirmed else 0.2,
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
        prev = df.iloc[-2]
        
        if pd.isna(latest['rsi']) or pd.isna(latest['ema_fast']) or pd.isna(latest['vol_sma']):
            return None

        # فیلتر حجم: حجم کندل باید از میانگین بیشتر باشد تا شکست جعلی نخوریم
        volume_confirmed = latest['volume'] > (latest['vol_sma'] * 0.9)

        ema_bullish = latest['ema_fast'] > latest['ema_slow']
        ema_bearish = latest['ema_fast'] < latest['ema_slow']

        # سیگنال خرید: روند صعودی + خروج RSI از منطقه اشباع + تأیید نسبی حجم
        rsi_buy = (prev['rsi'] < 42 and latest['rsi'] >= 42) or (45 < latest['rsi'] < 62 and prev['rsi'] < latest['rsi'])
        if ema_bullish and rsi_buy and volume_confirmed:
            return "BUY"

        # سیگنال فروش: روند نزولی + خروج RSI از منطقه اشباع + تأیید نسبی حجم
        rsi_sell = (prev['rsi'] > 58 and latest['rsi'] <= 58) or (38 < latest['rsi'] < 55 and prev['rsi'] > latest['rsi'])
        if ema_bearish and rsi_sell and volume_confirmed:
            return "SELL"

        return None

# ==================== ارسال تلگرام ====================
class TelegramSender:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

    def send_system_status(self, text: str):
        try:
            requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.config.TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "Markdown"
                },
                timeout=10
            )
        except Exception as e:
            logger.error(f"خطای ارسال پیام سیستمی به تلگرام: {e}")

    def send_signal(self, symbol: str, side: str, latest: pd.Series, confidence: float):
        emoji = "🟢" if side == "BUY" else "🔴"
        direction = "LONG" if side == "BUY" else "SHORT"
        price = float(latest['close'])
        atr = float(latest['atr']) if not pd.isna(latest['atr']) else price * 0.01

        # حد سود و زیان داینامیک بر اساس حمایت/مقاومت محلی و ATR
        if side == "BUY":
            stop_loss = min(float(latest['support']), price - (1.2 * atr))
            risk = price - stop_loss
            tp1 = round(price + (1.2 * risk), 4)
            tp2 = round(price + (2.0 * risk), 4)
            tp3 = round(price + (3.0 * risk), 4)
            stop_loss = round(stop_loss, 4)
        else:
            stop_loss = max(float(latest['resistance']), price + (1.2 * atr))
            risk = stop_loss - price
            tp1 = round(price - (1.2 * risk), 4)
            tp2 = round(price - (2.0 * risk), 4)
            tp3 = round(price - (3.0 * risk), 4)
            stop_loss = round(stop_loss, 4)

        message = f"""
{emoji} **SIGNAL: {side} / {direction}**

📍 **Symbol:** {symbol}
⏱ **Timeframe:** {self.config.TIMEFRAME}

💵 **Entry Price:** {price:,}

🎯 **Targets (Dynamic Pivots):**
  1️⃣ TP1: {tp1:,}
  2️⃣ TP2: {tp2:,}
  3️⃣ TP3: {tp3:,}

🛑 **Stop-Loss:** {stop_loss:,}

📊 **Analysis:** Volume Confirmed | RSI: {latest['rsi']:.1f} | AI: {confidence:.0%}
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
        
        # حافظه داخلی برای ثبت آخرین سیگنال جهت جلوگیری از سیگنال تکراری
        self.active_signals: Dict[str, str] = {}

    def process_symbol(self, symbol: str):
        try:
            df = self.data.fetch_ohlcv(symbol)
            df = self.analysis.calculate_indicators(df)

            rule_signal = self.signal_engine.get_rule_signal(df)
            if not rule_signal:
                # اگر سیگنال خنثی شد، حافظه قبلی پاک می‌شود
                self.active_signals[symbol] = None
                return

            # اگر سیگنال تکراری باشد، پیام دوباره فرستاده نمی‌شود
            if self.active_signals.get(symbol) == rule_signal:
                return

            latest = df.iloc[-1]
            logger.info(f"{symbol} | قانون گفت: {rule_signal} → در حال تأیید پیشرفته با AI...")

            ai_result = self.analysis.get_ai_confirmation(symbol, rule_signal, df)

            if ai_result["confirmed"] and ai_result["confidence"] >= self.config.MIN_CONFIDENCE_AI:
                self.telegram.send_signal(symbol, rule_signal, latest, ai_result["confidence"])
                self.active_signals[symbol] = rule_signal  # ثبت سیگنال ارسال‌شده
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
        logger.info("بات سیگنال پیشرفته شروع شد")
        logger.info(f"ارزها: {', '.join(self.config.SYMBOLS)}")
        
        start_message = f"🚀 **ربات سیگنال‌دهی پیشرفته (نسخه هوشمند V2) روشن شد.**\n\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}\nارزهای فعال: {', '.join(self.config.SYMBOLS)}"
        self.telegram.send_system_status(start_message)

        while self.running:
            self.run_once()
            gc.collect()
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
