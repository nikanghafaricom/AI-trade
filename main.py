# ==============================================
# Hybrid Signal Bot - نسخه رندر (Render: دریافت، تحلیل، ارسال به کانال و همروش)
# ==============================================
import os
import time
import logging
import gc
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from typing import Dict, Optional, List
import pandas as pd
import ccxt
import requests
from dotenv import load_dotenv

load_dotenv()

# ==================== تنظیمات ====================
class Config:
    EXCHANGE_ID = "coinex"
    API_KEY = os.getenv("EXCHANGE_API_KEY", "")
    SECRET = os.getenv("EXCHANGE_SECRET", "")
    PASSWORD = os.getenv("EXCHANGE_PASSWORD", "")

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")   # مخصوص کانال (سیگنال‌های خام)
    TELEGRAM_PERSONAL_ID = os.getenv("TELEGRAM_PERSONAL_ID", "") # مخصوص پی‌وی (نتیجه معاملات)
    HAMRAVESH_WEBHOOK_URL = os.getenv("HAMRAVESH_WEBHOOK_URL", "")
    SECRET_TOKEN = os.getenv("SECRET_TOKEN", "")

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
    ]

    ENTRY_TIMEFRAME = "15m"
    TREND_TIMEFRAME = "4h"
    CHECK_INTERVAL = 300  # هر ۵ دقیقه یک‌بار

    def validate(self):
        if not self.TELEGRAM_BOT_TOKEN or (not self.TELEGRAM_CHANNEL_ID and not self.TELEGRAM_PERSONAL_ID):
            print("هشدار: توکن یا آیدی‌های تلگرام به درستی تنظیم نشده‌اند.")

# ==================== لاگ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler("render_bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== لایه تحلیل تکنیکال (منتقل شده به رندر) ====================
class AnalysisLayer:
    def __init__(self, config: Config):
        self.config = config

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        df['ema_fast'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=50, adjust=False).mean()
        df['ema_trend'] = df['close'].ewm(span=200, adjust=False).mean()

        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close = (df['low'] - df['close'].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['atr'] = tr.rolling(window=14).mean()

        df['vol_sma'] = df['volume'].rolling(window=20).mean()
        df['support'] = df['low'].rolling(window=15).min()
        df['resistance'] = df['high'].rolling(window=15).max()

        return df

    def get_major_trend(self, df_trend: pd.DataFrame) -> str:
        latest = df_trend.iloc[-1]
        if latest['close'] > latest['ema_trend'] and latest['ema_fast'] > latest['ema_slow']:
            return "BULLISH"
        elif latest['close'] < latest['ema_trend'] and latest['ema_fast'] < latest['ema_slow']:
            return "BEARISH"
        return "NEUTRAL"

# ==================== موتور سیگنال (منتقل شده به رندر) ====================
class SignalEngine:
    def __init__(self, config: Config):
        self.config = config

    def get_rule_signal(self, df_15m: pd.DataFrame, trend_4h: str) -> Optional[str]:
        latest = df_15m.iloc[-1]
        prev = df_15m.iloc[-2]
        
        if pd.isna(latest['rsi']) or pd.isna(latest['ema_fast']) or pd.isna(latest['atr']):
            return None

        if latest['atr'] < (latest['close'] * 0.0015):
            return None

        volume_confirmed = latest['volume'] > (latest['vol_sma'] * 0.50)

        if trend_4h in ["BULLISH", "NEUTRAL"]:
            ema_bull = latest['ema_fast'] > latest['ema_slow']
            rsi_buy = (latest['rsi'] > 42 and prev['rsi'] <= 42) or (48 <= latest['rsi'] <= 65 and latest['rsi'] > prev['rsi'])
            if ema_bull and rsi_buy and volume_confirmed:
                return "BUY"

        if trend_4h in ["BEARISH", "NEUTRAL"]:
            ema_bear = latest['ema_fast'] < latest['ema_slow']
            rsi_sell = (latest['rsi'] < 58 and prev['rsi'] >= 58) or (35 <= latest['rsi'] <= 52 and latest['rsi'] < prev['rsi'])
            if ema_bear and rsi_sell and volume_confirmed:
                return "SELL"

        return None

# ==================== ارسال‌کننده پیام به تلگرام ====================
class TelegramNotifier:
    @staticmethod
    def send_to_channel(message: str):
        config = Config()
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHANNEL_ID:
            return
        try:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": config.TELEGRAM_CHANNEL_ID,
                "text": message,
                "parse_mode": "Markdown"
            }
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logger.error(f"خطا در ارسال پیام به کانال تلگرام: {e}")

    @staticmethod
    def send_to_personal(message: str):
        config = Config()
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_PERSONAL_ID:
            return
        try:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": config.TELEGRAM_PERSONAL_ID,
                "text": message,
                "parse_mode": "Markdown"
            }
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logger.error(f"خطا در ارسال پیام به پی‌وی تلگرام: {e}")

# ==================== وب‌سرور برای Health Check و دریافت گزارش سود/زیان از همروش ====================
class RenderWebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Render Signal Generator is alive and running!")

    def do_POST(self):
        try:
            auth_token = self.headers.get("X-Secret-Token")
            config = Config()
            
            if config.SECRET_TOKEN and auth_token != config.SECRET_TOKEN:
                self.send_response(403)
                self.end_headers()
                return

            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            action = data.get("action")

            # دریافت گزارش بسته شدن معامله و محاسبه سود/زیان واقعی از همروش
            if action == "close_trade":
                symbol = data.get("symbol")
                exit_price = data.get("exit_price")
                pnl = data.get("pnl")
                
                emoji = "✅" if pnl >= 0 else "❌"
                status_text = "سود" if pnl >= 0 else "زیان"
                
                close_msg = (
                    f"{emoji} **گزارش نتیجه نهایی معامله (اسپات)** {emoji}\n\n"
                    f"💎 نماد: `{symbol}`\n"
                    f"💵 قیمت خروج: `{exit_price}`\n"
                    f"📊 نتیجه: **{status_text} با {pnl:+.2f}%**\n"
                    f"🏷 صرافی: `تبدیل (Tabdeal)`"
                )
                TelegramNotifier.send_to_personal(close_msg)

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "received"}).encode('utf-8'))

        except Exception as e:
            logger.error(f"خطا در پردازش وب‌هوک برگشتی در رندر: {e}")
            self.send_response(500)
            self.end_headers()

    def log_message(self, format, *args):
        return

def start_render_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        server = HTTPServer(("0.0.0.0", port), RenderWebhookHandler)
        server.serve_forever()
    except Exception as e:
        logger.error(f"خطا در اجرای وب‌سرور رندر: {e}")

threading.Thread(target=start_render_server, daemon=True).start()

# ==================== صرافی (برای دانلود دیتا از کوین‌اکس) ====================
class PublicMarketDataFetcher:
    def __init__(self, config: Config):
        try:
            exchange_class = getattr(ccxt, config.EXCHANGE_ID)
            self.exchange = exchange_class({
                'apiKey': config.API_KEY,
                'secret': config.SECRET,
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'}
            })
        except Exception as e:
            logger.error(f"خطا در ایجاد اتصال صرافی: {e}")
            self.exchange = None

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> list:
        if not self.exchange:
            return []
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            return ohlcv
        except Exception as e:
            logger.error(f"خطا در دریافت کندل‌های {symbol} ({timeframe}): {e}")
            return []

# ==================== بررسی اتصال کامل چرخه‌ها و ارسال پیام راه‌اندازی ====================
def verify_and_notify_startup(config: Config):
    """بررسی اتصال تک تک چرخه‌ها (رندر به همروش و برگشت) و ارسال پیام راه‌اندازی فقط در صورت صحت کامل"""
    if not config.HAMRAVESH_WEBHOOK_URL:
        logger.warning("آدرس وب‌هوک همروش تنظیم نشده است؛ اتصال کامل تایید نمی‌شود.")
        return

    max_retries = 10
    delay = 6

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"تلاش {attempt}/{max_retries} برای بررسی اتصال چرخه کامل رندر <-> همروش...")
            headers = {"X-Secret-Token": config.SECRET_TOKEN}
            # ارسال یک درخواست پینگ تست به همروش برای سنجش سلامت چرخه
            response = requests.post(config.HAMRAVESH_WEBHOOK_URL, json={"action": "ping"}, headers=headers, timeout=5)
            
            if response.status_code == 200:
                logger.info("اتصال چرخه کامل رندر و همروش با موفقیت برقرار شد و تایید گردید.")
                startup_msg = "🚀 ربات هیبرید با موفقیت روشن شد: تمام چرخه‌ها (رندر، همروش، تحلیل و صرافی) کاملاً متصل و عملیاتی هستند."
                TelegramNotifier.send_to_personal(startup_msg)
                return
        except Exception as e:
            logger.warning(f"تلاش {attempt}: هنوز ارتباط کامل برقرار نشده است ({e})")
        
        time.sleep(delay)
    
    logger.error("خطا: چرخه‌های رندر و همروش به طور کامل متصل نشدند؛ پیام راه‌اندازی ارسال نگردید.")

# ==================== سیستم اصلی رندر ====================
class RenderSignalSystem:
    def __init__(self):
        self.config = Config()
        self.config.validate()
        self.data_fetcher = PublicMarketDataFetcher(self.config)
        self.analysis = AnalysisLayer(self.config)
        self.signal_engine = SignalEngine(self.config)
        self.running = True
        self.last_signal_time: Dict[str, datetime] = {}

    def send_signal_to_hamravesh(self, payload: dict):
        if not self.config.HAMRAVESH_WEBHOOK_URL:
            return
        try:
            headers = {"X-Secret-Token": self.config.SECRET_TOKEN}
            requests.post(self.config.HAMRAVESH_WEBHOOK_URL, json=payload, headers=headers, timeout=15)
        except Exception as e:
            logger.error(f"خطا در ارسال سیگنال به همروش: {e}")

    def run_loop(self):
        logger.info("بخش رندر (Render Signal Generator) با موفقیت فعال شد.")
        
        # بررسی صحت اتصال چرخه کامل و ارسال پیام راه‌اندازی (در یک ترد جداگانه برای جلوگیری از بلاک شدن حلقه اصلی)
        threading.Thread(target=verify_and_notify_startup, args=(self.config,), daemon=True).start()

        while self.running:
            for symbol in self.config.SYMBOLS:
                try:
                    ohlcv_15m = self.data_fetcher.fetch_ohlcv(symbol, self.config.ENTRY_TIMEFRAME, limit=120)
                    
                    if ohlcv_15m and len(ohlcv_15m) >= 50:
                        df = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                        
                        df_indicators = self.analysis.calculate_indicators(df)
                        trend = self.analysis.get_major_trend(df_indicators)

                        rule_signal = self.signal_engine.get_rule_signal(df_indicators, trend)
                        if rule_signal:
                            now = datetime.now()
                            if symbol in self.last_signal_time:
                                if now - self.last_signal_time[symbol] < timedelta(minutes=90):
                                    continue

                            latest = df_indicators.iloc[-1]
                            price = float(latest['close'])

                            # ۱. ارسال سیگنال خام به کانال تلگرام
                            channel_msg = (
                                f"📢 **سیگنال جدید بازار (تحلیل تکنیکال)** 📢\n\n"
                                f"💎 نماد: `{symbol}`\n"
                                f"📊 سیگنال: **{rule_signal}**\n"
                                f"💵 قیمت لحظه‌ای: `{price}`\n"
                                f"📈 روند صعودی/نزولی: `{trend}`\n"
                                f"⚡️ وضعیت: شناسایی و ارسال شده توسط ربات هوشمند"
                            )
                            TelegramNotifier.send_to_channel(channel_msg)

                            # ۲. ارسال دستور معامله به همروش
                            payload = {
                                "action": "execute_trade",
                                "symbol": symbol,
                                "side": rule_signal,
                                "price": price,
                                "trend": trend
                            }
                            self.send_signal_to_hamravesh(payload)

                            self.last_signal_time[symbol] = now

                    time.sleep(2)
                except Exception as e:
                    logger.error(f"خطا در حلقه پردازش نماد {symbol}: {e}")

            gc.collect()
            logger.info(f"پایان چرخه بررسی بازار. انتظار برای دور بعدی ({self.config.CHECK_INTERVAL} ثانیه)...")
            time.sleep(self.config.CHECK_INTERVAL)

    def stop(self):
        self.running = False
        logger.info("بخش رندر متوقف شد.")

if __name__ == "__main__":
    system = RenderSignalSystem()
    try:
        system.run_loop()
    except KeyboardInterrupt:
        system.stop()
