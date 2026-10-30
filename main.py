# ==============================================================================
# Ultra-Pro Hybrid Trading Bot Engine v3.0 (Enterprise Architecture)
# Powered by Price Action (BOS/CHoCH/FVG) + Multi-Timeframe Trend + Groq AI Guard
# ==============================================================================
import os
import time
import logging
import requests
import gc
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, date as date_cls
from typing import Dict, Optional, List, Tuple
import pandas as pd
import numpy as np
import ccxt
from dotenv import load_dotenv

load_dotenv()

# ==================== Health Check Web Server ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Trading Bot Engine is running at Peak Performance!")

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
        logger.error(f"Error starting health check server: {e}")

threading.Thread(target=start_health_check_server, daemon=True).start()

# ==================== Configuration ====================
class Config:
    EXCHANGE_ID = "binance"
    API_KEY = os.getenv("EXCHANGE_API_KEY", "")
    SECRET = os.getenv("EXCHANGE_SECRET", "")

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    PERSONAL_CHAT_ID = os.getenv("PERSONAL_CHAT_ID", "")

    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

    SYMBOLS = [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
        "XRP/USDT", "AVAX/USDT", "NEAR/USDT", "ADA/USDT",
        "DOGE/USDT", "LINK/USDT", "PAXG/USDT", "LTC/USDT"
    ]

    ENTRY_TIMEFRAME = "15m"
    CONFIRM_TIMEFRAME = "1h"
    TREND_TIMEFRAME = "4h"
    CHECK_INTERVAL = 300  # 5 minutes

    MIN_SIGNAL_SCORE = 7.0
    ATR_PERCENTILE_MAX = 96.0

    VIRTUAL_CAPITAL_USDT = float(os.getenv("VIRTUAL_CAPITAL_USDT", 10000))
    RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", 1.2))
    MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES", 4))

    DEFAULT_SPREAD_PCT_FALLBACK = 0.05
    EXCHANGE_TAKER_FEE_PCT = float(os.getenv("EXCHANGE_TAKER_FEE_PCT", 0.05))

    TRADE_MONITOR_INTERVAL_SECONDS = int(os.getenv("TRADE_MONITOR_INTERVAL_SECONDS", 20))

    # AI Quota & Budget Management
    GROQ_DAILY_TOKEN_CAP = int(os.getenv("GROQ_DAILY_TOKEN_CAP", 200000))
    GROQ_DAILY_REQUEST_CAP = int(os.getenv("GROQ_DAILY_REQUEST_CAP", 1000))

    def validate(self):
        required = {"TELEGRAM_BOT_TOKEN": self.TELEGRAM_BOT_TOKEN, "TELEGRAM_CHAT_ID": self.TELEGRAM_CHAT_ID}
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(f"Missing required env variables: {', '.join(missing)}")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler("trading_engine.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== Data Layer ====================
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

    BINANCE_PUBLIC_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]

    def _binance_get(self, path: str, params: dict):
        last_error = None
        for host in self.BINANCE_PUBLIC_HOSTS:
            try:
                resp = requests.get(f"{host}{path}", params=params, timeout=10)
                if resp.status_code == 200:
                    return resp.json()
                last_error = f"{host} -> HTTP {resp.status_code}"
            except Exception as e:
                last_error = f"{host} -> {e}"
        raise RuntimeError(last_error)

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        try:
            raw = self._binance_get("/api/v3/klines", {
                "symbol": symbol.replace("/", ""), "interval": timeframe, "limit": limit
            })
            ohlcv = [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in raw]
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception as e:
            logger.error(f"Error fetching OHLCV for {symbol} ({timeframe}): {e}")
            return pd.DataFrame()

# ==================== Advanced Technical & Smart Money Analysis Layer ====================
class PriceActionAnalysis:
    @staticmethod
    def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < 50:
            return df
        df = df.copy()

        # EMA Indicators
        df['ema_fast'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=50, adjust=False).mean()
        df['ema_trend'] = df['close'].ewm(span=200, adjust=False).mean()

        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / (loss + 1e-9)
        df['rsi'] = 100 - (100 / (1 + rs))

        # ATR
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close = (df['low'] - df['close'].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['true_range'] = tr
        df['atr'] = tr.rolling(window=14).mean()

        # Volume SMA
        df['vol_sma'] = df['volume'].rolling(window=20).mean()

        # Fair Value Gap (FVG) Detection
        df['fvg_bullish'] = (df['low'] > df['high'].shift(2)) & (df['close'].shift(1) > df['high'].shift(2))
        df['fvg_bearish'] = (df['high'] < df['low'].shift(2)) & (df['close'].shift(1) < df['low'].shift(2))

        # Structure Swings
        df['swing_high'] = df['high'].rolling(window=11, center=True).apply(lambda x: x[5] == max(x), raw=True)
        df['swing_low'] = df['low'].rolling(window=11, center=True).apply(lambda x: x[5] == min(x), raw=True)

        return df

    @staticmethod
    def detect_market_structure(df: pd.DataFrame) -> str:
        if df.empty or len(df) < 50:
            return "NEUTRAL"
        
        recent = df.tail(60)
        highs = recent[recent['swing_high'] == 1]['high'].values
        lows = recent[recent['swing_low'] == 1]['low'].values

        if len(highs) >= 2 and len(lows) >= 2:
            if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
                return "BULLISH_STRUCTURE"
            elif highs[-1] < highs[-2] and lows[-1] < lows[-2]:
                return "BEARISH_STRUCTURE"
        return "RANGE_STRUCTURE"

# ==================== AI Evaluation Engine (Groq Integration) ====================
class AIEvaluator:
    def __init__(self, config: Config):
        self.config = config
        self.api_key = config.GROQ_API_KEY
        self.url = "https://api.groq.com/openai/v1/chat/completions"

    def evaluate_signal(self, symbol: str, side: str, metrics: dict) -> Tuple[bool, int, str]:
        if not self.api_key:
            return True, 75, "AI verification skipped (No API Key provided)"

        prompt = f"""
You are an expert Wall Street algorithmic risk manager. Evaluate this crypto setup:
Asset: {symbol} | Direction: {side}
Technical Metrics: {json.dumps(metrics)}

Determine if this trade offers a high-probability R:R setup. Avoid trades at extreme resistance/support.
Return ONLY valid JSON in this structure:
{{"approve": true, "confidence": 85, "reason": "Clear breakout above 4H EMA with bullish volume confirmation."}}
"""
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": "openai/gpt-oss-120b",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 300
        }

        try:
            resp = requests.post(self.url, json=payload, headers=headers, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                content = data['choices'][0]['message']['content'].strip()
                if content.startswith("```"):
                    content = content.strip("`").replace("json\n", "").strip()
                res = json.loads(content)
                return bool(res.get("approve", True)), int(res.get("confidence", 70)), str(res.get("reason", ""))
        except Exception as e:
            logger.warning(f"AI evaluation fallback for {symbol}: {e}")

        return True, 70, "AI Fallback: Executing on Technical Setup"

# ==================== Journal & Trade Management ====================
class PaperTrader:
    def __init__(self, config: Config):
        self.config = config
        self.file_path = "active_trades.json"
        self.active_trades: Dict = self._load()
        self.lock = threading.Lock()

    def _load(self) -> Dict:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save(self):
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.active_trades, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to save active trades: {e}")

    def execute_trade(self, symbol: str, side: str, entry: float, sl: float, tp1: float, tp2: float, qty: float):
        trade_id = f"{symbol}_{int(time.time())}"
        with self.lock:
            self.active_trades[trade_id] = {
                "symbol": symbol, "side": side, "entry": entry, "sl": sl, "original_sl": sl,
                "tp1": tp1, "tp2": tp2, "qty": qty, "remaining_pct": 100, "tp1_hit": False,
                "created_at": datetime.now().isoformat()
            }
            self._save()

    def update_positions(self, data_layer: DataLayer):
        with self.lock:
            if not self.active_trades:
                return

            for tid, trade in list(self.active_trades.items()):
                df = data_layer.fetch_ohlcv(trade['symbol'], timeframe="1m", limit=5)
                if df.empty:
                    continue
                
                curr_high = float(df['high'].max())
                curr_low = float(df['low'].min())
                side = trade['side']

                # Stop Loss Check
                if (side == "BUY" and curr_low <= trade['sl']) or (side == "SELL" and curr_high >= trade['sl']):
                    logger.info(f"Stop-Loss triggered for {trade['symbol']}")
                    del self.active_trades[tid]
                    self._save()
                    continue

                # TP1 Check (Partial Take Profit at 1.5R + Move SL to Breakeven ONLY after 1.2R expansion)
                if not trade['tp1_hit']:
                    if (side == "BUY" and curr_high >= trade['tp1']) or (side == "SELL" and curr_low <= trade['tp1']):
                        trade['tp1_hit'] = True
                        trade['sl'] = trade['entry']  # Move SL to Breakeven
                        trade['remaining_pct'] = 40  # Keep 40% for TP2
                        logger.info(f"TP1 hit for {trade['symbol']}. Lock-in profits and SL moved to Breakeven.")
                        self._save()
                        continue

                # TP2 Check (Full Exit)
                if trade['tp1_hit']:
                    if (side == "BUY" and curr_high >= trade['tp2']) or (side == "SELL" and curr_low <= trade['tp2']):
                        logger.info(f"TP2 target achieved for {trade['symbol']}. Closing position.")
                        del self.active_trades[tid]
                        self._save()

# ==================== Core Trading Bot Engine ====================
class HybridTradingBot:
    def __init__(self):
        self.config = Config()
        self.config.validate()
        self.data = DataLayer(self.config)
        self.ai = AIEvaluator(self.config)
        self.trader = PaperTrader(self.config)
        self.running = True

    def run_strategy(self, symbol: str):
        df_15m = self.data.fetch_ohlcv(symbol, timeframe=self.config.ENTRY_TIMEFRAME)
        df_4h = self.data.fetch_ohlcv(symbol, timeframe=self.config.TREND_TIMEFRAME)

        if df_15m.empty or df_4h.empty:
            return

        df_15m = PriceActionAnalysis.add_indicators(df_15m)
        df_4h = PriceActionAnalysis.add_indicators(df_4h)

        latest_15m = df_15m.iloc[-1]
        structure_15m = PriceActionAnalysis.detect_market_structure(df_15m)
        trend_4h_bullish = df_4h.iloc[-1]['close'] > df_4h.iloc[-1]['ema_trend']

        # Core Trading Logic Rules
        buy_conditions = [
            trend_4h_bullish,
            structure_15m == "BULLISH_STRUCTURE",
            latest_15m['close'] > latest_15m['ema_fast'],
            latest_15m['rsi'] > 48 and latest_15m['rsi'] < 68,
            latest_15m['fvg_bullish'] or latest_15m['volume'] > latest_15m['vol_sma'] * 1.2
        ]

        if all(buy_conditions):
            entry = float(latest_15m['close'])
            atr = float(latest_15m['atr'])
            sl = entry - (atr * 1.5)
            risk = entry - sl
            tp1 = entry + (risk * 1.5)
            tp2 = entry + (risk * 3.0)

            metrics = {
                "rsi": float(latest_15m['rsi']),
                "atr": atr,
                "structure": structure_15m,
                "volume_ratio": float(latest_15m['volume'] / latest_15m['vol_sma'])
            }

            approved, confidence, reason = self.ai.evaluate_signal(symbol, "BUY", metrics)
            if approved and confidence >= 65:
                position_size = (self.config.VIRTUAL_CAPITAL_USDT * (self.config.RISK_PER_TRADE_PCT / 100)) / risk
                self.trader.execute_trade(symbol, "BUY", entry, sl, tp1, tp2, position_size)
                logger.info(f"🚀 BUY Signal Executed for {symbol} at {entry} | Reason: {reason}")

    def start(self):
        logger.info("⚡ Enterprise Hybrid Trading Bot Started.")
        
        # Start Monitor Thread for Active Positions
        def monitor_loop():
            while self.running:
                try:
                    self.trader.update_positions(self.data)
                except Exception as e:
                    logger.error(f"Error in position monitor thread: {e}")
                time.sleep(self.config.TRADE_MONITOR_INTERVAL_SECONDS)

        threading.Thread(target=monitor_loop, daemon=True).start()

        # Main Loop
        while self.running:
            for symbol in self.config.SYMBOLS:
                try:
                    self.run_strategy(symbol)
                except Exception as e:
                    logger.error(f"Error executing strategy on {symbol}: {e}")
                time.sleep(2)
            time.sleep(self.config.CHECK_INTERVAL)

if __name__ == "__main__":
    bot = HybridTradingBot()
    bot.start()