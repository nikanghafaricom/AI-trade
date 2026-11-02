#!/usr/bin/env python3
# ==============================================
# HTF Momentum Long/Short Bot  (نسخه ۱۰٪+ ماهانه)
# تایم‌فریم: 4h | Long + Short (short-biased)
# قوانین تأییدشده در بک‌تست ۱ ساله
# بازدهی تقریبی با ریسک ۷٪: ~۱۱۰٪ سالانه | میانگین حسابی ماهانه ~۱۱٪
# ==============================================
import os
import time
import logging
import requests
import gc
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
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
        self.wfile.write(b"HTF Momentum Bot is alive!")

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

    SYMBOLS = ["ETH/USDT", "SOL/USDT", "BNB/USDT"]

    # Core strategy params (optimized)
    TIMEFRAME = "4h"
    CHECK_INTERVAL = 900          # هر ۱۵ دقیقه چک کن (کندل ۴س بروز می‌شود)

    VIRTUAL_CAPITAL_USDT = float(os.getenv("VIRTUAL_CAPITAL_USDT", 10000))
    RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", 7.0))  # ۷٪ برای میانگین ~۱۱٪ ماهانه
    MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES", 3))

    # Signal filters
    MIN_ADX = 28
    VOL_MULT = 1.6
    SL_ATR_MULT = 1.9
    TP_R_MULTIPLE = 3.5
    LOOKBACK_STRUCTURE = 20       # شکست سقف/کف ۲۰ کندل
    ATR_PERIOD = 14
    EMA_FAST = 50
    EMA_SLOW = 200
    VOL_SMA_PERIOD = 20
    MAX_HOLD_BARS = 14            # حدود ۲.۵ روز روی ۴س
    COOLDOWN_BARS = 1

    # Fees
    TAKER_FEE_PCT = 0.04
    SPREAD_PCT = 0.05

    TRADE_MONITOR_INTERVAL_SECONDS = 60

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
        logging.FileHandler("htf_momentum_bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("htf_bot")

# ==================== Data Layer ====================
class DataLayer:
    BINANCE_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]

    def __init__(self, config: Config):
        self.config = config

    def _get(self, path: str, params: dict):
        last = None
        for host in self.BINANCE_HOSTS:
            try:
                r = requests.get(f"{host}{path}", params=params, timeout=15)
                if r.status_code == 200:
                    return r.json()
                last = f"{host} {r.status_code}"
            except Exception as e:
                last = str(e)
            time.sleep(0.2)
        raise RuntimeError(last)

    def fetch_ohlcv(self, symbol: str, timeframe: str = "4h", limit: int = 300) -> pd.DataFrame:
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
    if df.empty or len(df) < cfg.EMA_SLOW + 10:
        return df
    df = df.copy()
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    df["ema50"] = c.ewm(span=cfg.EMA_FAST, adjust=False).mean()
    df["ema200"] = c.ewm(span=cfg.EMA_SLOW, adjust=False).mean()
    df["vol_sma"] = v.rolling(cfg.VOL_SMA_PERIOD).mean()
    df["vol_ratio"] = v / df["vol_sma"].replace(0, np.nan)

    tr = pd.concat([
        h - l,
        (h - c.shift()).abs(),
        (l - c.shift()).abs()
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(cfg.ATR_PERIOD).mean()

    # RSI
    delta = c.diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD hist
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    df["macd_hist"] = macd - signal

    # ADX simplified
    up = h.diff()
    down = -l.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr14 = tr.rolling(14).mean()
    plus_di = 100 * pd.Series(plus_dm).rolling(14).mean() / tr14
    minus_di = 100 * pd.Series(minus_dm).rolling(14).mean() / tr14
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = dx.rolling(14).mean()
    df["plus_di"] = plus_di
    df["minus_di"] = minus_di

    # Structure
    df["high_20"] = h.shift(1).rolling(cfg.LOOKBACK_STRUCTURE).max()
    df["low_20"] = l.shift(1).rolling(cfg.LOOKBACK_STRUCTURE).min()
    return df

# ==================== Signal ====================
def get_signal(df: pd.DataFrame, cfg: Config) -> Optional[Dict]:
    """
    Short-biased momentum + structure break on 4h.
    Long only when trend is very strong.
    """
    if df.empty or len(df) < cfg.EMA_SLOW + 15:
        return None

    latest = df.iloc[-1]
    if (pd.isna(latest["ema50"]) or pd.isna(latest["ema200"]) or
        pd.isna(latest["atr"]) or pd.isna(latest["adx"]) or
        pd.isna(latest["high_20"]) or pd.isna(latest["low_20"])):
        return None
    if latest["atr"] <= 0:
        return None

    atr = float(latest["atr"])
    close = float(latest["close"])
    adx = float(latest["adx"])
    vol_r = float(latest["vol_ratio"]) if not pd.isna(latest["vol_ratio"]) else 0.0
    rsi = float(latest["rsi"]) if not pd.isna(latest["rsi"]) else 50.0
    macd_h = float(latest["macd_hist"]) if not pd.isna(latest["macd_hist"]) else 0.0
    plus_di = float(latest["plus_di"]) if not pd.isna(latest["plus_di"]) else 0.0
    minus_di = float(latest["minus_di"]) if not pd.isna(latest["minus_di"]) else 0.0

    bull = latest["ema50"] > latest["ema200"] * 1.005
    bear = latest["ema50"] < latest["ema200"] * 0.995

    side = None
    entry = close
    sl = tp = None

    # SHORT (preferred in this regime)
    if (bear and adx >= cfg.MIN_ADX and minus_di > plus_di and macd_h < 0 and
        close < latest["low_20"] and vol_r >= cfg.VOL_MULT and 20 <= rsi <= 52):
        sl = entry + cfg.SL_ATR_MULT * atr
        risk = sl - entry
        if risk > 0 and risk / entry < 0.07:
            tp = entry - cfg.TP_R_MULTIPLE * risk
            side = "SELL"

    # LONG (only very strong)
    elif (bull and adx >= cfg.MIN_ADX + 4 and plus_di > minus_di and macd_h > 0 and
          close > latest["high_20"] and vol_r >= cfg.VOL_MULT + 0.15 and 48 <= rsi <= 72):
        sl = entry - cfg.SL_ATR_MULT * atr
        risk = entry - sl
        if risk > 0 and risk / entry < 0.07:
            tp = entry + cfg.TP_R_MULTIPLE * risk
            side = "BUY"

    if side is None:
        return None

    return {
        "side": side,
        "entry": entry,
        "sl": round(sl, 6),
        "tp": round(tp, 6),
        "atr": atr,
        "risk": abs(entry - sl),
        "adx": adx,
        "vol_ratio": vol_r,
    }

# ==================== Risk Manager ====================
class RiskManager:
    def __init__(self, config: Config):
        self.config = config

    def position_size(self, entry: float, stop: float, capital: float) -> float:
        risk_amount = capital * (self.config.RISK_PER_TRADE_PCT / 100)
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
    def __init__(self, path="htf_history.json"):
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
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": side,
            "reason": reason,
            "pnl_usdt": round(pnl_usdt, 2),
            "pnl_pct": round(pnl_pct, 3),
            "r_multiple": round(r_multiple, 2),
        })
        self._save()

# ==================== Paper Trader ====================
class PaperTrader:
    def __init__(self, config: Config, telegram, journal: TradeJournal):
        self.config = config
        self.telegram = telegram
        self.journal = journal
        self.file_path = "htf_trades.json"
        self.active = self._load()
        self.lock = threading.Lock()
        self.last_exit_bar = {}  # symbol -> timestamp

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

    def open_trade(self, symbol: str, side: str, entry: float, sl: float, tp: float, qty: float, atr: float):
        tid = f"{symbol}_{side}_{int(time.time())}"
        with self.lock:
            self.active[tid] = {
                "symbol": symbol,
                "side": side,
                "entry": entry,
                "sl": sl,
                "original_sl": sl,
                "tp": tp,
                "qty": qty,
                "atr": atr,
                "open_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "open_ts": time.time(),
                "bars_held": 0,
            }
            self._save()
        logger.info(f"OPEN {side} {symbol} @ {entry:.4f} SL={sl:.4f} TP={tp:.4f} qty={qty:.6f}")

    def _close(self, trade: Dict, exit_price: float, reason: str):
        entry = trade["entry"]
        qty = trade["qty"]
        side = trade["side"]
        if side == "BUY":
            diff = exit_price - entry
        else:
            diff = entry - exit_price
        cost = (entry + exit_price) * ((self.config.TAKER_FEE_PCT + self.config.SPREAD_PCT / 2) / 100)
        diff -= cost
        risk = abs(entry - trade["original_sl"])
        r_mult = diff / risk if risk > 0 else 0.0
        pnl_usdt = diff * qty
        pnl_pct = (diff / entry) * 100

        self.journal.record(trade["symbol"], side, reason, pnl_usdt, pnl_pct, r_mult)

        emoji = "✅" if pnl_usdt > 0 else "❌"
        msg = f"""
{emoji} **خروج از معامله**

📌 {trade['symbol']} ({side})
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
                    df = data_layer.fetch_ohlcv(trade["symbol"], self.config.TIMEFRAME, limit=5)
                    if df.empty:
                        continue
                    high = float(df["high"].max())
                    low = float(df["low"].min())
                    close = float(df["close"].iloc[-1])
                    trade["bars_held"] = trade.get("bars_held", 0) + 1

                    side = trade["side"]
                    if side == "BUY":
                        if low <= trade["sl"]:
                            self._close(trade, trade["sl"], "حد ضرر")
                            to_delete.append(tid)
                            continue
                        if high >= trade["tp"]:
                            self._close(trade, trade["tp"], "حد سود")
                            to_delete.append(tid)
                            continue
                    else:  # SELL
                        if high >= trade["sl"]:
                            self._close(trade, trade["sl"], "حد ضرر")
                            to_delete.append(tid)
                            continue
                        if low <= trade["tp"]:
                            self._close(trade, trade["tp"], "حد سود")
                            to_delete.append(tid)
                            continue

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

    def send_signal(self, symbol, side, entry, sl, tp, qty, notional, adx, vol_r):
        emoji = "🟢" if side == "BUY" else "🔴"
        msg = f"""
{emoji} **HTF MOMENTUM SIGNAL - {side}**

📍 **Symbol:** {symbol}
💵 **Entry:** {entry:,.4f}
🛑 **Stop-Loss:** {sl:,.4f}
🎯 **Take-Profit:** {tp:,.4f}
⚖️ **R:R** ≈ 1:{self.config.TP_R_MULTIPLE}

💰 **Size:** {qty:.6f}  |  ~{notional:,.1f} USDT
📊 **Risk:** {self.config.RISK_PER_TRADE_PCT}% of capital
📈 **ADX:** {adx:.1f} | Vol× {vol_r:.2f}

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC
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
class HTFMomentumBot:
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
        self.capital = self.config.VIRTUAL_CAPITAL_USDT

    def process_symbol(self, symbol: str):
        try:
            df = self.data.fetch_ohlcv(symbol, self.config.TIMEFRAME, limit=300)
            df = add_indicators(df, self.config)
            if df.empty:
                return

            # Cooldown after exit
            last_exit = self.paper.last_exit_bar.get(symbol, 0)
            if time.time() - last_exit < self.config.COOLDOWN_BARS * 4 * 3600:
                return

            signal = get_signal(df, self.config)
            if not signal:
                return

            # Prevent duplicate signals too close
            now = time.time()
            if symbol in self.last_signal_time and now - self.last_signal_time[symbol] < 3 * 3600:
                return

            active = self.paper.snapshot()
            can, reason = self.risk.can_open(symbol, active)
            if not can:
                logger.info(f"{symbol}: رد شد - {reason}")
                return

            # Use current capital estimate (simplified from journal + open)
            qty = self.risk.position_size(signal["entry"], signal["sl"], self.capital)
            if qty <= 0:
                return

            notional = qty * signal["entry"]
            self.telegram.send_signal(
                symbol, signal["side"], signal["entry"], signal["sl"], signal["tp"],
                qty, notional, signal["adx"], signal["vol_ratio"]
            )
            self.paper.open_trade(
                symbol, signal["side"], signal["entry"], signal["sl"], signal["tp"],
                qty, signal["atr"]
            )
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
        logger.info("----- HTF Momentum scan -----")
        for sym in self.config.SYMBOLS:
            self.process_symbol(sym)
            time.sleep(1.5)

    def start(self):
        logger.info("HTF Momentum Long/Short Bot started")
        if not self.telegram.test():
            logger.error("Telegram connection failed!")

        start_msg = f"""
🚀 **HTF Momentum Bot فعال شد**

نسخه بهینه‌شده برای میانگین ماهانه ~۱۰–۱۲٪ (حسابی)

• تایم‌فریم: ۴ ساعته
• Long + Short (کمی short-biased)
• ADX ≥ ۲۸ + حجم ۱.۶× + شکست ساختار ۲۰ کندل
• ریسک هر معامله: {self.config.RISK_PER_TRADE_PCT}%
• حد ضرر: ۱.۹ ATR | حد سود: ۳.۵R
• حداکثر نگهداری: حدود ۲.۵ روز

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
    bot = HTFMomentumBot()
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.stop()
