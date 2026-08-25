# ==============================================
# Hybrid Signal Bot - نسخه نهایی رندر (Render - Signal Fetcher & Telegram Bot)
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
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")   # مخصوص کانال (سیگنال‌ها)
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

# ==================== وب‌سرور برای Health Check و دریافت تاییدیه معامله از همروش ====================
class RenderWebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Render Signal Generator is alive and running!")

    def do_POST(self):
        try:
            # بررسی توکن امنیتی دریافتی از همروش
            auth_token = self.headers.get("X-Secret-Token")
            config = Config()
            
            if config.SECRET_TOKEN and auth_token != config.SECRET_TOKEN:
                self.send_response(403)
                self.end_headers()
                return

            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            # اگر همروش گزارش انجام معامله داد، به پی‌وی شخصی ارسال کن
            if data.get("action") == "new_trade":
                symbol = data.get("symbol")
                side = data.get("side")
                price = data.get("price")
                trend = data.get("trend")
                
                msg = (
                    f"🚨 **سیگنال و اجرای معامله جدید (اسپات)** 🚨\n\n"
                    f"💎 نماد: `{symbol}`\n"
                    f"📊 نوع پوزیشن: **{side}**\n"
                    f"💵 قیمت ورود: `{price}`\n"
                    f"📈 روند کلی (4h): `{trend}`\n"
                    f"🏷 صرافی: `تبدیل (Tabdeal)`"
                )
                
                # ارسال به پی‌وی شخصی
                TelegramNotifier.send_to_personal(msg)

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

# ==================== ارسال‌کننده پیام به تلگرام (تفکیک کانال و پی‌وی) ====================
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

# ==================== صرافی عمومی (برای دانلود دیتا بدون نیاز به API Key) ====================
class PublicMarketDataFetcher:
    def __init__(self):
        try:
            self.exchange = ccxt.binance({
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'}
            })
        except Exception as e:
            logger.error(f"خطا در ایجاد اتصال عمومی صرافی: {e}")
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

# ==================== سیستم اصلی رندر ====================
class RenderSignalSystem:
    def __init__(self):
        self.config = Config()
        self.config.validate()
        self.data_fetcher = PublicMarketDataFetcher()
        self.running = True

    def send_to_hamravesh(self, symbol: str, ohlcv: list):
        if not self.config.HAMRAVESH_WEBHOOK_URL:
            return
        try:
            payload = {
                "symbol": symbol,
                "ohlcv": ohlcv
            }
            headers = {"X-Secret-Token": self.config.SECRET_TOKEN}
            response = requests.post(
                self.config.HAMRAVESH_WEBHOOK_URL, 
                json=payload, 
                headers=headers, 
                timeout=15
            )
            if response.status_code != 200:
                logger.warning(f"ارسال به همروش با کد وضعیت نامعتبر مواجه شد: {response.status_code}")
        except Exception as e:
            logger.error(f"خطا در ارسال داده بازار به همروش برای {symbol}: {e}")

    def run_loop(self):
        logger.info("بخش رندر (Render Signal Generator) با موفقیت فعال شد.")
        TelegramNotifier.send_to_personal("🚀 ربات رندر (تحلیلگر و ارسال‌کننده داده) با موفقیت روشن شد.")

        while self.running:
            for symbol in self.config.SYMBOLS:
                try:
                    ohlcv_15m = self.data_fetcher.fetch_ohlcv(symbol, self.config.ENTRY_TIMEFRAME, limit=120)
                    
                    if ohlcv_15m:
                        self.send_to_hamravesh(symbol, ohlcv_15m)
                    
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
