# ==============================================
# Hybrid Signal Bot - نسخه ضد ضرر و فوق‌پیشرفته تطبیقی (Anti-Loss & Peak Performance)
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

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 150) -> pd.DataFrame:
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception as e:
            logger.error(f"خطا در دریافت داده {symbol} در تایم‌فریم {timeframe}: {e}")
            return pd.DataFrame()

# ==================== لایه تحلیل و تشخیص رژیم بازار ====================
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
        """تشخیص رژیم بازار - جلوگیری از ترید در بازارهای رنج و فیک"""
        if df_15m.empty or len(df_15m) < 30:
            return False
        latest = df_15m.iloc[-1]
        
        if pd.isna(latest['volume']) or pd.isna(latest['vol_sma']) or latest['vol_sma'] == 0:
            return False
            
        volume_ratio = latest['volume'] / latest['vol_sma']
        # اگر حجم بازار خیلی مرده باشد (کمتر از 0.8 میانگین)، ترید ممنوع
        if volume_ratio < 0.8:
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

# ==================== هوش مصنوعی پیشرفته اختصاصی و ضد ضرر ====================
class AIParameterOptimizer:
    def __init__(self, config):
        self.config = config
        self.groq_api_key = os.getenv("GROQ_API_KEY", "")
        self.groq_endpoint = "https://api.groq.com/openai/"
        
        # سیستم بلک‌لیست و تنبیه هوشمند برای ارزهای ضررده
        self.blacklist: Dict[str, datetime] = {}
        
        self.symbol_states = {}
        for sym in config.SYMBOLS:
            self.symbol_states[sym] = {
                "last_optimized_time": None,
                "consecutive_losses": 0,
                "params": {
                    "rsi_buy_min": 42,
                    "rsi_buy_max_range_start": 48,
                    "rsi_buy_max_range_end": 65,
                    "rsi_sell_max": 58,
                    "rsi_sell_min_range_start": 35,
                    "rsi_sell_min_range_end": 52,
                    "volume_mult": 1.2,
                    "atr_min_filter": 0.0015,
                    "cooldown_minutes": 120,
                    "sl_atr_mult": 1.5,
                    "tp1_mult": 1.5,
                    "tp2_mult": 2.5,
                    "tp3_mult": 4.0,
                    "trailing_mult": 1.0
                }
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
        penalty_hours = min(2 * state["consecutive_losses"], 12)
        self.blacklist[symbol] = datetime.now() + timedelta(hours=penalty_hours)
        logger.warning(f"سیستم ضد ضرر: ارز {symbol} به دلیل ضرر متوالی به مدت {penalty_hours} ساعت مسدود شد.")

    def register_win(self, symbol: str):
        state = self.symbol_states[symbol]
        state["consecutive_losses"] = max(0, state["consecutive_losses"] - 1)

    def validate_and_clamp_params(self, new_params: dict) -> dict:
        clamped = {}
        clamped["rsi_buy_min"] = max(30, min(float(new_params.get("rsi_buy_min", 42)), 50))
        clamped["rsi_buy_max_range_start"] = max(40, min(float(new_params.get("rsi_buy_max_range_start", 48)), 55))
        clamped["rsi_buy_max_range_end"] = max(55, min(float(new_params.get("rsi_buy_max_range_end", 65)), 75))
        
        clamped["rsi_sell_max"] = max(50, min(float(new_params.get("rsi_sell_max", 58)), 70))
        clamped["rsi_sell_min_range_start"] = max(25, min(float(new_params.get("rsi_sell_min_range_start", 35)), 45))
        clamped["rsi_sell_min_range_end"] = max(40, min(float(new_params.get("rsi_sell_min_range_end", 52)), 60))
        
        clamped["volume_mult"] = max(0.9, min(float(new_params.get("volume_mult", 1.2)), 1.8))
        clamped["atr_min_filter"] = max(0.0008, min(float(new_params.get("atr_min_filter", 0.0015)), 0.004))
        clamped["cooldown_minutes"] = max(45, min(int(new_params.get("cooldown_minutes", 120)), 240))
        
        clamped["sl_atr_mult"] = max(1.2, min(float(new_params.get("sl_atr_mult", 1.5)), 2.5))
        clamped["tp1_mult"] = max(1.2, min(float(new_params.get("tp1_mult", 1.5)), 3.0))
        clamped["tp2_mult"] = max(2.0, min(float(new_params.get("tp2_mult", 2.5)), 5.0))
        clamped["tp3_mult"] = max(3.0, min(float(new_params.get("tp3_mult", 4.0)), 8.0))
        clamped["trailing_mult"] = max(0.8, min(float(new_params.get("trailing_mult", 1.0)), 2.0))
        return clamped

    def should_optimize(self, symbol: str) -> bool:
        state = self.symbol_states[symbol]
        if state["last_optimized_time"] is None:
            return True
        return datetime.now() - state["last_optimized_time"] >= self.optimization_interval

    def optimize_symbol_parameters(self, symbol: str, df_15m: pd.DataFrame):
        if not self.groq_api_key or df_15m.empty:
            return

        state = self.symbol_states[symbol]
        latest = df_15m.iloc[-1]
        
        # ارسال داده‌های تأثیرگذار و کلیدی بازار به هوش مصنوعی برای تصمیم‌گیری دقیق‌تر
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

        prompt = f"""
You are an advanced quantitative trading AI. First, analyze the following key market data and indicators for asset {symbol}:
{json.dumps(market_metrics, indent=2)}

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
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2
        }

        try:
            response = requests.post(f"{self.groq_endpoint}v1/chat/completions", json=payload, headers=headers, timeout=25)
            if response.status_code == 200:
                res_data = response.json()
                content = res_data['choices'][0]['message']['content'].strip()
                
                if content.startswith("```"):
                    content = content.strip("`").replace("json\n", "").strip()

                raw_params = json.loads(content)
                state["params"] = self.validate_and_clamp_params(raw_params)
                state["last_optimized_time"] = datetime.now()
                logger.info(f"پارامترهای ضد ضرر و پویای {symbol} بر اساس داده‌های روز بروزرسانی شد.")
        except Exception as e:
            logger.error(f"خطا در بهینه‌سازی هوش مصنوعی برای {symbol}: {e}")

    def get_params(self, symbol: str) -> dict:
        return self.symbol_states[symbol]["params"]

# ==================== موتور سیگنال ضد ضرر ====================
class SignalEngine:
    def __init__(self, config: Config, ai_optimizer: AIParameterOptimizer, analysis: AnalysisLayer):
        self.config = config
        self.ai_optimizer = ai_optimizer
        self.analysis = analysis

    def get_rule_signal(self, symbol: str, df_15m: pd.DataFrame, trend_4h: str) -> Optional[str]:
        if df_15m.empty or len(df_15m) < 30:
            return None
            
        # بررسی بلک‌لیست (اگر ارز به دلیل ضرر قفل باشد، هیچ سیگنالی صادر نمی‌شود)
        if self.ai_optimizer.is_blacklisted(symbol):
            return None

        # بررسی رژیم بازار (جلوگیری از ترید در بازارهای رنج و پر از فیک)
        if not self.analysis.is_market_tradable(df_15m):
            return None
            
        latest = df_15m.iloc[-1]
        prev = df_15m.iloc[-2]
        p = self.ai_optimizer.get_params(symbol)

        if pd.isna(latest['rsi']) or pd.isna(latest['ema_fast']) or pd.isna(latest['atr']):
            return None

        if latest['atr'] < (latest['close'] * p["atr_min_filter"]):
            return None

        volume_confirmed = latest['volume'] > (latest['vol_sma'] * p["volume_mult"])
        if not volume_confirmed:
            return None

        if trend_4h in ["BULLISH", "NEUTRAL"]:
            ema_bull = latest['ema_fast'] > latest['ema_slow']
            rsi_buy = (latest['rsi'] > p["rsi_buy_min"] and prev['rsi'] <= p["rsi_buy_min"]) or (p["rsi_buy_max_range_start"] <= latest['rsi'] <= p["rsi_buy_max_range_end"] and latest['rsi'] > prev['rsi'])
            if ema_bull and rsi_buy:
                return "BUY"

        if trend_4h in ["BEARISH", "NEUTRAL"]:
            ema_bear = latest['ema_fast'] < latest['ema_slow']
            rsi_sell = (latest['rsi'] < p["rsi_sell_max"] and prev['rsi'] >= p["rsi_sell_max"]) or (p["rsi_sell_min_range_start"] <= latest['rsi'] <= p["rsi_sell_min_range_end"] and latest['rsi'] < prev['rsi'])
            if ema_bear and rsi_sell:
                return "SELL"

        return None

# ==================== ماژول معامله مجازی محافظت‌شده ====================
class PaperTrader:
    def __init__(self, config: Config, telegram_sender, ai_optimizer: AIParameterOptimizer):
        self.config = config
        self.telegram = telegram_sender
        self.ai_optimizer = ai_optimizer
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
                entry = trade['entry']
                symbol = trade['symbol']

                if side == "BUY":
                    if latest_low <= trade['sl']:
                        pnl = ((trade['sl'] - entry) / entry) * 100
                        self.ai_optimizer.register_loss(symbol)
                        self._send_close_report(trade, pnl)
                        del self.active_trades[trade_id]
                        continue
                    elif latest_high >= trade['tp1']:
                        pnl = ((trade['tp1'] - entry) / entry) * 100
                        self.ai_optimizer.register_win(symbol)
                        self._send_close_report(trade, pnl)
                        del self.active_trades[trade_id]
                        continue

                elif side == "SELL":
                    if latest_high >= trade['sl']:
                        pnl = ((entry - trade['sl']) / entry) * 100
                        self.ai_optimizer.register_loss(symbol)
                        self._send_close_report(trade, pnl)
                        del self.active_trades[trade_id]
                        continue
                    elif latest_low <= trade['tp1']:
                        pnl = ((entry - trade['tp1']) / entry) * 100
                        self.ai_optimizer.register_win(symbol)
                        self._send_close_report(trade, pnl)
                        del self.active_trades[trade_id]
                        continue

            except Exception as e:
                logger.error(f"خطا در بررسی معامله مجازی {trade_id}: {e}")

        self._save_trades()

    def _send_close_report(self, trade: Dict, pnl: float):
        emoji = "✅" if pnl > 0 else "❌"
        msg = f"""
{emoji} **گزارش معامله محافظت‌شده**

📌 **ارز:** {trade['symbol']} ({trade['side']})
📈 **سود/زیان:** {pnl:+.2f}%
"""
        self.telegram.send_personal_message(msg)

# ==================== ارسال تلگرام ====================
class TelegramSender:
    def __init__(self, config: Config, ai_optimizer: AIParameterOptimizer):
        self.config = config
        self.ai_optimizer = ai_optimizer
        self.base_url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){config.TELEGRAM_BOT_TOKEN}"

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

        p = self.ai_optimizer.get_params(symbol)

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
{emoji} **ANTI-LOSS ULTRA SIGNAL: {side} / {direction}**

📍 **Symbol:** {symbol}
⏱ **Timeframe:** {timeframe} (Trend 4H: {trend_4h})

💵 **Entry Price:** {price:,}

🎯 **Dynamic Targets:**
  1️⃣ TP1: {tp1:,}
  2️⃣ TP2: {tp2:,}
  3️⃣ TP3 (Max Yield): {tp3:,}

🛑 **Stop-Loss:** {stop_loss:,}
⚙️ **Trailing Stop Trigger:** Move SL to Entry at {trailing_step:,}

📊 **Metrics:** RSI: {latest['rsi']:.1f} | Market Guardrails Active
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
            logger.info(f"سیگنال ضد ضرر {side} برای {symbol} ارسال شد")
            
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
        self.signal_engine = SignalEngine(self.config, self.ai_optimizer, self.analysis)
        self.telegram = TelegramSender(self.config, self.ai_optimizer)
        self.paper_trader = PaperTrader(self.config, self.telegram, self.ai_optimizer)
        self.running = True
        self.last_signal_time: Dict[str, datetime] = {}

    def process_symbol(self, symbol: str):
        try:
            df_15m = self.data.fetch_ohlcv(symbol, timeframe=self.config.ENTRY_TIMEFRAME)
            df_15m = self.analysis.calculate_indicators(df_15m)
            if df_15m.empty:
                return

            if self.ai_optimizer.should_optimize(symbol):
                logger.info(f"بروزرسانی پارامترهای ضد ضرر هوش مصنوعی برای {symbol}...")
                self.ai_optimizer.optimize_symbol_parameters(symbol, df_15m)

            df_4h = self.data.fetch_ohlcv(symbol, timeframe=self.config.TREND_TIMEFRAME)
            df_4h = self.analysis.calculate_indicators(df_4h)
            trend_4h = self.analysis.get_major_trend(df_4h)

            rule_signal = self.signal_engine.get_rule_signal(symbol, df_15m, trend_4h)
            if not rule_signal:
                return

            now = datetime.now()
            p = self.ai_optimizer.get_params(symbol)
            cooldown = p.get("cooldown_minutes", 120)
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
        logger.info("----- شروع آنالیز ایمن و ضد ضرر بازار -----")
        for symbol in self.config.SYMBOLS:
            self.process_symbol(symbol)
            time.sleep(1.5)
            
        self.paper_trader.update_and_check_trades(self.data)

    def start(self):
        logger.info("بات ضد ضرر با قابلیت تنظیم اختصاصی ارزها فعال شد")
        start_message = "🛡 **نسخه جدید ضد ضرر و محافظت از سرمایه فعال شد.**\n\nربات اکنون داده‌های بازار را به هوش مصنوعی می‌دهد تا بر اساس شرایط روز، پارامترها را بهینه‌سازی کند."
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
