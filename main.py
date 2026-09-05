# ==============================================
# Hybrid Signal Bot - نسخه جامع نهایی (V5 + Groq + Safe Clamping)
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
    EXCHANGE_ID = "coinex"
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

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
        ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
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

# ==================== ماژول هوش مصنوعی گروک (با محدوده‌بندی سخت‌گیرانه کدی) ====================
class AIParameterOptimizer:
    def __init__(self, config):
        self.config = config
        self.last_optimized_time = None
        self.optimization_interval = timedelta(hours=10)
        
        self.groq_api_key = os.getenv("GROQ_API_KEY", "")
        self.groq_endpoint = "https://api.groq.com/openai/v1/chat/completions"
        
        # پارامترهای پیش‌فرض پایه
        self.dynamic_params = {
            "rsi_buy_min": 42,
            "rsi_buy_max_range_start": 48,
            "rsi_buy_max_range_end": 65,
            "rsi_sell_max": 58,
            "rsi_sell_min_range_start": 35,
            "rsi_sell_min_range_end": 52,
            "volume_mult": 0.50,
            "atr_min_filter": 0.0015,
            "cooldown_minutes": 90,
            "sl_atr_mult": 1.3,
            "tp1_mult": 1.5,
            "tp2_mult": 2.5,
            "tp3_mult": 4.2,
            "trailing_mult": 1.0
        }

    def validate_and_clamp_params(self, new_params: dict) -> dict:
        """محدود کردن بازه‌ای پارامترها در داخل خود کد برای جلوگیری از خطای ربات"""
        clamped = {}
        
        clamped["rsi_buy_min"] = max(30, min(float(new_params.get("rsi_buy_min", 42)), 50))
        clamped["rsi_buy_max_range_start"] = max(40, min(float(new_params.get("rsi_buy_max_range_start", 48)), 55))
        clamped["rsi_buy_max_range_end"] = max(55, min(float(new_params.get("rsi_buy_max_range_end", 65)), 75))
        
        clamped["rsi_sell_max"] = max(50, min(float(new_params.get("rsi_sell_max", 58)), 70))
        clamped["rsi_sell_min_range_start"] = max(25, min(float(new_params.get("rsi_sell_min_range_start", 35)), 45))
        clamped["rsi_sell_min_range_end"] = max(40, min(float(new_params.get("rsi_sell_min_range_end", 52)), 60))
        
        clamped["volume_mult"] = max(0.2, min(float(new_params.get("volume_mult", 0.50)), 1.5))
        clamped["atr_min_filter"] = max(0.0005, min(float(new_params.get("atr_min_filter", 0.0015)), 0.005))
        clamped["cooldown_minutes"] = max(30, min(int(new_params.get("cooldown_minutes", 90)), 360))
        
        clamped["sl_atr_mult"] = max(0.8, min(float(new_params.get("sl_atr_mult", 1.3)), 2.5))
        clamped["tp1_mult"] = max(1.0, min(float(new_params.get("tp1_mult", 1.5)), 3.0))
        clamped["tp2_mult"] = max(2.0, min(float(new_params.get("tp2_mult", 2.5)), 5.0))
        clamped["tp3_mult"] = max(3.5, min(float(new_params.get("tp3_mult", 4.2)), 8.0))
        clamped["trailing_mult"] = max(0.5, min(float(new_params.get("trailing_mult", 1.0)), 2.0))
        
        return clamped

    def should_optimize(self) -> bool:
        if self.last_optimized_time is None:
            return True
        return datetime.now() - self.last_optimized_time >= self.optimization_interval

    def gather_market_summary(self, data_layer, analysis_layer) -> dict:
        market_summary = {}
        for symbol in self.config.SYMBOLS:
            try:
                df_15m = data_layer.fetch_ohlcv(symbol, timeframe="15m", limit=50)
                df_15m = analysis_layer.calculate_indicators(df_15m)
                latest = df_15m.iloc[-1]
                
                market_summary[symbol] = {
                    "close": float(latest['close']),
                    "rsi": float(latest['rsi']) if not pd.isna(latest['rsi']) else 50,
                    "atr": float(latest['atr']) if not pd.isna(latest['atr']) else 0,
                    "volume_ratio": float(latest['volume'] / latest['vol_sma']) if not pd.isna(latest['vol_sma']) else 1.0
                }
            except Exception as e:
                logger.error(f"خطا در جمع‌آوری داده {symbol} برای هوش مصنوعی: {e}")
        return market_summary

    def optimize_parameters(self, data_layer, analysis_layer):
        if not self.groq_api_key:
            logger.warning("کلید GROQ_API_KEY تنظیم نشده است. از پارامترهای فعلی استفاده می‌شود.")
            return

        logger.info("در حال ارسال داده‌های بازار به هوش مصنوعی Groq...")
        market_data = self.gather_market_summary(data_layer, analysis_layer)

        prompt = f"""
You are an expert quantitative trading system manager. 
Analyze the current market metrics for multiple cryptocurrency symbols:
{json.dumps(market_data, indent=2)}

Current parameters being used:
{json.dumps(self.dynamic_params, indent=2)}

Optimize these parameters based on market conditions. 
CRITICAL: Return ONLY a valid JSON object containing the updated parameters with the exact same keys. Do not include markdown formatting like ```json or any extra text.
"""

        payload = {
            "model": "llama3-70b-8192",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2
        }

        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
            "Content-Type": "application/json"
        }

        try:
            response = requests.post(self.groq_endpoint, json=payload, headers=headers, timeout=30)
            if response.status_code == 200:
                res_data = response.json()
                content = res_data['choices'][0]['message']['content'].strip()
                
                if content.startswith("```"):
                    content = content.strip("`").replace("json\n", "").strip()

                raw_params = json.loads(content)
                
                # عبور از صافیِ بازه‌های امن کدی
                self.dynamic_params = self.validate_and_clamp_params(raw_params)
                self.last_optimized_time = datetime.now()
                logger.info(f"پارامترها پس از بررسی محدوده امن کدی بروزرسانی شدند: {self.dynamic_params}")
            else:
                logger.error(f"خطای API گروک: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"خطا در پردازش پاسخ گروک: {e}")

# ==================== موتور سیگنال ====================
class SignalEngine:
    def __init__(self, config: Config, ai_optimizer: AIParameterOptimizer):
        self.config = config
        self.ai_optimizer = ai_optimizer

    def get_rule_signal(self, df_15m: pd.DataFrame, trend_4h: str) -> Optional[str]:
        latest = df_15m.iloc[-1]
        prev = df_15m.iloc[-2]
        
        p = self.ai_optimizer.dynamic_params

        if pd.isna(latest['rsi']) or pd.isna(latest['ema_fast']) or pd.isna(latest['atr']):
            return None

        if latest['atr'] < (latest['close'] * p["atr_min_filter"]):
            return None

        volume_confirmed = latest['volume'] > (latest['vol_sma'] * p["volume_mult"])

        if trend_4h in ["BULLISH", "NEUTRAL"]:
            ema_bull = latest['ema_fast'] > latest['ema_slow']
            rsi_buy = (latest['rsi'] > p["rsi_buy_min"] and prev['rsi'] <= p["rsi_buy_min"]) or (p["rsi_buy_max_range_start"] <= latest['rsi'] <= p["rsi_buy_max_range_end"] and latest['rsi'] > prev['rsi'])
            if ema_bull and rsi_buy and volume_confirmed:
                return "BUY"

        if trend_4h in ["BEARISH", "NEUTRAL"]:
            ema_bear = latest['ema_fast'] < latest['ema_slow']
            rsi_sell = (latest['rsi'] < p["rsi_sell_max"] and prev['rsi'] >= p["rsi_sell_max"]) or (p["rsi_sell_min_range_start"] <= latest['rsi'] <= p["rsi_sell_min_range_end"] and latest['rsi'] < prev['rsi'])
            if ema_bear and rsi_sell and volume_confirmed:
                return "SELL"

        return None

# ==================== ماژول معامله مجازی (Paper Trading) ====================
class PaperTrader:
    def __init__(self, config: Config, telegram_sender):
        self.config = config
        self.telegram = telegram_sender
        self.file_path = "paper_trades.json"
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

    def open_virtual_trade(self, symbol: str, side: str, entry_price: float, tp1: float, tp2: float, tp3: float, sl: float):
        trade_id = f"{symbol}_{int(time.time())}"
        self.active_trades[trade_id] = {
            "symbol": symbol,
            "side": side,
            "entry": entry_price,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "sl": sl,
            "open_time": datetime.now().strftime('%Y-%m-%d %H:%M')
        }
        self._save_trades()

    def update_and_check_trades(self, data_layer: DataLayer):
        for trade_id, trade in list(self.active_trades.items()):
            try:
                df = data_layer.fetch_ohlcv(trade['symbol'], timeframe="1m", limit=5)
                latest_high = float(df['high'].max())
                latest_low = float(df['low'].min())

                side = trade['side']
                entry = trade['entry']

                if side == "BUY":
                    if latest_low <= trade['sl']:
                        pnl = ((trade['sl'] - entry) / entry) * 100
                        self._send_close_report(trade, pnl)
                        del self.active_trades[trade_id]
                        continue
                    elif latest_high >= trade['tp1']:
                        pnl = ((trade['tp1'] - entry) / entry) * 100
                        self._send_close_report(trade, pnl)
                        del self.active_trades[trade_id]
                        continue

                elif side == "SELL":
                    if latest_high >= trade['sl']:
                        pnl = ((entry - trade['sl']) / entry) * 100
                        self._send_close_report(trade, pnl)
                        del self.active_trades[trade_id]
                        continue
                    elif latest_low <= trade['tp1']:
                        pnl = ((entry - trade['tp1']) / entry) * 100
                        self._send_close_report(trade, pnl)
                        del self.active_trades[trade_id]
                        continue

            except Exception as e:
                logger.error(f"خطا در بروزرسانی معامله مجازی {trade_id}: {e}")

        self._save_trades()

    def _send_close_report(self, trade: Dict, pnl: float):
        emoji = "✅" if pnl > 0 else "❌"
        msg = f"""
{emoji} **معامله بسته شد**

📌 **ارز:** {trade['symbol']} ({trade['side']})
📈 **سود/زیان:** {pnl:+.2f}%
"""
        self.telegram.send_personal_message(msg)

# ==================== ارسال تلگرام ====================
class TelegramSender:
    def __init__(self, config: Config, ai_optimizer: AIParameterOptimizer):
        self.config = config
        self.ai_optimizer = ai_optimizer
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

    def send_signal(self, symbol: str, side: str, latest: pd.Series, trend_4h: str, timeframe: str) -> Dict:
        emoji = "🟢" if side == "BUY" else "🔴"
        direction = "LONG" if side == "BUY" else "SHORT"
        price = float(latest['close'])
        atr = float(latest['atr']) if not pd.isna(latest['atr']) else price * 0.01

        p = self.ai_optimizer.dynamic_params

        if side == "BUY":
            stop_loss = min(float(latest['support']), price - (p["sl_atr_mult"] * atr))
            risk = price - stop_loss
            tp1 = round(price + (p["tp1_mult"] * risk), 4)
            tp2 = round(price + (p["tp2_mult"] * risk), 4)
            tp3 = round(price + (p["tp3_mult"] * risk), 4)
            stop_loss = round(stop_loss, 4)
            trailing_step = round(price + (p["trailing_mult"] * risk), 4)
        else:
            stop_loss = max(float(latest['resistance']), price + (p["sl_atr_mult"] * atr))
            risk = stop_loss - price
            tp1 = round(price - (p["tp1_mult"] * risk), 4)
            tp2 = round(price - (p["tp2_mult"] * risk), 4)
            tp3 = round(price - (p["tp3_mult"] * risk), 4)
            stop_loss = round(stop_loss, 4)
            trailing_step = round(price - (p["trailing_mult"] * risk), 4)

        message = f"""
{emoji} **ULTRA SIGNAL (Safe-Clamped AI): {side} / {direction}**

📍 **Symbol:** {symbol}
⏱ **Timeframe:** {timeframe} (Trend 4H: {trend_4h})

💵 **Entry Price:** {price:,}

🎯 **Dynamic Targets:**
  1️⃣ TP1: {tp1:,}
  2️⃣ TP2: {tp2:,}
  3️⃣ TP3 (Max Yield): {tp3:,}

🛑 **Stop-Loss:** {stop_loss:,}
⚙️ **Trailing Stop Trigger:** Move SL to Entry at {trailing_step:,}

📊 **Metrics:** RSI: {latest['rsi']:.1f} | Guardrails Active
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
            logger.info(f"سیگنال امن {side} برای {symbol} ارسال شد")
            
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
        self.ai_optimizer = AIParameterOptimizer(self.config)
        self.signal_engine = SignalEngine(self.config, self.ai_optimizer)
        self.telegram = TelegramSender(self.config, self.ai_optimizer)
        self.paper_trader = PaperTrader(self.config, self.telegram)
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
            cooldown = self.ai_optimizer.dynamic_params.get("cooldown_minutes", 90)
            if symbol in self.last_signal_time:
                if now - self.last_signal_time[symbol] < timedelta(minutes=cooldown):
                    return

            latest = df_15m.iloc[-1]
            
            trade_data = self.telegram.send_signal(symbol, rule_signal, latest, trend_4h, self.config.ENTRY_TIMEFRAME)
            
            if trade_data:
                self.paper_trader.open_virtual_trade(
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
        if self.ai_optimizer.should_optimize():
            self.telegram.send_system_status("🔄 **ارتباط با Groq برای بهینه‌سازی پارامترها (با اعمال مرزهای امن کدی)...**")
            self.ai_optimizer.optimize_parameters(self.data, self.analysis)

        logger.info("----- شروع آنالیز پیشرفته بازار -----")
        for symbol in self.config.SYMBOLS:
            self.process_symbol(symbol)
            time.sleep(1.5)
            
        self.paper_trader.update_and_check_trades(self.data)

    def start(self):
        logger.info("بات V5 Ultimate Pro با بازه‌های امن کدی فعال شد")
        start_message = "⚡️ **نسخه نهایی با محافظت کدی فعال شد.**\n\nهوش مصنوعی پارامترها را تنظیم می‌کند، اما بازه‌ها در خود کد قفل هستند تا ربات نه کور شود و نه متوقف."
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
