کد کامل و اصلاح‌شدهٔ سیستم به‌صورت زیر است. در این نسخه، مشکل قطع شدن پاسخ هوش مصنوعی (max_tokens)، مکانیزم جایگزین در صورت خطای API، و محاسبهٔ حجم معاملات صرافی CoinEx برطرف شده است.
# ==============================================
# Hybrid Signal Bot - نسخه کامل و اصلاح‌شده (V5 Ultimate Pro)
# ==============================================
import os
import re
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
from openai import OpenAI
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

    AI_API_KEY = os.getenv("AI_API_KEY")
    AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1")
    AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-oss-120b")

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    PERSONAL_CHAT_ID = os.getenv("PERSONAL_CHAT_ID")

    FALLBACK_SYMBOLS = [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
        "AVAX/USDT", "NEAR/USDT", "ADA/USDT", "DOGE/USDT", "LINK/USDT",
    ]
    SYMBOLS = list(FALLBACK_SYMBOLS)

    MAX_SYMBOLS_TO_SCAN = 80          
    MIN_QUOTE_VOLUME_USDT = 150_000   
    SYMBOL_REFRESH_HOURS = 12         

    ENTRY_TIMEFRAME = "15m"
    TREND_TIMEFRAME = "4h"
    CHECK_INTERVAL = 300
    MIN_CONFIDENCE_AI = 0.58  

    MIN_ADX_STRENGTH = 10          
    MAX_CONCURRENT_TRADES = 6      
    MAX_TRADES_PER_SYMBOL = 1      
    SIGNAL_COOLDOWN_MINUTES = 45   

    ACCOUNT_BALANCE_USDT = float(os.getenv("ACCOUNT_BALANCE_USDT", "1000"))
    RISK_PER_TRADE_PCT = 1.5   
    MAX_RISK_PER_TRADE_PCT = 2.5  

    RISK_CONFIG_FILE = "risk_config.json"

    def load_dynamic_risk_config(self):
        if not os.path.exists(self.RISK_CONFIG_FILE):
            return
        try:
            with open(self.RISK_CONFIG_FILE, "r") as f:
                overrides = json.load(f)

            if "risk_per_trade_pct" in overrides:
                self.RISK_PER_TRADE_PCT = min(float(overrides["risk_per_trade_pct"]), self.MAX_RISK_PER_TRADE_PCT)
            if "min_adx_strength" in overrides:
                self.MIN_ADX_STRENGTH = max(5, float(overrides["min_adx_strength"]))
            if "min_confidence_ai" in overrides:
                self.MIN_CONFIDENCE_AI = min(max(float(overrides["min_confidence_ai"]), 0.50), 0.98)
            if "max_concurrent_trades" in overrides:
                self.MAX_CONCURRENT_TRADES = max(1, int(overrides["max_concurrent_trades"]))
            if "account_balance_usdt" in overrides:
                self.ACCOUNT_BALANCE_USDT = max(0.0, float(overrides["account_balance_usdt"]))
        except Exception as e:
            logger.error(f"خطا در خواندن risk_config.json: {e}")

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

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
        ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df

    def get_liquid_symbols(self, quote: str = "USDT", limit: int = 80, min_quote_volume: float = 150_000) -> List[str]:
        try:
            markets = self.exchange.load_markets()
            tickers = self.exchange.fetch_tickers()
            candidates = []
            for symbol, market in markets.items():
                if not market.get('active', True):
                    continue
                if market.get('quote') != quote or market.get('type') != 'spot':
                    continue
                ticker = tickers.get(symbol) or {}
                
                quote_volume = ticker.get('quoteVolume')
                if not quote_volume or quote_volume == 0:
                    base_vol = ticker.get('baseVolume') or 0
                    last_price = ticker.get('last') or 0
                    quote_volume = base_vol * last_price

                if quote_volume >= min_quote_volume:
                    candidates.append((symbol, quote_volume))

            candidates.sort(key=lambda x: x[1], reverse=True)
            symbols = [s for s, _ in candidates[:limit]]
            if symbols:
                logger.info(f"{len(symbols)} نماد نقدشونده پیدا شد")
                return symbols
        except Exception as e:
            logger.error(f"خطا در دریافت لیست نمادها: {e}")

        return list(self.config.FALLBACK_SYMBOLS)

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
        
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
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

        up_move = df['high'].diff()
        down_move = -df['low'].diff()
        plus_dm = pd.Series(0.0, index=df.index)
        minus_dm = pd.Series(0.0, index=df.index)
        plus_dm[(up_move > down_move) & (up_move > 0)] = up_move[(up_move > down_move) & (up_move > 0)]
        minus_dm[(down_move > up_move) & (down_move > 0)] = down_move[(down_move > up_move) & (down_move > 0)]
        atr_smooth = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        plus_di = 100 * (plus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr_smooth)
        minus_di = 100 * (minus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr_smooth)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
        df['adx'] = dx.ewm(alpha=1/14, adjust=False, min_periods=14).mean()

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

    def get_ai_confirmation(self, symbol: str, side: str, df: pd.DataFrame, trend: str) -> Dict:
        latest = df.iloc[-1]
        adx_value = float(latest['adx']) if not pd.isna(latest['adx']) else 0.0

        prompt = f"""
Analyze this crypto trading setup and output ONLY a single integer score from 0 to 100 representing signal strength.

Setup:
- Symbol: {symbol}
- Side: {side}
- 4H Trend: {trend}
- 15m Close: {latest['close']}
- RSI: {latest['rsi']:.1f}
- ADX: {adx_value:.1f}
- EMA20/50: {latest['ema_fast']:.2f} / {latest['ema_slow']:.2f}

Output format: Just the number (e.g. 75).
"""
        try:
            response = self.client.chat.completions.create(
                model=self.config.AI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=20
            )
            answer = response.choices[0].message.content.strip()
            match = re.search(r'\d{1,3}', answer)
            if match:
                raw_score = int(match.group())
                score = max(0, min(100, raw_score)) / 100.0
                return {"confidence": score, "error": False}
            raise ValueError(f"No digit found in answer: '{answer}'")
        except Exception as e:
            logger.warning(f"خطای AI برای {symbol}: {e} - استفاده از سیستم جایگزین تکنیکال")
            fallback_score = 0.65 if adx_value >= 18 else 0.50
            return {"confidence": fallback_score, "error": True}

# ==================== موتور سیگنال ====================
class SignalEngine:
    def __init__(self, config: Config):
        self.config = config

    def get_rule_signal(self, df_15m: pd.DataFrame, trend_4h: str, trend_1h: str = "NEUTRAL") -> Optional[str]:
        latest = df_15m.iloc[-1]
        prev = df_15m.iloc[-2]

        if pd.isna(latest['rsi']) or pd.isna(latest['ema_fast']) or pd.isna(latest['atr']) or pd.isna(latest['adx']):
            return None

        if latest['atr'] < (latest['close'] * 0.0012):
            return None

        volume_confirmed = latest['volume'] > (latest['vol_sma'] * 0.50)
        strong_volume = latest['volume'] > (latest['vol_sma'] * 1.0)
        strong_adx = latest['adx'] >= self.config.MIN_ADX_STRENGTH

        ema_bull = latest['ema_fast'] > latest['ema_slow']
        ema_bear = latest['ema_fast'] < latest['ema_slow']

        rsi_buy_zone = (35 <= latest['rsi'] <= 72) and (latest['rsi'] > prev['rsi'])
        rsi_sell_zone = (28 <= latest['rsi'] <= 65) and (latest['rsi'] < prev['rsi'])

        buy_score = 0
        if ema_bull: buy_score += 2
        if rsi_buy_zone: buy_score += 2
        if volume_confirmed: buy_score += 1
        if strong_volume: buy_score += 1
        if strong_adx: buy_score += 1
        if trend_4h == "BULLISH": buy_score += 1
        elif trend_4h == "BEARISH": buy_score -= 2
        if trend_1h == "BULLISH": buy_score += 1
        elif trend_1h == "BEARISH": buy_score -= 1

        sell_score = 0
        if ema_bear: sell_score += 2
        if rsi_sell_zone: sell_score += 2
        if volume_confirmed: sell_score += 1
        if strong_volume: sell_score += 1
        if strong_adx: sell_score += 1
        if trend_4h == "BEARISH": sell_score += 1
        elif trend_4h == "BULLISH": sell_score -= 2
        if trend_1h == "BEARISH": sell_score += 1
        elif trend_1h == "BULLISH": sell_score -= 1

        MIN_SCORE = 4

        logger.info(f"[SCORE] buy={buy_score} sell={sell_score} (آستانه={MIN_SCORE}) rsi={latest['rsi']:.1f} adx={latest['adx']:.1f}")

        if buy_score >= MIN_SCORE and buy_score > sell_score:
            return "BUY"
        if sell_score >= MIN_SCORE and sell_score > buy_score:
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

    TP1_PORTION = 0.50
    TP2_PORTION = 0.30
    TP3_PORTION = 0.20

    def open_virtual_trade(self, symbol: str, side: str, entry_price: float, tp1: float, tp2: float, tp3: float, sl: float,
                            position_size_units: float = 0.0, position_value_usdt: float = 0.0):
        trade_id = f"{symbol}_{int(time.time())}"
        self.active_trades[trade_id] = {
            "symbol": symbol,
            "side": side,
            "entry": entry_price,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "sl": sl,
            "original_sl": sl,
            "remaining_pct": 1.0,
            "tp1_hit": False,
            "tp2_hit": False,
            "realized_pnl_contribution": 0.0,
            "position_size_units": position_size_units,
            "position_value_usdt": position_value_usdt,
            "open_time": datetime.now().strftime('%Y-%m-%d %H:%M')
        }
        self._save_trades()

    @staticmethod
    def _pnl_pct(side: str, entry: float, exit_price: float) -> float:
        if side == "BUY":
            return ((exit_price - entry) / entry) * 100
        return ((entry - exit_price) / entry) * 100

    def update_and_check_trades(self, data_layer: DataLayer):
        for trade_id, trade in list(self.active_trades.items()):
            try:
                df = data_layer.fetch_ohlcv(trade['symbol'], timeframe="1m", limit=5)
                latest_high = float(df['high'].max())
                latest_low = float(df['low'].min())

                side = trade['side']
                entry = trade['entry']

                sl_triggered = (latest_low <= trade['sl']) if side == "BUY" else (latest_high >= trade['sl'])
                if sl_triggered:
                    pnl_leg = self._pnl_pct(side, entry, trade['sl'])
                    total_pnl = trade['realized_pnl_contribution'] + (pnl_leg * trade['remaining_pct'])
                    label = "SL/Breakeven" if trade['tp1_hit'] else "Stop-Loss"
                    self._send_close_report(trade, total_pnl, label)
                    del self.active_trades[trade_id]
                    continue

                tp1_hit_now = (latest_high >= trade['tp1']) if side == "BUY" else (latest_low <= trade['tp1'])
                if not trade['tp1_hit'] and tp1_hit_now:
                    pnl_leg = self._pnl_pct(side, entry, trade['tp1'])
                    trade['realized_pnl_contribution'] += pnl_leg * self.TP1_PORTION
                    trade['remaining_pct'] -= self.TP1_PORTION
                    trade['tp1_hit'] = True
                    trade['sl'] = entry
                    self.telegram.send_personal_message(
                        f"🟡 *TP1 زده شد - {trade['symbol']} ({side})*\n"
                        f"۵۰٪ پوزیشن با {pnl_leg:+.2f}% بسته شد. SL به نقطه ورود (Breakeven) منتقل شد."
                    )

                tp2_hit_now = (latest_high >= trade['tp2']) if side == "BUY" else (latest_low <= trade['tp2'])
                if trade['tp1_hit'] and not trade['tp2_hit'] and tp2_hit_now:
                    pnl_leg = self._pnl_pct(side, entry, trade['tp2'])
                    trade['realized_pnl_contribution'] += pnl_leg * self.TP2_PORTION
                    trade['remaining_pct'] -= self.TP2_PORTION
                    trade['tp2_hit'] = True
                    trade['sl'] = trade['tp1']
                    self.telegram.send_personal_message(
                        f"🟡 *TP2 زده شد - {trade['symbol']} ({side})*\n"
                        f"۳۰٪ دیگر با {pnl_leg:+.2f}% بسته شد. SL به سطح TP1 منتقل شد."
                    )

                tp3_hit_now = (latest_high >= trade['tp3']) if side == "BUY" else (latest_low <= trade['tp3'])
                if trade['tp1_hit'] and trade['tp2_hit'] and tp3_hit_now:
                    pnl_leg = self._pnl_pct(side, entry, trade['tp3'])
                    total_pnl = trade['realized_pnl_contribution'] + (pnl_leg * trade['remaining_pct'])
                    self._send_close_report(trade, total_pnl, "TP3 - Full Target")
                    del self.active_trades[trade_id]
                    continue

            except Exception as e:
                logger.error(f"خطا در بروزرسانی معامله مجازی {trade_id}: {e}")

        self._save_trades()

    def _send_close_report(self, trade: Dict, pnl: float, reason: str = ""):
        emoji = "✅" if pnl > 0 else "❌"
        reason_line = f"🔎 *دلیل:* {reason}\n" if reason else ""
        usdt_pnl = round(trade.get('position_value_usdt', 0.0) * (pnl / 100.0), 2)
        msg = f"""
{emoji} *معامله بسته شد (نتیجه نهایی)*

📌 *ارز:* {trade['symbol']} ({trade['side']})
{reason_line}📈 *سود/زیان کل (وزن‌دار):* {pnl:+.2f}% (~{usdt_pnl:+,.2f} USDT)
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

    def send_signal(self, symbol: str, side: str, latest: pd.Series, confidence: float, trend_4h: str) -> Dict:
        emoji = "🟢" if side == "BUY" else "🔴"
        direction = "LONG" if side == "BUY" else "SHORT"
        price = float(latest['close'])
        atr = float(latest['atr']) if not pd.isna(latest['atr']) else price * 0.01

        if side == "BUY":
            raw_sl = min(float(latest['support']), price - (1.3 * atr))
            min_dist = 0.8 * atr
            max_dist = 2.5 * atr
            dist = min(max(price - raw_sl, min_dist), max_dist)
            stop_loss = round(price - dist, 4)
            risk = price - stop_loss
            tp1 = round(price + (1.5 * risk), 4)
            tp2 = round(price + (2.5 * risk), 4)
            tp3 = round(price + (4.2 * risk), 4)
        else:
            raw_sl = max(float(latest['resistance']), price + (1.3 * atr))
            min_dist = 0.8 * atr
            max_dist = 2.5 * atr
            dist = min(max(raw_sl - price, min_dist), max_dist)
            stop_loss = round(price + dist, 4)
            risk = stop_loss - price
            tp1 = round(price - (1.5 * risk), 4)
            tp2 = round(price - (2.5 * risk), 4)
            tp3 = round(price - (4.2 * risk), 4)

        confidence_multiplier = 0.8 + (0.4 * min(max(confidence, 0.0), 1.0))
        effective_risk_pct = min(self.config.RISK_PER_TRADE_PCT * confidence_multiplier, self.config.MAX_RISK_PER_TRADE_PCT)
        risk_amount_usdt = self.config.ACCOUNT_BALANCE_USDT * (effective_risk_pct / 100.0)
        position_size_units = round(risk_amount_usdt / risk, 6) if risk > 0 else 0
        position_value_usdt = round(position_size_units * price, 2)

        message = f"""
{emoji} *ULTRA SIGNAL: {side} / {direction}*

📍 *Symbol:* {symbol}
⏱ *Timeframe:* {self.config.ENTRY_TIMEFRAME} (Trend 4H: {trend_4h})

💵 *Entry Price:* {price:,}

🎯 *Dynamic Targets:*
  1️⃣ TP1 (50%): {tp1:,}
  2️⃣ TP2 (30%): {tp2:,}
  3️⃣ TP3 (20%): {tp3:,}

🛑 *Stop-Loss:* {stop_loss:,}

💰 *Position Size:* {position_size_units} (~{position_value_usdt:,} USDT)
📊 *Metrics:* RSI: {latest['rsi']:.1f} | ADX: {latest['adx']:.1f} | AI Score: {confidence:.0%}
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
            logger.info(f"سیگنال {side} برای {symbol} ارسال شد")
            
            return {
                "price": price,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
                "sl": stop_loss,
                "position_size_units": position_size_units,
                "position_value_usdt": position_value_usdt
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
        self.paper_trader = PaperTrader(self.config, self.telegram)
        self.running = True
        self.last_signal_time: Dict[str, datetime] = {}
        self.last_symbol_refresh: Optional[datetime] = None
        self.cycle_stats = {"scanned": 0, "rule_passed": 0, "ai_passed": 0, "ai_errors": 0}
        self._refresh_symbol_list()

    def _refresh_symbol_list(self):
        symbols = self.data.get_liquid_symbols(
            limit=self.config.MAX_SYMBOLS_TO_SCAN,
            min_quote_volume=self.config.MIN_QUOTE_VOLUME_USDT
        )
        if symbols:
            self.config.SYMBOLS = symbols
        else:
            self.config.SYMBOLS = list(self.config.FALLBACK_SYMBOLS)
        self.last_symbol_refresh = datetime.now()
        logger.info(f"در حال اسکن {len(self.config.SYMBOLS)} نماد")

    def process_symbol(self, symbol: str):
        try:
            self.cycle_stats["scanned"] += 1

            df_15m = self.data.fetch_ohlcv(symbol, timeframe=self.config.ENTRY_TIMEFRAME)
            df_15m = self.analysis.calculate_indicators(df_15m)

            df_1h = self.data.fetch_ohlcv(symbol, timeframe="1h")
            df_1h = self.analysis.calculate_indicators(df_1h)
            trend_1h = self.analysis.get_major_trend(df_1h)

            df_4h = self.data.fetch_ohlcv(symbol, timeframe=self.config.TREND_TIMEFRAME)
            df_4h = self.analysis.calculate_indicators(df_4h)
            trend_4h = self.analysis.get_major_trend(df_4h)

            rule_signal = self.signal_engine.get_rule_signal(df_15m, trend_4h, trend_1h)

            latest_debug = df_15m.iloc[-1]
            logger.info(
                f"[DEBUG] {symbol} | trend4h={trend_4h} trend1h={trend_1h} | "
                f"RSI={latest_debug['rsi']:.1f} ADX={latest_debug['adx']:.1f} "
                f"signal={rule_signal}"
            )

            if not rule_signal:
                return

            self.cycle_stats["rule_passed"] += 1

            if len(self.paper_trader.active_trades) >= self.config.MAX_CONCURRENT_TRADES:
                return

            open_for_symbol = sum(1 for t in self.paper_trader.active_trades.values() if t['symbol'] == symbol)
            if open_for_symbol >= self.config.MAX_TRADES_PER_SYMBOL:
                return

            now = datetime.now()
            if symbol in self.last_signal_time:
                if now - self.last_signal_time[symbol] < timedelta(minutes=self.config.SIGNAL_COOLDOWN_MINUTES):
                    return

            latest = df_15m.iloc[-1]
            ai_result = self.analysis.get_ai_confirmation(symbol, rule_signal, df_15m, trend_4h)
            if ai_result.get("error"):
                self.cycle_stats["ai_errors"] += 1

            if ai_result["confidence"] >= self.config.MIN_CONFIDENCE_AI:
                self.cycle_stats["ai_passed"] += 1
                trade_data = self.telegram.send_signal(symbol, rule_signal, latest, ai_result["confidence"], trend_4h)
                
                if trade_data:
                    self.paper_trader.open_virtual_trade(
                        symbol=symbol,
                        side=rule_signal,
                        entry_price=trade_data["price"],
                        tp1=trade_data["tp1"],
                        tp2=trade_data["tp2"],
                        tp3=trade_data["tp3"],
                        sl=trade_data["sl"],
                        position_size_units=trade_data.get("position_size_units", 0.0),
                        position_value_usdt=trade_data.get("position_value_usdt", 0.0)
                    )

                self.last_signal_time[symbol] = now

        except Exception as e:
            logger.error(f"خطا در پردازش {symbol}: {e}")

    def run_once(self):
        self.config.load_dynamic_risk_config()

        if datetime.now() - self.last_symbol_refresh >= timedelta(hours=self.config.SYMBOL_REFRESH_HOURS):
            self._refresh_symbol_list()

        self.cycle_stats = {"scanned": 0, "rule_passed": 0, "ai_passed": 0, "ai_errors": 0}
        logger.info(f"----- شروع آنالیز پیشرفته بازار ({len(self.config.SYMBOLS)} نماد) -----")
        for symbol in self.config.SYMBOLS:
            self.process_symbol(symbol)
            time.sleep(0.4)

        logger.info(
            f"----- پایان چرخه | اسکن‌شده: {self.cycle_stats['scanned']} | "
            f"رد فیلتر تکنیکال: {self.cycle_stats['rule_passed']} | "
            f"تایید AI: {self.cycle_stats['ai_passed']} | خطای AI: {self.cycle_stats['ai_errors']} -----"
        )

        self.paper_trader.update_and_check_trades(self.data)

    def start(self):
        logger.info("بات V5 Ultimate Pro فعال شد")
        start_message = "⚡️ *نسخه کامل V5 Ultimate Pro فعال شد.*\n\nسیستم اصلاح‌شده آماده پردازش است."
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

