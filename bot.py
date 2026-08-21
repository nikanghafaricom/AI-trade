# ==============================================
# Hybrid Signal Bot - (V5.1 Auto-Proxy Iran Edition)
# ==============================================
import os
import time
import logging
import requests
import gc
import json
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from typing import Dict, Optional, List
import pandas as pd
import ccxt
from dotenv import load_dotenv

load_dotenv()

# ==================== وب‌سرور جهت نگه داشتن زنده Render ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot is alive and proxy engine is active!")

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
    EXCHANGE_ID = os.getenv("EXCHANGE_ID", "tabdil")
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

# ==================== ماژول هوشمند دریافت و تست خودکار پروکسی ایران ====================
class AutoProxyManager:
    def __init__(self):
        self.working_proxy: Optional[Dict[str, str]] = None
        self.last_update = datetime.min

    def fetch_iran_proxies(self) -> List[str]:
        """استخراج پروکسی‌های ایران از منابع رایگان عمومی"""
        proxies = []
        sources = [
            "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=IR&ssl=all&anonymity=all",
            "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
            "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"
        ]
        
        for url in sources:
            try:
                res = requests.get(url, timeout=5)
                if res.status_code == 200:
                    found = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}:\d+\b', res.text)
                    proxies.extend(found)
            except Exception:
                continue
                
        return list(set(proxies))

    def test_proxy(self, proxy_address: str) -> bool:
        """تست اینکه آیا پروکسی می‌تواند به API تبدیل وصل شود یا خیر"""
        proxy_dict = {
            "http": f"http://{proxy_address}",
            "https": f"http://{proxy_address}"
        }
        try:
            # ارسال یک درخواست سریع به صرافی تبدیل برای سنجش اتصال
            res = requests.get("https://api.tabdil.org/p2p/v1/ticker?symbol=BTC_USDT", proxies=proxy_dict, timeout=4)
            return res.status_code == 200
        except Exception:
            return False

    def get_valid_proxy(self, force_refresh=False) -> Optional[Dict[str, str]]:
        """دریافت پروکسی سالم (با قابلیت رفرش خودکار)"""
        now = datetime.now()
        
        # اگر پروکسی موجود است و کمتر از ۱ ساعت گذشته، از همان استفاده کن
        if self.working_proxy and not force_refresh and (now - self.last_update < timedelta(hours=1)):
            return self.working_proxy

        logger.info("🔍 در حال جستجو و پیدا کردن پروکسی جدید ایران...")
        proxy_candidates = self.fetch_iran_proxies()
        
        for proxy_str in proxy_candidates:
            if self.test_proxy(proxy_str):
                logger.info(f"✅ پروکسی سالم ایران پیدا شد: {proxy_str}")
                self.working_proxy = {
                    "http": f"http://{proxy_str}",
                    "https": f"http://{proxy_str}"
                }
                self.last_update = now
                return self.working_proxy

        logger.warning("⚠️ هیچ پروکسی رایگان سالمی در این لحظه پیدا نشد. سیستم بدون پروکسی تلاش خواهد کرد.")
        self.working_proxy = None
        return None

proxy_manager = AutoProxyManager()

# ==================== کلاس API صرافی تبدیل (با پروکسی خودکار) ====================
class TabdilExchange:
    def __init__(self, api_key: str, secret: str):
        self.api_key = api_key
        self.secret = secret
        self.base_url = "https://api.tabdil.org"
        self.headers = {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json"
        }

    def _symbol_transform(self, symbol: str) -> str:
        return symbol.replace("/", "_")

    def _make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """ارسال درخواست با مدیریت خودکار خطای پروکسی"""
        proxies = proxy_manager.get_valid_proxy()
        try:
            res = requests.request(method, url, headers=self.headers, proxies=proxies, timeout=10, **kwargs)
            return res
        except Exception as e:
            logger.warning(f"خطای ارتباط با پروکسی فعلی ({e}). تلاش مجدد برای تعویض پروکسی...")
            # در صورت بروز خطا، پروکسی اجباراً رفرش و تعویض می‌شود
            new_proxies = proxy_manager.get_valid_proxy(force_refresh=True)
            return requests.request(method, url, headers=self.headers, proxies=new_proxies, timeout=10, **kwargs)

    def fetch_balance(self) -> dict:
        url = f"{self.base_url}/p2p/v1/user/balances"
        try:
            res = self._make_request("GET", url)
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
        try:
            res = self._make_request("GET", url)
            data = res.json()
            last_price = float(data.get("lastPrice", 0)) if res.status_code == 200 else 0.0
            return {"last": last_price}
        except Exception:
            return {"last": 0.0}

    def create_market_buy_order(self, symbol: str, amount: float) -> dict:
        market = self._symbol_transform(symbol)
        url = f"{self.base_url}/p2p/v1/order"
        payload = {
            "symbol": market,
            "side": "BUY",
            "type": "MARKET",
            "quantity": amount
        }
        res = self._make_request("POST", url, json=payload)
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
        res = self._make_request("POST", url, json=payload)
        data = res.json()
        return {"status": "closed", "raw": data}

# ==================== لایه داده ====================
class DataLayer:
    def __init__(self, config: Config):
        self.config = config
        self.market_data_exchange = ccxt.coinex({'enableRateLimit': True})
        
        exchange_id = config.EXCHANGE_ID.lower()
        if exchange_id == "tabdil":
            self.exchange = TabdilExchange(config.API_KEY, config.SECRET)
        else:
            exchange_class = getattr(ccxt, exchange_id)
            self.exchange = exchange_class({
                'apiKey': config.API_KEY,
                'secret': config.SECRET,
                'password': config.PASSWORD,
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'}
            })

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
        ohlcv = self.market_data_exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
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
            balance = self.data_layer.exchange.fetch_balance()
            return float(balance.get('USDT', {}).get('free', 0.0))
        except Exception as e:
            logger.error(f"خطا در دریافت موجودی صرافی: {e}")
            return 0.0

    def _get_total_balance_value(self) -> float:
        try:
            balance = self.data_layer.exchange.fetch_balance()
            total_usdt = float(balance.get('USDT', {}).get('total', 0.0))
            for currency, data in balance.get('total', {}).items():
                if currency != 'USDT' and data > 0:
                    try:
                        ticker = self.data_layer.exchange.fetch_ticker(f"{currency}/USDT")
                        total_usdt += data * ticker['last']
                    except Exception:
                        pass
            return round(total_usdt, 2)
        except Exception as e:
            logger.error(f"خطا در دریافت کل دارایی: {e}")
            return 0.0

    def open_real_trade(self, symbol: str, side: str, entry_price: float, tp1: float, tp2: float, tp3: float, sl: float):
        if side != "BUY":
            logger.info(f"سیگنال {side} برای {symbol} به دلیل Spot بودن سیستم نادیده گرفته شد (بدون اهرم).")
            return

        free_usdt = self._get_usdt_balance()
        allocated_usdt = free_usdt / self.config.MAX_CONCURRENT_TRADES

        if allocated_usdt < 5.0:
            logger.warning(f"موجودی کافی برای ورود به معامله واقعی نیست: {allocated_usdt:.2f} USDT")
            return

        try:
            amount = allocated_usdt / entry_price
            order = self.data_layer.exchange.create_market_buy_order(symbol, amount)
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
            logger.info(f"معامله واقعی اسپات ثبت شد: {symbol} | مقدار: {executed_amount}")
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
                        self.data_layer.exchange.create_market_sell_order(symbol, amount)
                    except Exception as order_err:
                        logger.error(f"خطا در فروش سفارش مارکت {symbol}: {order_err}")

                    pnl = ((close_price - entry) / entry) * 100
                    total_balance = self._get_total_balance_value()
                    
                    self._send_close_report(trade, pnl, total_balance)
                    del self.active_trades[trade_id]

            except Exception as e:
                logger.error(f"خطا در بروزرسانی معامله واقعی {trade_id}: {e}")

        self._save_trades()

    def _send_close_report(self, trade: Dict, pnl: float, total_balance: float):
        emoji = "✅" if pnl > 0 else "❌"
        msg = f"""
{emoji} **معامله واقعی بسته شد**

📌 **ارز:** {trade['symbol']} (SPOT BUY)
📈 **سود/زیان معامله:** {pnl:+.2f}%
💰 **موجودی کل حساب (با سود/زیان):** {total_balance:,} USDT
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
            logger.error(f"خطای ارسال پیام شخصی به تلگرام: {e}")

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
            trailing_step = round(price + (1.0 * risk), 4)
        else:
            stop_loss = max(float(latest['resistance']), price + (1.3 * atr))
            risk = stop_loss - price
            tp1 = round(price - (1.5 * risk), 4)
            tp2 = round(price - (2.5 * risk), 4)
            tp3 = round(price - (4.2 * risk), 4)
            stop_loss = round(stop_loss, 4)
            trailing_step = round(price - (1.0 * risk), 4)

        message = f"""
{emoji} **ULTRA SIGNAL: {side} / {direction}**

📍 **Symbol:** {symbol}
⏱ **Timeframe:** {self.config.ENTRY_TIMEFRAME} (Trend 4H: {trend_4h})

💵 **Entry Price:** {price:,}

🎯 **Dynamic Targets:**
  1️⃣ TP1: {tp1:,}
  2️⃣ TP2: {tp2:,}
  3️⃣ TP3 (Max Yield): {tp3:,}

🛑 **Stop-Loss:** {stop_loss:,}
⚙️ **Trailing Stop Trigger:** Move SL to Entry at {trailing_step:,}

📊 **Metrics:** RSI: {latest['rsi']:.1f}
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
            logger.info(f"سیگنال فوق‌پیشرفته {side} برای {symbol} ارسال شد")
            
            return {
                "price": price,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
                "sl": stop_loss
            }
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
        logger.info("----- شروع آنالیز پیشرفته بازار -----")
        for symbol in self.config.SYMBOLS:
            self.process_symbol(symbol)
            time.sleep(1.5)
            
        self.live_trader.update_and_check_trades()

    def start(self):
        logger.info("بات V5.1 Auto-Proxy فعال شد")
        start_message = "⚡️ **نسخه V5.1 Auto-Proxy فعال شد.**\n\nربات به‌صورت خودکار پروکسی‌های سالم ایران را شناسایی کرده و ارتباط با صرافی تبدیل را برقرار می‌سازد."
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
