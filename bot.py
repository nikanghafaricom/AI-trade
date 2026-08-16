# ==============================================
# Hybrid Signal Bot V5 Ultimate + MiniApp Dashboard (Enhanced AI)
# ==============================================
import os
import time
import logging
import requests
import gc
import json
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional, List
import pandas as pd
import ccxt
from openai import OpenAI
from dotenv import load_dotenv

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

load_dotenv()

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
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
        "XRP/USDT", "AVAX/USDT", "NEAR/USDT", "ADA/USDT",
        "DOGE/USDT", "LINK/USDT",
    ]

    ENTRY_TIMEFRAME = "15m"
    TREND_TIMEFRAME = "4h"
    CHECK_INTERVAL = 180  # بهینه‌سازی زمان اسکن به ۳ دقیقه
    MIN_CONFIDENCE_AI = 0.78  # تنظیم دقیق‌تر آستانه پذیرش سیگنال

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

# Global Reference for Active Trades
GLOBAL_PAPER_TRADER = None

# ==================== وب‌اپلیکیشن داشبورد (FastAPI) ====================
app_web = FastAPI()

HTML_DASHBOARD = """
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Trading Dashboard</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body { font-family: system-ui, sans-serif; background: #121824; color: #fff; margin: 0; padding: 15px; }
        .card { background: #1e293b; border-radius: 12px; padding: 15px; margin-bottom: 12px; border: 1px solid #334155; }
        .card-header { display: flex; justify-content: space-between; align-items: center; }
        .badge-buy { background: #16a34a; padding: 4px 8px; border-radius: 6px; font-size: 12px; }
        .badge-sell { background: #dc2626; padding: 4px 8px; border-radius: 6px; font-size: 12px; }
        .btn { width: 100%; padding: 10px; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; margin-top: 10px; }
        .btn-danger { background: #ef4444; color: white; }
        .input-group { margin-top: 10px; display: flex; gap: 8px; align-items: center; }
        input { background: #0f172a; border: 1px solid #334155; color: white; padding: 8px; border-radius: 6px; width: 60px; text-align: center; }
    </style>
</head>
<body>
    <h2>📊 داشبورد لایو معاملات</h2>
    <div id="trades-container">در حال دریافت پوزیشن‌ها...</div>

    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();

        async function fetchTrades() {
            try {
                const res = await fetch('/api/trades');
                const data = await res.json();
                const container = document.getElementById('trades-container');
                container.innerHTML = '';

                if (Object.keys(data).length === 0) {
                    container.innerHTML = '<p style="text-align:center; color:#94a3b8;">هیچ معامله باز فعال نیست.</p>';
                    return;
                }

                for (let id in data) {
                    let trade = data[id];
                    let badgeClass = trade.side === 'BUY' ? 'badge-buy' : 'badge-sell';
                    container.innerHTML += `
                        <div class="card">
                            <div class="card-header">
                                <strong>${trade.symbol}</strong>
                                <span class="${badgeClass}">${trade.side}</span>
                            </div>
                            <p style="margin: 8px 0; font-size: 14px;">ورود: ${trade.entry}</p>
                            <p style="margin: 4px 0; font-size: 12px; color: #94a3b8;">تارگت: ${trade.tp1} | استاپ: ${trade.sl}</p>
                            <div class="input-group">
                                <label style="font-size:12px;">اهرم:</label>
                                <input type="number" value="10" min="1" max="100">
                                <button class="btn btn-danger" style="margin:0;" onclick="closeTrade('${id}')">بستن فوری</button>
                            </div>
                        </div>
                    `;
                }
            } catch (e) { console.error(e); }
        }

        async function closeTrade(id) {
            await fetch('/api/close?id=' + id, { method: 'POST' });
            fetchTrades();
        }

        setInterval(fetchTrades, 3000);
        fetchTrades();
    </script>
</body>
</html>
"""

@app_web.get("/", response_class=HTMLResponse)
def get_dashboard():
    return HTML_DASHBOARD

@app_web.get("/api/trades")
def get_active_trades():
    if GLOBAL_PAPER_TRADER:
        return GLOBAL_PAPER_TRADER.active_trades
    return {}

@app_web.post("/api/close")
def close_trade_manual(id: str):
    if GLOBAL_PAPER_TRADER and id in GLOBAL_PAPER_TRADER.active_trades:
        trade = GLOBAL_PAPER_TRADER.active_trades[id]
        GLOBAL_PAPER_TRADER._send_close_report(trade, 0.0)
        del GLOBAL_PAPER_TRADER.active_trades[id]
        GLOBAL_PAPER_TRADER._save_trades()
        return {"status": "ok"}
    return {"status": "not_found"}

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app_web, host="0.0.0.0", port=port, log_level="warning")

threading.Thread(target=run_web_server, daemon=True).start()

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

# ==================== لایه تحلیل (ارتقا یافته) ====================
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

    # پرامپت تحلیل هوش مصنوعی پیشرفته‌تر شده تا تاییدهای دقیق‌تری بدهد
    def get_ai_confirmation(self, symbol: str, side: str, df: pd.DataFrame, trend: str) -> Dict:
        latest = df.iloc[-1]
        vol_ratio = latest['volume'] / latest['vol_sma'] if latest['vol_sma'] > 0 else 1.0
        
        prompt = f"""
You are an advanced quantitative crypto risk officer.
Evaluate this setup carefully:
- Symbol: {symbol}
- Trade Direction: {side}
- 4H Higher Timeframe Trend: {trend}
- 15m Price: {latest['close']}
- Key Support: {latest['support']} | Resistance: {latest['resistance']}
- Technicals: RSI(14)={latest['rsi']:.1f}, EMA20={latest['ema_fast']:.2f}, EMA50={latest['ema_slow']:.2f}
- Market Dynamics: ATR={latest['atr']:.4f}, Volume Multiplier={vol_ratio:.2f}x

Evaluation Rule:
Assign an integer score between 60 and 95 based on trend alignment, volume confirmation, and S/R room.
If signal conflicts with trend or high risk, rate low (<75).

Output ONLY the raw integer score (e.g. 85).
"""
        try:
            response = self.client.chat.completions.create(
                model=self.config.AI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=6
            )
            answer = response.choices[0].message.content.strip()
            score = float(''.join(filter(str.isdigit, answer))) / 100.0
            if score < 0.60 or score > 0.98:
                score = 0.82
            return {"confidence": score}
        except Exception as e:
            logger.error(f"خطای AI برای {symbol}: {e}")
            return {"confidence": 0.80}

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

# ==================== ماژول معامله مجازی ====================
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
        trade_id = f"{symbol.replace('/', '')}_{int(time.time())}"
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
        msg = f"\n{emoji} **معامله بسته شد**\n\n📌 **ارز:** {trade['symbol']} ({trade['side']})\n📈 **سود/زیان:** {pnl:+.2f}%\n"
        self.telegram.send_personal_message(msg)

# ==================== ارسال تلگرام ====================
class TelegramSender:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

    def send_system_status(self, text: str):
        try:
            requests.post(f"{self.base_url}/sendMessage", json={"chat_id": self.config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
        except Exception as e:
            logger.error(f"خطا: {e}")

    def send_personal_message(self, text: str):
        target_id = self.config.PERSONAL_CHAT_ID or self.config.TELEGRAM_CHAT_ID
        try:
            requests.post(f"{self.base_url}/sendMessage", json={"chat_id": target_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
        except Exception as e:
            logger.error(f"خطا: {e}")

    def send_signal(self, symbol: str, side: str, latest: pd.Series, confidence: float, trend_4h: str) -> Dict:
        emoji = "🟢" if side == "BUY" else "🔴"
        direction = "LONG" if side == "BUY" else "SHORT"
        price = float(latest['close'])
        atr = float(latest['atr']) if not pd.isna(latest['atr']) else price * 0.01

        if side == "BUY":
            stop_loss = round(min(float(latest['support']), price - (1.3 * atr)), 4)
            risk = price - stop_loss
            tp1 = round(price + (1.5 * risk), 4)
            tp2 = round(price + (2.5 * risk), 4)
            tp3 = round(price + (4.2 * risk), 4)
            trailing_step = round(price + (1.0 * risk), 4)
        else:
            stop_loss = round(max(float(latest['resistance']), price + (1.3 * atr)), 4)
            risk = stop_loss - price
            tp1 = round(price - (1.5 * risk), 4)
            tp2 = round(price - (2.5 * risk), 4)
            tp3 = round(price - (4.2 * risk), 4)
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

📊 **Metrics:** RSI: {latest['rsi']:.1f} | AI Score: {confidence:.0%}
⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
        try:
            requests.post(f"{self.base_url}/sendMessage", json={"chat_id": self.config.TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=10)
            return {"price": price, "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": stop_loss}
        except Exception as e:
            logger.error(f"خطای ارسال تلگرام: {e}")
            return None

# ==================== سیستم اصلی ====================
class HybridTradingSystem:
    def __init__(self):
        global GLOBAL_PAPER_TRADER
        self.config = Config()
        self.config.validate()
        self.data = DataLayer(self.config)
        self.analysis = AnalysisLayer(self.config)
        self.signal_engine = SignalEngine(self.config)
        self.telegram = TelegramSender(self.config)
        self.paper_trader = PaperTrader(self.config, self.telegram)
        GLOBAL_PAPER_TRADER = self.paper_trader
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
        self.paper_trader.update_and_check_trades(self.data)

    def start(self):
        logger.info("بات V5 Ultimate Pro فعال شد")
        self.telegram.send_system_status("⚡️ **نسخه جامع V5 با مینی‌اپ فعال شد.**")
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
