# ==============================================
# Render Server - Data Relay & Telegram Channel
# ==============================================
import os
import time
import logging
import requests
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta
import pandas as pd
import ccxt
from dotenv import load_dotenv

load_dotenv()

# ==================== تنظیمات ====================
class Config:
    EXCHANGE_ID = "coinex"
    API_KEY = os.getenv("EXCHANGE_API_KEY", "")
    SECRET = os.getenv("EXCHANGE_SECRET", "")
    
    # آدرس وب‌هوک همروش که قبلاً ساختید
    HAMRAVESH_WEBHOOK_URL = os.getenv("HAMRAVESH_WEBHOOK_URL", "https://autotrade.darkube.ir/webhook")

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    PERSONAL_CHAT_ID = os.getenv("PERSONAL_CHAT_ID")

    SYMBOLS = [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
        "AVAX/USDT", "NEAR/USDT", "ADA/USDT", "DOGE/USDT", "LINK/USDT",
    ]

    ENTRY_TIMEFRAME = "15m"
    TREND_TIMEFRAME = "4h"
    CHECK_INTERVAL = 300

# ==================== لاگ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ==================== وب‌سرور رندر (برای دریافت سیگنال/گزارش از همروش) ====================
class RenderWebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            data = json.loads(post_data.decode('utf-8'))
            action = data.get("action")
            
            # اگر همروش سیگنال فرستاد تا به کانال تلگرام بفرستیم
            if action == "send_channel_signal":
                msg = data.get("message")
                telegram_sender.send_telegram_message(config.TELEGRAM_CHAT_ID, msg)
            
            # اگر گزارش معامله واقعی بود که باید به پی‌وی بفرستد
            elif action == "send_pv_report":
                msg = data.get("message")
                target_id = config.PERSONAL_CHAT_ID or config.TELEGRAM_CHAT_ID
                telegram_sender.send_telegram_message(target_id, msg)

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception as e:
            logger.error(f"خطا در پردازش وب‌هوک در رندر: {e}")
            self.send_response(500)
            self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Render Relay Server is running!")

    def log_message(self, format, *args):
        return

def start_render_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), RenderWebhookHandler)
    server.serve_forever()

# ==================== ارتباط با تلگرام ====================
class TelegramSender:
    def __init__(self, config: Config):
        self.base_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

    def send_telegram_message(self, chat_id: str, text: str):
        try:
            requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10
            )
        except Exception as e:
            logger.error(f"خطای ارسال تلگرام: {e}")

# ==================== لایه داده و ارسال به همروش ====================
class RenderPipeline:
    def __init__(self, config: Config):
        self.config = config
        exchange_class = getattr(ccxt, config.EXCHANGE_ID)
        self.exchange = exchange_class({
            'apiKey': config.API_KEY,
            'secret': config.SECRET,
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })

    def fetch_and_send(self):
        logger.info("----- جمع‌آوری داده‌ها و ارسال به همروش -----")
        for symbol in self.config.SYMBOLS:
            try:
                ohlcv_15m = self.exchange.fetch_ohlcv(symbol, timeframe=self.config.ENTRY_TIMEFRAME, limit=100)
                ohlcv_4h = self.exchange.fetch_ohlcv(symbol, timeframe=self.config.TREND_TIMEFRAME, limit=100)

                payload = {
                    "symbol": symbol,
                    "ohlcv_15m": ohlcv_15m,
                    "ohlcv_4h": ohlcv_4h
                }

                # ارسال داده‌ها به وب‌هوک همروش
                requests.post(self.config.HAMRAVESH_WEBHOOK_URL, json=payload, timeout=15)
                time.sleep(1)
            except Exception as e:
                logger.error(f"خطا در ارسال داده {symbol} به همروش: {e}")

    def run(self):
        while True:
            self.fetch_and_send()
            time.sleep(self.config.CHECK_INTERVAL)

if __name__ == "__main__":
    config = Config()
    telegram_sender = TelegramSender(config)
    
    # استارت وب‌سرور برای گرفتن پیام‌ها از همروش
    threading.Thread(target=start_render_server, daemon=True).start()
    
    # استارت حلقه ارسال داده به همروش
    pipeline = RenderPipeline(config)
    pipeline.run()
