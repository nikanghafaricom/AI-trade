# ==============================================
# Hybrid Signal Bot - نسخه جامع (V5 Ultimate Pro)
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
    AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.x.ai/v1")
    AI_MODEL = os.getenv("AI_MODEL", "grok-3")

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
        "TON/USDT",
        "SUI/USDT",
        "APT/USDT",
        "ARB/USDT",
        "OP/USDT",
        "LTC/USDT",
        "DOT/USDT",
        "TRX/USDT",
        "INJ/USDT",
        "FIL/USDT",
    ]

    ENTRY_TIMEFRAME = "15m"
    TREND_TIMEFRAME = "4h"
    CHECK_INTERVAL = 300
    MIN_CONFIDENCE_AI = 0.68  # قبلاً 0.72؛ برای افزایش بیشتر فراوانی سیگنال شل‌تر شد

    # --- مدیریت ریسک اضافه‌شده ---
    MIN_ADX_STRENGTH = 12          # قبلاً 16؛ برای افزایش بیشتر فراوانی سیگنال شل‌تر شد
    MAX_CONCURRENT_TRADES = 6      # سقف تعداد معاملات همزمان برای کنترل ریسک کلی پرتفوی
    MAX_TRADES_PER_SYMBOL = 1      # هر نماد فقط یک معامله باز همزمان
    SIGNAL_COOLDOWN_MINUTES = 45   # قبلاً 90؛ فرصت سیگنال بیشتر روی هر نماد

    # --- Position Sizing واقعی بر اساس درصد ریسک ثابت ---
    ACCOUNT_BALANCE_USDT = float(os.getenv("ACCOUNT_BALANCE_USDT", "1000"))  # سرمایه‌ی فرضی/واقعی حساب
    RISK_PER_TRADE_PCT = 1.5   # درصدی از کل سرمایه که در هر معامله در معرض خطره (استاندارد صنعت: 0.5 تا 2 درصد)
    MAX_RISK_PER_TRADE_PCT = 2.5  # سقف مطلق ریسک هر معامله؛ حتی با تنظیم داشبورد در آینده از این بیشتر نمی‌شه

    RISK_CONFIG_FILE = "risk_config.json"

    def load_dynamic_risk_config(self):
        """
        این فایل جدا از کد اصلیه تا بعداً بشه از یه داشبورد بیرونی (یا حتی دستی) این
        پارامترها رو بدون نیاز به ری‌دیپلوی کد تغییر داد. هر پارامتر یه سقف/کف منطقی
        داره تا داشبورد نتونه به‌طور تصادفی ریسک رو به مقدار خطرناک ببره.
        """
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
            logger.error(f"خطا در خواندن risk_config.json: {e} - از مقادیر پیش‌فرض استفاده می‌شه")

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
        
        # RSI با میانگین‌گیری Wilder (استاندارد واقعی RSI، دقیق‌تر از rolling mean ساده)
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

        # ADX: قدرت روند. اضافه شده تا سیگنال‌های صادرشده در بازار بی‌روند/رنج فیلتر بشن
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
        prev = df.iloc[-2]

        adx_value = latest['adx'] if not pd.isna(latest['adx']) else 0.0

        prompt = f"""
You are a highly risk-averse, skeptical quantitative crypto trader whose job is to REJECT
mediocre setups, not to find reasons to approve them. Assume most signals are false positives
unless the confluence of evidence is strong.

Context:
- Symbol: {symbol}
- Trade Side: {side}
- Higher Timeframe (4H) Trend: {trend}
- 15m Close: {latest['close']}
- RSI: {latest['rsi']:.1f}
- ADX (trend strength): {adx_value:.1f}
- EMA20/50: {latest['ema_fast']:.2f} / {latest['ema_slow']:.2f}
- ATR Volatility: {latest['atr']:.4f}
- Volume ratio: {latest['volume']/latest['vol_sma']:.2f}x

Score this setup from 0 to 100 on probability of hitting TP before SL.
Penalize heavily for: counter-trend entries, weak ADX (<20), weak/average volume, RSI near
overbought/oversold extremes without confirmation.
Reply with ONLY the integer score and nothing else.
"""
        try:
            response = self.client.chat.completions.create(
                model=self.config.AI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=6
            )
            answer = response.choices[0].message.content.strip()
            match = re.search(r'\d{1,3}', answer)
            if not match:
                raise ValueError(f"پاسخ قابل پارس نبود: '{answer}'")
            raw_score = int(match.group())
            raw_score = max(0, min(100, raw_score))
            score = raw_score / 100.0
            return {"confidence": score}
        except Exception as e:
            # نکته مهم: قبلاً اینجا یه مقدار پیش‌فرض بالای آستانه برمی‌گشت که یعنی
            # وقتی AI خراب می‌شد سیگنال بدون تایید واقعی رد می‌شد به تلگرام!
            # الان به‌جای فال‌بک خوش‌بینانه، سیگنال رد می‌شه تا فیلتر AI معنی واقعی داشته باشه.
            logger.error(f"خطای AI برای {symbol}: {e} - سیگنال به‌صورت ایمن رد شد")
            return {"confidence": 0.0}

# ==================== موتور سیگنال ====================
class SignalEngine:
    def __init__(self, config: Config):
        self.config = config

    def get_rule_signal(self, df_15m: pd.DataFrame, trend_4h: str, trend_1h: str = "NEUTRAL") -> Optional[str]:
        latest = df_15m.iloc[-1]
        prev = df_15m.iloc[-2]
        
        if pd.isna(latest['rsi']) or pd.isna(latest['ema_fast']) or pd.isna(latest['atr']):
            return None

        if latest['atr'] < (latest['close'] * 0.0015):
            return None

        # فیلتر جدید: بازار بدون روند مشخص (ADX پایین) منبع اصلی سیگنال‌های کاذب هست
        if pd.isna(latest['adx']) or latest['adx'] < self.config.MIN_ADX_STRENGTH:
            return None

        volume_confirmed = latest['volume'] > (latest['vol_sma'] * 0.50)

        # تایید سه‌تایم‌فریمی: تایم‌فریم ۱ساعته نباید مخالف جهت معامله باشه
        # (فقط رد میشه اگه صراحتاً در جهت مخالف باشه، نه اینکه NEUTRAL باشه)
        if trend_4h in ["BULLISH", "NEUTRAL"] and trend_1h != "BEARISH":
            ema_bull = latest['ema_fast'] > latest['ema_slow']
            rsi_buy = (latest['rsi'] > 42 and prev['rsi'] <= 42) or (48 <= latest['rsi'] <= 65 and latest['rsi'] > prev['rsi'])
            if ema_bull and rsi_buy and volume_confirmed:
                return "BUY"

        if trend_4h in ["BEARISH", "NEUTRAL"] and trend_1h != "BULLISH":
            ema_bear = latest['ema_fast'] < latest['ema_slow']
            rsi_sell = (latest['rsi'] < 58 and prev['rsi'] >= 58) or (35 <= latest['rsi'] <= 52 and latest['rsi'] < prev['rsi'])
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

    # درصد پوزیشنی که در هر تارگت بسته می‌شه (جمعاً ۱۰۰٪)
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
            "realized_pnl_contribution": 0.0,  # سهم وزن‌دار PnL بسته‌شده تا الان
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
                hit_high = latest_high if side == "BUY" else latest_low
                hit_low = latest_low if side == "BUY" else latest_high
                # hit_high/hit_low نرمال‌شده نسبت به جهت معامله: hit_high یعنی سمتی که به سود نزدیک‌تره

                # ۱. حد ضرر (شامل حالتی که SL بعد از TP1/TP2 به breakeven/سود منتقل شده)
                sl_triggered = (latest_low <= trade['sl']) if side == "BUY" else (latest_high >= trade['sl'])
                if sl_triggered:
                    pnl_leg = self._pnl_pct(side, entry, trade['sl'])
                    total_pnl = trade['realized_pnl_contribution'] + (pnl_leg * trade['remaining_pct'])
                    label = "SL/Breakeven" if trade['tp1_hit'] else "Stop-Loss"
                    self._send_close_report(trade, total_pnl, label)
                    del self.active_trades[trade_id]
                    continue

                # ۲. TP1 - بستن ۵۰٪ پوزیشن و انتقال SL به نقطه ورود (بدون ریسک برای باقیمانده)
                tp1_hit_now = (latest_high >= trade['tp1']) if side == "BUY" else (latest_low <= trade['tp1'])
                if not trade['tp1_hit'] and tp1_hit_now:
                    pnl_leg = self._pnl_pct(side, entry, trade['tp1'])
                    trade['realized_pnl_contribution'] += pnl_leg * self.TP1_PORTION
                    trade['remaining_pct'] -= self.TP1_PORTION
                    trade['tp1_hit'] = True
                    trade['sl'] = entry  # breakeven
                    self.telegram.send_personal_message(
                        f"🟡 **TP1 زده شد - {trade['symbol']} ({side})**\n"
                        f"۵۰٪ پوزیشن با {pnl_leg:+.2f}% بسته شد. SL به نقطه ورود (Breakeven) منتقل شد."
                    )

                # ۳. TP2 - بستن ۳۰٪ دیگر و انتقال SL به سطح TP1 (قفل کردن بخشی از سود)
                tp2_hit_now = (latest_high >= trade['tp2']) if side == "BUY" else (latest_low <= trade['tp2'])
                if trade['tp1_hit'] and not trade['tp2_hit'] and tp2_hit_now:
                    pnl_leg = self._pnl_pct(side, entry, trade['tp2'])
                    trade['realized_pnl_contribution'] += pnl_leg * self.TP2_PORTION
                    trade['remaining_pct'] -= self.TP2_PORTION
                    trade['tp2_hit'] = True
                    trade['sl'] = trade['tp1']  # قفل کردن سود تا سطح TP1
                    self.telegram.send_personal_message(
                        f"🟡 **TP2 زده شد - {trade['symbol']} ({side})**\n"
                        f"۳۰٪ دیگر با {pnl_leg:+.2f}% بسته شد. SL به سطح TP1 منتقل شد."
                    )

                # ۴. TP3 - بستن کامل باقیمانده پوزیشن
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
            # محدود کردن فاصله‌ی SL بین 0.8×ATR (خیلی تنگ نشه) و 2.5×ATR (خیلی گشاد نشه)
            # تا هم از نویز در امان باشه هم TP ها به فاصله‌ی غیرمنطقی دور نرن
            min_dist = 0.8 * atr
            max_dist = 2.5 * atr
            dist = min(max(price - raw_sl, min_dist), max_dist)
            stop_loss = round(price - dist, 4)
            risk = price - stop_loss
            tp1 = round(price + (1.5 * risk), 4)
            tp2 = round(price + (2.5 * risk), 4)
            tp3 = round(price + (4.2 * risk), 4)
            trailing_step = round(price + (1.0 * risk), 4)
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
            trailing_step = round(price - (1.0 * risk), 4)

        # --- Position Sizing واقعی: چند واحد از دارایی بر اساس درصد ریسک ثابت باید خرید/فروخت ---
        # سایز به‌صورت محدود با confidence هوش مصنوعی تنظیم می‌شه (سیگنال قوی‌تر = سایز کمی بزرگ‌تر)
        # ولی هیچ‌وقت از MAX_RISK_PER_TRADE_PCT بیشتر نمی‌شه، صرف‌نظر از confidence
        confidence_multiplier = 0.8 + (0.4 * min(max(confidence, 0.0), 1.0))  # بازه: 0.8x تا 1.2x
        effective_risk_pct = min(self.config.RISK_PER_TRADE_PCT * confidence_multiplier, self.config.MAX_RISK_PER_TRADE_PCT)
        risk_amount_usdt = self.config.ACCOUNT_BALANCE_USDT * (effective_risk_pct / 100.0)
        position_size_units = round(risk_amount_usdt / risk, 6) if risk > 0 else 0
        position_value_usdt = round(position_size_units * price, 2)

        message = f"""
{emoji} *ULTRA SIGNAL: {side} / {direction}*

📍 *Symbol:* {symbol}
⏱ *Timeframe:* {self.config.ENTRY_TIMEFRAME} (Trend 4H: {trend_4h})

💵 *Entry Price:* {price:,}

🎯 *Dynamic Targets (partial exits: 50% / 30% / 20%):*
  1️⃣ TP1 (50%): {tp1:,}
  2️⃣ TP2 (30%): {tp2:,}
  3️⃣ TP3 (20%, Max Yield): {tp3:,}

🛑 *Stop-Loss:* {stop_loss:,}
⚙️ *Auto Trailing:* SL → Breakeven after TP1, SL → TP1 after TP2

💰 *Position Size (Risk {effective_risk_pct:.2f}% of {self.config.ACCOUNT_BALANCE_USDT:,.0f} USDT):* {position_size_units} واحد (~{position_value_usdt:,} USDT)

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
            logger.info(f"سیگنال فوق‌پیشرفته {side} برای {symbol} ارسال شد")
            
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

    def process_symbol(self, symbol: str):
        try:
            df_15m = self.data.fetch_ohlcv(symbol, timeframe=self.config.ENTRY_TIMEFRAME)
            df_15m = self.analysis.calculate_indicators(df_15m)

            df_1h = self.data.fetch_ohlcv(symbol, timeframe="1h")
            df_1h = self.analysis.calculate_indicators(df_1h)
            trend_1h = self.analysis.get_major_trend(df_1h)

            df_4h = self.data.fetch_ohlcv(symbol, timeframe=self.config.TREND_TIMEFRAME)
            df_4h = self.analysis.calculate_indicators(df_4h)

            trend_4h = self.analysis.get_major_trend(df_4h)

            rule_signal = self.signal_engine.get_rule_signal(df_15m, trend_4h, trend_1h)
            if not rule_signal:
                return

            # سقف کلی معاملات همزمان برای جلوگیری از قرارگیری بیش‌ازحد در معرض ریسک
            if len(self.paper_trader.active_trades) >= self.config.MAX_CONCURRENT_TRADES:
                logger.info(f"سقف معاملات همزمان پره، سیگنال {symbol} نادیده گرفته شد")
                return

            # جلوگیری از باز کردن چند پوزیشن روی یک نماد
            open_for_symbol = sum(1 for t in self.paper_trader.active_trades.values() if t['symbol'] == symbol)
            if open_for_symbol >= self.config.MAX_TRADES_PER_SYMBOL:
                return

            now = datetime.now()
            if symbol in self.last_signal_time:
                if now - self.last_signal_time[symbol] < timedelta(minutes=self.config.SIGNAL_COOLDOWN_MINUTES):
                    return

            latest = df_15m.iloc[-1]
            ai_result = self.analysis.get_ai_confirmation(symbol, rule_signal, df_15m, trend_4h)

            if ai_result["confidence"] >= self.config.MIN_CONFIDENCE_AI:
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
        logger.info("----- شروع آنالیز پیشرفته بازار -----")
        for symbol in self.config.SYMBOLS:
            self.process_symbol(symbol)
            time.sleep(1.5)
            
        self.paper_trader.update_and_check_trades(self.data)

    def start(self):
        logger.info("بات V5 Ultimate Pro فعال شد")
        start_message = "⚡️ **نسخه جامع V5 Ultimate Pro فعال شد.**\n\nسیستم با تحلیل چند تایم‌فریم (MTF) و تریلینگ استاپ آماده‌سازی شد."
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
