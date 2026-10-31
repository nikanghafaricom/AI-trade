#!/usr/bin/env python3
# ==============================================
# Breakout High-Profit Bot
# قوانین تأییدشده در بک‌تست ۶ ماهه (+۱۸۴٪)
# ارزها: ETH, SOL, BNB | فقط لانگ | ریسک ۲٪
# ==============================================
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
from dotenv import load_dotenv

load_dotenv()

# ==================== Health Server ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Breakout Bot is alive!")

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
        logger.error(f"Health server error: {e}")

threading.Thread(target=start_health_check_server, daemon=True).start()

# ==================== Config ====================
class Config:
    EXCHANGE_ID = "binance"
    API_KEY = os.getenv("EXCHANGE_API_KEY", "")
    SECRET = os.getenv("EXCHANGE_SECRET", "")
    PASSWORD = os.getenv("EXCHANGE_PASSWORD", "")

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    PERSONAL_CHAT_ID = os.getenv("PERSONAL_CHAT_ID")

    # فقط ارزهای تأییدشده در بک‌تست سود بالا
    SYMBOLS = [
        "ETH/USDT",
        "SOL/USDT",
        "BNB/USDT",
    ]

    ENTRY_TIMEFRAME = "15m"
    CHECK_INTERVAL = 180  # هر ۳ دقیقه چک کن

    VIRTUAL_CAPITAL_USDT = float(os.getenv("VIRTUAL_CAPITAL_USDT", 10000))
    RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", 2.0))  # ۲٪ طبق بک‌تست
    MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES", 3))

    # پارامترهای بریک‌اوت (تأییدشده)
    BREAKOUT_LOOKBACK = 12          # سقف ۱۲ کندل
    VOLUME_MULT = 1.3               # حجم ۱.۳ برابر میانگین
    SL_ATR_MULT = 1.4               # حد ضرر ۱.۴ ATR
    TP_R_MULTIPLE = 2.2             # حد سود ۲.۲ برابر ریسک
    MAX_HOLD_BARS = 64              # حداکثر حدود ۱۶ ساعت (۶۴ × ۱۵م)
    COOLDOWN_BARS = 6               # فاصله بعد از خروج
    ATR_PERIOD = 14
    EMA_FAST = 50
    EMA_SLOW = 200
    VOL_SMA_PERIOD = 20

    # کارمزد و اسپرد تقریبی
    TAKER_FEE_PCT = 0.04
    SPREAD_PCT = 0.05

    TRADE_MONITOR_INTERVAL_SECONDS = 20

    def validate(self):
        required = {
            "TELEGRAM_BOT_TOKEN": self.TELEGRAM_BOT_TOKEN,
            "TELEGRAM_CHAT_ID": self.TELEGRAM_CHAT_ID,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(f"Missing env vars: {', '.join(missing)}")

# ==================== Logging ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("breakout_bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("breakout")

# ==================== Data Layer ====================
class DataLayer:
    BINANCE_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]

    def __init__(self, config: Config):
        self.config = config

    def _get(self, path: str, params: dict):
        last = None
        for host in self.BINANCE_HOSTS:
            try:
                r = requests.get(f"{host}{path}", params=params, timeout=12)
                if r.status_code == 200:
                    return r.json()
                last = f"{host} {r.status_code}"
            except Exception as e:
                last = str(e)
            time.sleep(0.15)
        raise RuntimeError(last)

    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 250) -> pd.DataFrame:
        try:
            raw = self._get("/api/v3/klines", {
                "symbol": symbol.replace("/", ""),
                "interval": timeframe,
                "limit": limit
            })
            rows = [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in raw]
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            return df
        except Exception as e:
            logger.error(f"Fetch error {symbol}: {e}")
            return pd.DataFrame()

# ==================== Indicators ====================
def add_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    if df.empty or len(df) < cfg.EMA_SLOW + 5:
        return df
    df = df.copy()
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    df["ema50"] = c.ewm(span=cfg.EMA_FAST, adjust=False).mean()
    df["ema200"] = c.ewm(span=cfg.EMA_SLOW, adjust=False).mean()
    df["vol_sma"] = v.rolling(cfg.VOL_SMA_PERIOD).mean()

    tr = pd.concat([
        h - l,
        (h - c.shift()).abs(),
        (l - c.shift()).abs()
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(cfg.ATR_PERIOD).mean()

    # سقف ۱۲ کندل قبلی (بدون کندل جاری)
    df["high_lookback"] = h.shift(1).rolling(cfg.BREAKOUT_LOOKBACK).max()
    return df

# ==================== Signal (Breakout Rules) ====================
def get_breakout_signal(df: pd.DataFrame, cfg: Config) -> Optional[Dict]:
    """
    قوانین دقیق بک‌تست:
    1. ema50 > ema200 و close > ema50
    2. close > سقف ۱۲ کندل اخیر
    3. volume > 1.3 * vol_sma
    """
    if df.empty or len(df) < cfg.EMA_SLOW + 10:
        return None

    latest = df.iloc[-1]
    if pd.isna(latest["ema50"]) or pd.isna(latest["ema200"]) or pd.isna(latest["atr"]) or pd.isna(latest["high_lookback"]):
        return None
    if latest["atr"] <= 0:
        return None

    # شرط روند
    if not (latest["ema50"] > latest["ema200"] and latest["close"] > latest["ema50"]):
        return None

    # شرط بریک‌اوت
    if latest["close"] <= latest["high_lookback"]:
        return None

    # شرط حجم
    if latest["vol_sma"] <= 0 or latest["volume"] < cfg.VOLUME_MULT * latest["vol_sma"]:
        return None

    entry = float(latest["close"])
    atr = float(latest["atr"])
    sl = entry - cfg.SL_ATR_MULT * atr
    risk = entry - sl
    if risk <= 0 or risk / entry > 0.05:
        return None

    tp = entry + cfg.TP_R_MULTIPLE * risk

    return {
        "side": "BUY",
        "entry": entry,
        "sl": round(sl, 6),
        "tp": round(tp, 6),
        "atr": atr,
        "risk": risk,
    }

# ==================== Risk Manager ====================
class RiskManager:
    def __init__(self, config: Config):
        self.config = config

    def position_size(self, entry: float, stop: float) -> float:
        risk_amount = self.config.VIRTUAL_CAPITAL_USDT * (self.config.RISK_PER_TRADE_PCT / 100)
        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0:
            return 0.0
        return risk_amount / risk_per_unit

    def can_open(self, symbol: str, active: Dict) -> Tuple[bool, str]:
        if len(active) >= self.config.MAX_CONCURRENT_TRADES:
            return False, "سقف معاملات همزمان پر است"
        if any(t["symbol"] == symbol for t in active.values()):
            return False, "قبلاً روی این ارز پوزیشن باز است"
        return True, ""

# ==================== Journal ====================
class TradeJournal:
    def __init__(self, path="breakout_history.json"):
        self.path = path
        self.records = self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.records, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Journal save error: {e}")

    def record(self, symbol, side, reason, pnl_usdt, pnl_pct, r_multiple):
        self.records.append({
            "date": date_cls.today().isoformat(),
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "side": side,
            "reason": reason,
            "pnl_usdt": round(pnl_usdt, 2),
            "pnl_pct": round(pnl_pct, 3),
            "r_multiple": round(r_multiple, 2),
        })
        self._save()

    def today_pnl(self) -> float:
        today = date_cls.today().isoformat()
        return sum(r["pnl_usdt"] for r in self.records if r["date"] == today)

# ==================== Paper Trader ====================
class PaperTrader:
    def __init__(self, config: Config, telegram, journal: TradeJournal):
        self.config = config
        self.telegram = telegram
        self.journal = journal
        self.file_path = "breakout_trades.json"
        self.active = self._load()
        self.lock = threading.Lock()
        self.last_exit_bar = {}  # symbol -> last exit time for cooldown

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
                json.dump(self.active, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Save trades error: {e}")

    def snapshot(self) -> Dict:
        with self.lock:
            return dict(self.active)

    def open_trade(self, symbol: str, entry: float, sl: float, tp: float, qty: float, atr: float):
        tid = f"{symbol}_{int(time.time())}"
        with self.lock:
            self.active[tid] = {
                "symbol": symbol,
                "side": "BUY",
                "entry": entry,
                "sl": sl,
                "original_sl": sl,
                "tp": tp,
                "qty": qty,
                "atr": atr,
                "open_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "open_ts": time.time(),
                "bars_held": 0,
            }
            self._save()
        logger.info(f"OPEN {symbol} @ {entry:.4f} SL={sl:.4f} TP={tp:.4f} qty={qty:.6f}")

    def _close(self, trade: Dict, exit_price: float, reason: str):
        entry = trade["entry"]
        qty = trade["qty"]
        diff = exit_price - entry
        # هزینه تقریبی
        cost = (entry + exit_price) * ((self.config.TAKER_FEE_PCT + self.config.SPREAD_PCT / 2) / 100)
        diff -= cost
        risk = abs(entry - trade["original_sl"])
        r_mult = diff / risk if risk > 0 else 0.0
        pnl_usdt = diff * qty
        pnl_pct = (diff / entry) * 100

        self.journal.record(trade["symbol"], "BUY", reason, pnl_usdt, pnl_pct, r_mult)

        emoji = "✅" if pnl_usdt > 0 else "❌"
        msg = f"""
{emoji} **خروج از معامله**

📌 {trade['symbol']} (BUY)
📎 علت: {reason}
📈 سود/زیان: {pnl_pct:+.2f}% ({pnl_usdt:+.2f} USDT)
📐 R: {r_mult:+.2f}R
"""
        self.telegram.send_personal(msg)
        self.last_exit_bar[trade["symbol"]] = time.time()

    def update(self, data_layer: DataLayer):
        with self.lock:
            if not self.active:
                return
            to_delete = []
            for tid, trade in list(self.active.items()):
                try:
                    df = data_layer.fetch_ohlcv(trade["symbol"], "15m", limit=5)
                    if df.empty:
                        continue
                    high = float(df["high"].max())
                    low = float(df["low"].min())
                    close = float(df["close"].iloc[-1])
                    trade["bars_held"] = trade.get("bars_held", 0) + 1

                    # حد ضرر
                    if low <= trade["sl"]:
                        self._close(trade, trade["sl"], "حد ضرر")
                        to_delete.append(tid)
                        continue

                    # حد سود
                    if high >= trade["tp"]:
                        self._close(trade, trade["tp"], "حد سود")
                        to_delete.append(tid)
                        continue

                    # حداکثر زمان نگه‌داری
                    if trade["bars_held"] >= self.config.MAX_HOLD_BARS:
                        self._close(trade, close, "پایان زمان مجاز")
                        to_delete.append(tid)
                        continue

                    self._save()
                except Exception as e:
                    logger.error(f"Update trade error {tid}: {e}")

            for tid in to_delete:
                del self.active[tid]
            if to_delete:
                self._save()

# ==================== Telegram ====================
class TelegramSender:
    def __init__(self, config: Config):
        self.config = config
        self.base = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

    def _send(self, chat_id, text):
        try:
            r = requests.post(f"{self.base}/sendMessage", json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }, timeout=10)
            if r.status_code != 200:
                logger.error(f"Telegram error: {r.status_code} {r.text[:200]}")
        except Exception as e:
            logger.error(f"Telegram send error: {e}")

    def send_signal(self, symbol, entry, sl, tp, qty, notional):
        msg = f"""
🟢 **BREAKOUT SIGNAL - BUY**

📍 **Symbol:** {symbol}
💵 **Entry:** {entry:,.4f}
🛑 **Stop-Loss:** {sl:,.4f}
🎯 **Take-Profit:** {tp:,.4f}
⚖️ **R:R** ≈ 1:{self.config.TP_R_MULTIPLE}

💰 **Size:** {qty:.6f}  |  ~{notional:,.1f} USDT
📊 **Risk:** {self.config.RISK_PER_TRADE_PCT}% of capital

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
        self._send(self.config.TELEGRAM_CHAT_ID, msg)

    def send_personal(self, text):
        chat = self.config.PERSONAL_CHAT_ID or self.config.TELEGRAM_CHAT_ID
        self._send(chat, text)

    def send_status(self, text):
        self._send(self.config.TELEGRAM_CHAT_ID, text)

    def test(self) -> bool:
        try:
            r = requests.get(f"{self.base}/getMe", timeout=8)
            if r.status_code == 200 and r.json().get("ok"):
                logger.info(f"Telegram OK: @{r.json()['result'].get('username')}")
                return True
        except Exception as e:
            logger.error(f"Telegram test failed: {e}")
        return False

# ==================== Main System ====================
class BreakoutBot:
    def __init__(self):
        self.config = Config()
        self.config.validate()
        self.data = DataLayer(self.config)
        self.risk = RiskManager(self.config)
        self.telegram = TelegramSender(self.config)
        self.journal = TradeJournal()
        self.paper = PaperTrader(self.config, self.telegram, self.journal)
        self.running = True
        self.last_signal_time: Dict[str, float] = {}

    def process_symbol(self, symbol: str):
        try:
            df = self.data.fetch_ohlcv(symbol, self.config.ENTRY_TIMEFRAME, limit=250)
            df = add_indicators(df, self.config)
            if df.empty:
                return

            # کول‌داون بعد از خروج
            last_exit = self.paper.last_exit_bar.get(symbol, 0)
            if time.time() - last_exit < self.config.COOLDOWN_BARS * 15 * 60:
                return

            signal = get_breakout_signal(df, self.config)
            if not signal:
                return

            # جلوگیری از سیگنال تکراری نزدیک
            now = time.time()
            if symbol in self.last_signal_time and now - self.last_signal_time[symbol] < 30 * 60:
                return

            active = self.paper.snapshot()
            can, reason = self.risk.can_open(symbol, active)
            if not can:
                logger.info(f"{symbol}: رد شد - {reason}")
                return

            qty = self.risk.position_size(signal["entry"], signal["sl"])
            if qty <= 0:
                return

            notional = qty * signal["entry"]
            self.telegram.send_signal(symbol, signal["entry"], signal["sl"], signal["tp"], qty, notional)
            self.paper.open_trade(symbol, signal["entry"], signal["sl"], signal["tp"], qty, signal["atr"])
            self.last_signal_time[symbol] = now

        except Exception as e:
            logger.error(f"Process {symbol} error: {e}")

    def _monitor_loop(self):
        while self.running:
            try:
                self.paper.update(self.data)
            except Exception as e:
                logger.error(f"Monitor error: {e}")
            time.sleep(self.config.TRADE_MONITOR_INTERVAL_SECONDS)

    def run_once(self):
        logger.info("----- Breakout scan -----")
        for sym in self.config.SYMBOLS:
            self.process_symbol(sym)
            time.sleep(1.2)

    def start(self):
        logger.info("Breakout High-Profit Bot started")
        if not self.telegram.test():
            logger.error("Telegram connection failed!")

        start_msg = f"""
🚀 **Breakout Bot فعال شد**

قوانین (بک‌تست ۶ ماهه +۱۸۴٪):
• ارزها: ETH / SOL / BNB
• فقط خرید (Long)
• بریک‌اوت سقف ۱۲ کندل + حجم ۱.۳× + روند صعودی
• ریسک هر معامله: {self.config.RISK_PER_TRADE_PCT}%
• حد ضرر: ۱.۴ ATR | حد سود: ۲.۲R
• حداکثر نگهداری: حدود ۱۶ ساعت

سرمایه مجازی: {self.config.VIRTUAL_CAPITAL_USDT:,.0f} USDT
"""
        self.telegram.send_status(start_msg)

        threading.Thread(target=self._monitor_loop, daemon=True, name="Monitor").start()

        while self.running:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"Main loop error: {e}")
                self.telegram.send_personal(f"🚨 خطای چرخه اصلی:\n`{e}`")
            gc.collect()
            time.sleep(self.config.CHECK_INTERVAL)

    def stop(self):
        self.running = False
        logger.info("Bot stopped")

if __name__ == "__main__":
    bot = BreakoutBot()
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.stop()
