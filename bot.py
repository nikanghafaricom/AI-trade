# ==============================================
# Hybrid Signal Bot - نسخه ترکیبی (CCXT Data + Tabdeal Execution)
# ==============================================
import os
import time
import logging
import requests
import gc
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from typing import Dict, Optional, List
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
        self.wfile.write(b"Bot is alive and running!")

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
    # داده‌ها از کوینکس یا بایننس گرفته می‌شود تا خطا ندهد
    DATA_EXCHANGE_ID = os.getenv("DATA_EXCHANGE_ID", "coinex")
    
    # معامله روی صرافی تبدیل انجام می‌شود
    API_KEY = os.getenv("EXCHANGE_API_KEY", "")
    SECRET = os.getenv("EXCHANGE_SECRET", "")
    PASSWORD = os.getenv("EXCHANGE_PASSWORD", "")

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    PERSONAL_CHAT_ID = os.getenv("PERSONAL_CHAT_ID")

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
    CHECK_INTERVAL = 300
    MAX_CONCURRENT_TRADES = 4

    def validate(self):
        required = {
            "TELEGRAM_BOT_TOKEN": self.TELEGRAM_BOT_TOKEN,
            "TELEGRAM_CHAT_ID": self.TELEGRAM_CHAT_ID,
            "EXCHANGE_API_KEY": self.API_KEY,
            "EXCHANGE_SECRET": self.SECRET,
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

# ==================== کلاس صرافی تبدیل (فقط برای معامله و موجودی) ====================
class TabdilExchange:
    def __init__(self, api_key: str, secret: str):
        self.api_key = api_key
        self.secret = secret
        self.base_url = "https://api.tabdeal.org"
        self.headers = {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json"
        }

    def _symbol_transform(self, symbol: str) -> str:
        return symbol.replace("/", "_")

    def fetch_balance(self) -> dict:
        url = f"{self.base_url}/p2p/v1/user/balances"
        try:
            res = requests.get(url, headers=self.headers, timeout=10)
            data = res.json()
            balances = {"USDT": {"free": 0.0, "total": 0.0}, "total": {}}
            if res.status_code == 200 and "data" in data:
                for item in data["data"]:
                    asset = item.get("asset")
                    free = float(item.get("free", 0))
                    locked = float(item.get("locked", 0))
                    total = free + locked
                    balances["total"][asset] = total
                    if asset == "USDT":
                        balances["USDT"] = {"free": free, "total": total}
            return balances
        except Exception as e:
            logger.error(f"خطا در دریافت موجودی تبدیل: {e}")
            return {"USDT": {"free": 0.0, "total": 0.0}, "total": {}}

    def fetch_ticker(self, symbol: str) -> dict:
        market = self._symbol_transform(symbol)
        url = f"{self.base_url}/p2p/v1/ticker?symbol={market}"
        res = requests.get(url, timeout=10)
        data = res.json()
        last_price = float(data.get("lastPrice", 0)) if res.status_code == 200 else 0.0
        return {"last": last_price}

    def create_market_buy_order(self, symbol: str, amount: float) -> dict:
        market = self._symbol_transform(symbol)
        url = f"{self.base_url}/p2p/v1/order"
        payload = {
            "symbol": market,
            "side": "BUY",
            "type": "MARKET",
            "quantity": amount
        }
        res = requests.post(url, json=payload, headers=self.headers, timeout=10)
        data = res.json()
        ticker = self.fetch_ticker(symbol)
        return {
            "average": ticker.get("last", 0),
            "filled": amount,
            "raw": data
        }

    def create_market_sell_order(self, symbol: str, amount: float) -> dict:
        market = self._symbol_transform(symbol)
        url = f"{self.base_url}/p2p/v1/order"
        payload = {
            "symbol": market,
            "side": "SELL",
            "type": "MARKET",
            "quantity": amount
        }
        res = requests.post(url, json=payload, headers=self.headers, timeout=10)
        data = res.json()
        return {"status": "closed", "raw": data}

# ==================== لایه داده (استفاده از CCXT برای کندل‌ها) ====================
class DataLayer:
    def __init__(self, config: Config):
        self.config = config
        exchange_class = getattr(ccxt, config.DATA_EXCHANGE_ID)
        self.data_exchange = exchange_class({
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'},
            'timeout': 30000,
            'headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
        })
        self.trade_exchange = TabdilExchange(config.API_KEY, config.SECRET)

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
        ohlcv = self.data_exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df

# ==================== لایه تحلیل ====================
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

    def get_major_trend(self, df_4h: pd.DataFrame) -> str:
        latest = df_4h.iloc[-1]
        if latest['close'] > latest['ema_trend'] and latest['ema_fast'] > latest['ema_slow']:
            return "BULLISH"
        elif latest['close'] < latest['ema_trend'] and latest['ema_fast'] < latest['ema_slow']:
            return "BEARISH"
        return "NEUTRAL"

# ==================== موتور سیگنال ====================
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

# ==================== ماژول معامله واقعی اسپات ====================
class LiveSpotTrader:
    def __init__(self, config: Config, data_layer: DataLayer, telegram_sender):
        self.config = config
        self.data_layer = data_layer
        self.telegram = telegram_sender
        self.file_path = "live_trades.json"
        self.active_trades = self._load_trades()

    def _load_trades(self) -> Dict:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_trades(self):
        with open(self.file_path, "w") as f:
            json.dump(self.active_trades, f, indent=4)

    def _get_usdt_balance(self) -> float:
        try:
            balance = self.data_layer.trade_exchange.fetch_balance()
            return float(balance.get('USDT', {}).get('free', 0.0))
        except Exception as e:
            logger.error(f"خطا در دریافت موجودی صرافی تبدیل: {e}")
            return 0.0

    def _get_total_balance_value(self) -> float:
        try:
            balance = self.data_layer.trade_exchange.fetch_balance()
            total_usdt = float(balance.get('USDT', {}).get('total', 0.0))
            for currency, data in balance.get('total', {}).items():
                if currency != 'USDT' and data > 0:
                    try:
                        ticker = self.data_layer.trade_exchange.fetch_ticker(f"{currency}/USDT")
                        total_usdt += data * ticker['last']
                    except Exception:
                        pass
            return round(total_usdt, 2)
        except Exception as e:
            logger.error(f"خطا در دریافت کل دارایی: {e}")
            return 0.0

    def open_real_trade(self, symbol: str, side: str, entry_price: float, tp1: float, tp2: float, tp3: float, sl: float):
        if side != "BUY":
            return

        free_usdt = self._get_usdt_balance()
        allocated_usdt = free_usdt / self.config.MAX_CONCURRENT_TRADES

        if allocated_usdt < 5.0:
            logger.warning(f"موجودی کافی نیست: {allocated_usdt:.2f} USDT")
            return

        try:
            amount = allocated_usdt / entry_price
            order = self.data_layer.trade_exchange.create_market_buy_order(symbol, amount)
            executed_price = float(order.get('average', entry_price))
            executed_amount = float(order.get('filled', amount))

            trade_id = f"{symbol}_{int(time.time())}"
            self.active_trades[trade_id] = {
                "symbol": symbol,
                "side": side,
                "entry": executed_price,
                "amount": executed_amount,
                "allocated_usdt": allocated_usdt,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
                "sl": sl,
                "open_time": datetime.now().strftime('%Y-%m-%d %H:%M')
            }
            self._save_trades()
            logger.info(f"معامله واقعی ثبت شد: {symbol}")
        except Exception as e:
            logger.error(f"خطا در ثبت سفارش خرید واقعی برای {symbol}: {e}")

    def update_and_check_trades(self):
        for trade_id, trade in list(self.active_trades.items()):
            try:
                symbol = trade['symbol']
                df = self.data_layer.fetch_ohlcv(symbol, timeframe="1m", limit=5)
                latest_high = float(df['high'].max())
                latest_low = float(df['low'].min())
                entry = trade['entry']
                amount = trade['amount']

                is_close = False
                close_price = entry

                if latest_low <= trade['sl']:
                    is_close = True
                    close_price = trade['sl']
                elif latest_high >= trade['tp1']:
                    is_close = True
                    close_price = trade['tp1']

                if is_close:
                    try:
                        self.data_layer.trade_exchange.create_market_sell_order(symbol, amount)
                    except Exception as order_err:
                        logger.error(f"خطا در فروش مارکت {symbol}: {order_err}")

                    pnl = ((close_price - entry) / entry) * 100
                    total_balance = self._get_total_balance_value()
                    self._send_close_report(trade, pnl, total_balance)
                    del self.active_trades[trade_id]

            except Exception as e:
                logger.error(f"خطا در بروزرسانی معامله {trade_id}: {e}")

        self._save_trades()

    def _send_close_report(self, trade: Dict, pnl: float, total_balance: float):
        emoji = "✅" if pnl > 0 else "❌"
        msg = f"""
{emoji} **معامله واقعی بسته شد**

📌 **ارز:** {trade['symbol']} (SPOT BUY)
📈 **سود/زیان:** {pnl:+.2f}%
💰 **موجودی کل حساب:** {total_balance:,} USDT
"""
        self.telegram.send_personal_message(msg)

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
            logger.error(f"خطای ارسال پیام به تلگرام: {e}")

    def send_personal_message(self, text: str):
        target_id = self.config.PERSONAL_CHAT_ID or self.config.TELEGRAM_CHAT_ID
        try:
            requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": target_id,
                    "text": text,
                    "parse_mode": "Markdown"
                },
                timeout=10
            )
        except Exception as e:
            logger.error(f"خطای ارسال پیام شخصی: {e}")

    def send_signal(self, symbol: str, side: str, latest: pd.Series, trend_4h: str) -> Dict:
        emoji = "🟢" if side == "BUY" else "🔴"
        direction = "LONG" if side == "BUY" else "SHORT"
        price = float(latest['close'])
        atr = float(latest['atr']) if not pd.isna(latest['atr']) else price * 0.01

        if side == "BUY":
            stop_loss = min(float(latest['support']), price - (1.3 * atr))
            risk = price - stop_loss
            tp1 = round(price + (1.5 * risk), 4)
            tp2 = round(price + (2.5 * risk), 4)
            tp3 = round(price + (4.2 * risk), 4)
            stop_loss = round(stop_loss, 4)
        else:
            stop_loss = max(float(latest['resistance']), price + (1.3 * atr))
            risk = stop_loss - price
            tp1 = round(price - (1.5 * risk), 4)
            tp2 = round(price - (2.5 * risk), 4)
            tp3 = round(price - (4.2 * risk), 4)
            stop_loss = round(stop_loss, 4)

        message = f"""
{emoji} **ULTRA SIGNAL: {side} / {direction}**

📍 **Symbol:** {symbol}
⏱ **Timeframe:** {self.config.ENTRY_TIMEFRAME} (Trend 4H: {trend_4h})

💵 **Entry Price:** {price:,}

🎯 **Dynamic Targets:**
  1️⃣ TP1: {tp1:,}
  2️⃣ TP2: {tp2:,}
  3️⃣ TP3: {tp3:,}

🛑 **Stop-Loss:** {stop_loss:,}
📊 **RSI:** {latest['rsi']:.1f}
⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
        try:
            requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.config.TELEGRAM_CHAT_ID,
                    "text": message,
                    "parse_mode": "Markdown"
                },
                timeout=10
            )
            return {"price": price, "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": stop_loss}
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
        self.signal_engine = SignalEngine(self.config)
        self.telegram = TelegramSender(self.config)
        self.live_trader = LiveSpotTrader(self.config, self.data, self.telegram)
        self.running = True
        self.last_signal_time: Dict[str, datetime] = {}

    def process_symbol(self, symbol: str):
        try:
            df_15m = self.data.fetch_ohlcv(symbol, timeframe=self.config.ENTRY_TIMEFRAME)
            df_15m = self.analysis.calculate_indicators(df_15m)

            df_4h = self.data.fetch_ohlcv(symbol, timeframe=self.config.TREND_TIMEFRAME)
            df_4h = self.analysis.calculate_indicators(df_4h)

            trend_4h = self.analysis.get_major_trend(df_4h)
            rule_signal = self.signal_engine.get_rule_signal(df_15m, trend_4h)
            if not rule_signal:
                return

            now = datetime.now()
            if symbol in self.last_signal_time:
                if now - self.last_signal_time[symbol] < timedelta(minutes=90):
                    return

            latest = df_15m.iloc[-1]
            trade_data = self.telegram.send_signal(symbol, rule_signal, latest, trend_4h)
            
            if trade_data:
                self.live_trader.open_real_trade(
                    symbol=symbol,
                    side=rule_signal,
                    entry_price=trade_data["price"],
                    tp1=trade_data["tp1"],
                    tp2=trade_data["tp2"],
                    tp3=trade_data["tp3"],
                    sl=trade_data["sl"]
                )

            self.last_signal_time[symbol] = now

        except Exception as e:
            logger.error(f"خطا در پردازش {symbol}: {e}")

    def run_once(self):
        logger.info("----- شروع آنالیز بازار -----")
        for symbol in self.config.SYMBOLS:
            self.process_symbol(symbol)
            time.sleep(1.5)
            
        self.live_trader.update_and_check_trades()

    def start(self):
        logger.info("بات فعال شد")
        start_message = "⚡️ **ربات با موفقیت راه‌اندازی شد.**\n\nتحلیل تکنیکال و کندل‌ها از طریق منبع پایدار و معاملات از صرافی تبدیل انجام می‌شود."
        self.telegram.send_system_status(start_message)

        while self.running:
            self.run_once()
            gc.collect()
            time.sleep(self.config.CHECK_INTERVAL)

    def stop(self):
        self.running = False

if __name__ == "__main__":
    bot = HybridTradingSystem()
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.stop()
