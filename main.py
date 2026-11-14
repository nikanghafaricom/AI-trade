# ==============================================
# Online Mode Selection Bot - نسخه زنده (معامله مجازی)
#
# هسته‌ی تحلیل و تولید سیگنال: کاملاً از final_online_bot.py (انتخاب آنلاین حالت‌ها)
#   - ۴ حالت استراتژی (agg / bal / qual / def) به‌صورت موازی محاسبه می‌شن
#   - هر ۱۰ روز بر اساس عملکرد ۶۰ روز اخیر، وزن هر حالت به‌روز می‌شه
#   - حالتی که بالاترین وزن رو داره، پرتفوی هدف همون روز رو تعیین می‌کنه
# بخش‌های حاشیه‌ای (وب‌سرور، تلگرام، معامله‌ی مجازی، ژورنال، هشدار خطا) نگه داشته شدن.
#
# داده: OKX Spot کندل روزانه | تصمیم‌گیری: یک بار در روز، بعد از بسته‌شدن کندل روزانه
# ==============================================
import os
import time
import logging
import gc
import json
import threading
import requests
import numpy as np
import pandas as pd
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, date as date_cls
from typing import Dict, Optional, List, Tuple
from dotenv import load_dotenv

load_dotenv()

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


# ==================== وب‌سرور استاندارد ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Online Mode Selection Bot is alive and running!")

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
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")          # کانال/چت سیگنال
    PERSONAL_CHAT_ID = os.getenv("PERSONAL_CHAT_ID")          # گزارش و هشدار شخصی

    # ---- حساب مجازی ----
    VIRTUAL_CAPITAL_USDT = float(os.getenv("VIRTUAL_CAPITAL_USDT", 10000))
    FEE_SIDE = float(os.getenv("FEE_SIDE", 0.001))            # ۰.۱٪ هر طرف (۰.۲٪ رفت‌وبرگشت)

    # سقف مجموع سهم پوزیشن‌ها از سرمایه. ۱.۰ یعنی فقط اسپات (بدون اهرم).
    # فرمول وزن‌دهی استراتژی گاهی مجموع وزن‌ها رو بیشتر از ۱۰۰٪ می‌کنه (یعنی اهرم)؛
    # با این سقف، همون وزن‌ها به‌نسبت کوچیک می‌شن تا از سرمایه بیشتر نشه.
    # برای بازتولید دقیق بک‌تست اصلی (با اهرم) مقدارش رو مثلاً 99 بذار.
    MAX_GROSS_EXPOSURE = float(os.getenv("MAX_GROSS_EXPOSURE", 1.0))

    # برای جلوگیری از معاملات ریز و بی‌اهمیت در اجرا (فقط روی اجرای مجازی اثر داره)
    REBALANCE_BAND = float(os.getenv("REBALANCE_BAND", 0.05))        # تغییر کمتر از ۵٪ سرمایه اجرا نمی‌شه
    MIN_NEW_POSITION = float(os.getenv("MIN_NEW_POSITION", 0.02))    # پوزیشن جدید کمتر از ۲٪ سرمایه باز نمی‌شه

    CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))           # هر چند ثانیه چک بشه کندل روزانه‌ی جدید اومده یا نه

    STATE_FILE = "bot_state.json"
    JOURNAL_FILE = "trade_history_v2.json"

    # ---- داده ----
    OKX_HOSTS = ["https://www.okx.com", "https://aws.okx.com"]
    OKX_BAR = "1D"
    HISTORY_DAYS = 420            # تعداد روز تاریخچه برای محاسبه‌ی روزانه
    SIM_WARMUP_DAYS = 100         # روزهای اولِ تاریخچه که فقط برای گرم‌شدن اندیکاتورهاست
    MIN_SYMBOLS_FRACTION = 0.5    # اگه کمتر از این نسبت ارزها دریافت بشه، تصمیم‌گیری انجام نمی‌شه

    # ---- پارامترهای انتخاب آنلاین (عیناً از نسخه‌ی جدید) ----
    LOOKBACK = 60             # پنجره ارزیابی عملکرد اخیر (روز)
    SELECT_EVERY = 10         # هر چند روز یکبار انتخاب مجدد
    SMOOTH = 0.30             # هموارسازی وزن‌ها

    SYMBOLS = [
        "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT",
        "TRX", "LTC", "BCH", "NEAR", "ATOM", "UNI", "AAVE", "ETC", "SUI", "HBAR",
        "SHIB", "ICP", "APT", "ARB", "OP", "INJ", "FIL", "XLM", "ALGO",
        "FET", "WLD", "ONDO", "ENA", "PEPE", "RENDER", "SEI",
        "TIA", "JUP", "PENDLE", "LDO", "IMX", "STX", "CRV", "SAND", "GRT", "THETA",
        "ZEC", "HYPE", "PUMP", "RAY", "MINA", "CVX", "OMI", "ETHFI", "KMNO",
        "BOME", "WIF", "BONK", "FLOKI", "NOT", "ORDI",
        "MEW", "NEIRO", "GOAT", "MOODENG", "PNUT", "ACT", "VIRTUAL", "AIXBT"
    ]

    # ---- تعریف حالت‌ها (عیناً از نسخه‌ی جدید) ----
    MODES = [
        dict(name="agg",  K=1, MOM_L=82, MOM_S=14, MIN_SC=-0.20, SOFT=1.025, FORCE=0.25, POWER=1.5,  BO=0.995, SMA=30, REQ=False),
        dict(name="bal",  K=2, MOM_L=70, MOM_S=12, MIN_SC=-0.08, SOFT=1.03,  FORCE=0.20, POWER=1.35, BO=0.996, SMA=40, REQ=False),
        dict(name="qual", K=1, MOM_L=60, MOM_S=10, MIN_SC=0.05,  SOFT=1.04,  FORCE=0.16, POWER=1.3,  BO=0.997, SMA=45, REQ=True),
        dict(name="def",  K=1, MOM_L=50, MOM_S=8,  MIN_SC=0.12,  SOFT=1.05,  FORCE=0.12, POWER=1.2,  BO=0.998, SMA=50, REQ=True),
    ]

    MODE_LABELS = {"agg": "تهاجمی (agg)", "bal": "متعادل (bal)", "qual": "کیفی (qual)", "def": "دفاعی (def)"}

    def validate(self):
        required = {
            "TELEGRAM_BOT_TOKEN": self.TELEGRAM_BOT_TOKEN,
            "TELEGRAM_CHAT_ID": self.TELEGRAM_CHAT_ID,
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ValueError(f"این متغیرهای محیطی تنظیم نشدن: {', '.join(missing)}")


def fmt_price(p: float) -> str:
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:,.4f}"
    return f"{p:.8f}".rstrip("0").rstrip(".")


# ==================== لایه داده (OKX روزانه) ====================
class OKXData:
    def __init__(self, config: Config):
        self.config = config

    def _get(self, path: str, params: dict) -> list:
        last_error = None
        for host in self.config.OKX_HOSTS:
            try:
                r = requests.get(f"{host}{path}", params=params, timeout=15)
                if r.status_code == 200:
                    j = r.json()
                    if j.get("code") == "0":
                        return j.get("data") or []
                    last_error = f"{host} -> code {j.get('code')}: {j.get('msg')}"
                else:
                    last_error = f"{host} -> HTTP {r.status_code}"
            except Exception as e:
                last_error = f"{host} -> {e}"
        raise RuntimeError(last_error)

    def latest_confirmed_ts(self) -> Optional[int]:
        """زمان شروع آخرین کندل روزانه‌ی بسته‌شده‌ی BTC (میلی‌ثانیه). چک سبک و سریع."""
        try:
            data = self._get("/api/v5/market/candles",
                             {"instId": "BTC-USDT", "bar": self.config.OKX_BAR, "limit": "3"})
            confirmed = [int(r[0]) for r in data if len(r) > 8 and r[8] == "1"]
            return max(confirmed) if confirmed else None
        except Exception as e:
            logger.error(f"خطا در چک کندل روزانه‌ی جدید: {e}")
            return None

    def fetch_daily(self, symbol: str, days: int) -> Optional[pd.DataFrame]:
        inst = f"{symbol}-USDT"
        start_ms = int((time.time() - days * 86400) * 1000)
        rows, after = [], None
        try:
            for _ in range(12):
                params = {"instId": inst, "bar": self.config.OKX_BAR, "limit": "100"}
                if after:
                    params["after"] = str(after)
                data = self._get("/api/v5/market/history-candles", params)
                if not data:
                    break
                rows.extend(data)
                oldest = int(data[-1][0])
                if oldest <= start_ms:
                    break
                after = oldest
                time.sleep(0.12)
            # تازه‌ترین کندل‌ها (آخر اضافه می‌شن تا در حذف تکراری‌ها اولویت داشته باشن)
            try:
                rows.extend(self._get("/api/v5/market/candles",
                                      {"instId": inst, "bar": self.config.OKX_BAR, "limit": "5"}))
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"{symbol}: دریافت داده ناموفق ({e})")
            return None

        if not rows:
            return None
        parsed = []
        for r in rows:
            ts = int(r[0])
            if ts < start_ms:
                continue
            confirm = r[8] if len(r) > 8 else "1"
            parsed.append({"ts": ts, "open_time": pd.to_datetime(ts, unit="ms"),
                           "close": float(r[4]), "confirm": confirm})
        if not parsed:
            return None
        df = pd.DataFrame(parsed).drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)
        df = df[df["confirm"] == "1"]          # فقط کندل‌های کاملاً بسته‌شده
        return df.reset_index(drop=True)

    def load_closes(self) -> pd.DataFrame:
        cols, skipped = {}, []
        for sym in self.config.SYMBOLS:
            d = self.fetch_daily(sym, self.config.HISTORY_DAYS)
            if d is None or len(d) < 80:
                skipped.append(sym)
            else:
                cols[sym + "/USDT"] = pd.Series(d["close"].to_numpy(), index=pd.DatetimeIndex(d["open_time"]))
            time.sleep(0.05)
        if skipped:
            logger.info(f"ارزهای ردشده (داده‌ی ناکافی یا نبودن در OKX): {', '.join(skipped)}")
        if "BTC/USDT" not in cols:
            raise RuntimeError("داده‌ی BTC دریافت نشد.")
        if len(cols) < self.config.MIN_SYMBOLS_FRACTION * len(self.config.SYMBOLS):
            raise RuntimeError(f"فقط {len(cols)} ارز از {len(self.config.SYMBOLS)} دریافت شد - تصمیم‌گیری انجام نمی‌شه.")
        return pd.DataFrame(cols).sort_index().ffill(limit=5).ffill().bfill(limit=5)


# ==================== هسته‌ی تحلیل و تولید سیگنال (انتخاب آنلاین حالت‌ها) ====================
class StrategyEngine:
    def __init__(self, config: Config):
        self.config = config

    def simulate_mode(self, C: np.ndarray, bi: int, i0: int, mode: dict) -> Tuple[np.ndarray, np.ndarray]:
        """
        اجرای کامل یک حالت. خروجی: منحنی equity و وزن‌های هدف در آخرین کندل بسته‌شده.
        منطق همون run_single_mode نسخه‌ی جدیده؛ فقط یک قدم اضافه داره: تصمیم آخرین کندل
        (که در بک‌تست چون «فردایی» نداره اجرا نمی‌شد) اینجا محاسبه و برگردونده می‌شه.
        """
        fee = self.config.FEE_SIDE
        max_gross = self.config.MAX_GROSS_EXPOSURE
        T, nS = C.shape
        w = np.zeros(nS)
        val = 1.0
        peak = np.zeros(nS)
        eq = np.empty(T - i0)
        eq[0] = 1.0

        Sma = pd.DataFrame(C).rolling(35, min_periods=12).mean().to_numpy()
        Sf = pd.Series(C[:, bi]).rolling(mode["SMA"], min_periods=8).mean().to_numpy()
        momL = pd.DataFrame(C).pct_change(mode["MOM_L"], fill_method=None).to_numpy()
        momS = pd.DataFrame(C).pct_change(mode["MOM_S"], fill_method=None).to_numpy()
        rets = pd.DataFrame(C).pct_change(fill_method=None).to_numpy()
        vol = pd.DataFrame(rets).rolling(12, min_periods=4).std().to_numpy()
        highs = pd.DataFrame(C).rolling(16, min_periods=4).max().to_numpy()

        final_w = np.zeros(nS)
        for t in range(i0, T):
            btc_ok = np.isfinite(Sf[t]) and C[t, bi] > Sf[t] * mode["SOFT"]
            new = w.copy()

            # Trailing stop
            if mode["FORCE"] > 0:
                for j in range(nS):
                    if w[j] > 0.01:
                        if C[t, j] > peak[j]:
                            peak[j] = C[t, j]
                        if peak[j] > 0 and (C[t, j] / peak[j] - 1) < -mode["FORCE"]:
                            new[j] = 0.0
                            peak[j] = 0.0

            if not btc_ok:
                new[:] = 0.0
                peak[:] = 0.0
            else:
                with np.errstate(all="ignore"):
                    raw = 0.9 * momL[t] + 0.1 * momS[t]
                    score = np.where(vol[t] > 1e-8, raw / (vol[t] * np.sqrt(mode["MOM_L"]) + 1e-8), raw)
                    elig = (
                        (C[t] > Sma[t] * 0.92) &
                        (score > mode["MIN_SC"]) &
                        np.isfinite(score) &
                        (C[t] >= highs[t] * mode["BO"]) &
                        np.isfinite(C[t])
                    )
                    if mode["REQ"]:
                        elig = elig & (momS[t] > 0)

                    idx = np.flatnonzero(elig)
                    new = np.zeros(nS)
                    if len(idx) >= 1:
                        order = idx[np.argsort(-score[idx])]
                        top = order[:mode["K"]]
                        sc = np.maximum(score[top], 0.0)
                        if sc.sum() > 1e-12:
                            ww = (sc ** mode["POWER"]) / sc.sum()
                        else:
                            ww = np.ones(len(top)) / len(top)
                        new[top] = ww
                        for j in top:
                            if w[j] < 0.01 or peak[j] == 0:
                                peak[j] = C[t, j]

            # سقف اسپات: مجموع وزن‌ها از سقف تنظیم‌شده بیشتر نشه
            gross = new.sum()
            if gross > max_gross:
                new = new * (max_gross / gross)

            to = np.abs(new - w).sum()
            val *= (1 - to * fee)
            w = new

            if t == T - 1:
                final_w = w.copy()
                break

            with np.errstate(all="ignore"):
                r = np.where(np.isnan(C[t + 1] / C[t]), 0., C[t + 1] / C[t] - 1)
            port = float((w * r).sum())
            val *= (1 + port)
            if abs(1 + port) > 1e-12 and w.sum() > 0.01:
                w = w * (1 + r) / (1 + port)
            eq[t + 1 - i0] = val

        return eq, final_w

    def decide(self, closes: pd.DataFrame) -> Optional[Dict]:
        cfg = self.config
        names = list(closes.columns)
        dates = closes.index
        C = closes.to_numpy(float)
        T, nS = C.shape
        bi = names.index("BTC/USDT")
        i0 = cfg.SIM_WARMUP_DAYS
        if T - i0 < cfg.LOOKBACK + cfg.SELECT_EVERY + 2:
            logger.error(f"تاریخچه‌ی کافی نیست (T={T}).")
            return None

        # ---------- اجرای همه حالت‌ها ----------
        mode_eqs, mode_targets = {}, {}
        for m in cfg.MODES:
            eq, target = self.simulate_mode(C, bi, i0, m)
            mode_eqs[m["name"]] = eq
            mode_targets[m["name"]] = target

        # ---------- انتخاب آنلاین ----------
        n = len(next(iter(mode_eqs.values())))
        weights = {m["name"]: 1.0 / len(cfg.MODES) for m in cfg.MODES}
        for step in range(n):
            if step >= cfg.LOOKBACK and step % cfg.SELECT_EVERY == 0:
                scores = {}
                for name, eq in mode_eqs.items():
                    scores[name] = eq[step] / max(eq[step - cfg.LOOKBACK], 1e-8) - 1
                arr = np.array(list(scores.values()))
                arr = arr - arr.min() + 0.01
                arr = np.power(arr, 1.5)
                probs = arr / arr.sum()
                for i, name in enumerate(scores.keys()):
                    weights[name] = (1 - cfg.SMOOTH) * weights[name] + cfg.SMOOTH * probs[i]
        best = max(weights, key=weights.get)

        best_mode = next(m for m in cfg.MODES if m["name"] == best)
        Sf = pd.Series(C[:, bi]).rolling(best_mode["SMA"], min_periods=8).mean().to_numpy()
        btc_ok = bool(np.isfinite(Sf[-1]) and C[-1, bi] > Sf[-1] * best_mode["SOFT"])

        final_w = mode_targets[best]
        targets = {names[j]: float(final_w[j]) for j in range(nS) if final_w[j] > 0.005}
        prices = {names[j]: float(C[-1, j]) for j in range(nS) if np.isfinite(C[-1, j]) and C[-1, j] > 0}
        mode_recent = {name: float(eq[-1] / max(eq[-1 - cfg.LOOKBACK], 1e-8) - 1) for name, eq in mode_eqs.items()}

        last_open = dates[-1]
        return {
            "ts": int((last_open - pd.Timestamp("1970-01-01")) // pd.Timedelta(milliseconds=1)),
            "date": (last_open + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            "best_mode": best,
            "mode_weights": {k: float(v) for k, v in weights.items()},
            "mode_recent": mode_recent,
            "btc_ok": btc_ok,
            "targets": targets,
            "prices": prices,
        }


# ==================== ژورنال معاملات ====================
class TradeJournal:
    def __init__(self, path: str):
        self.path = path
        self.records: List[Dict] = self._load()

    def _load(self) -> List[Dict]:
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
                json.dump(self.records, f, indent=2)
        except Exception as e:
            logger.error(f"خطا در ذخیره ژورنال معاملات: {e}")

    def record(self, symbol: str, reason: str, pnl_usdt: float, pnl_pct: float, closed_pct: float):
        self.records.append({
            "date": date_cls.today().isoformat(),
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "reason": reason,
            "pnl_usdt": round(pnl_usdt, 2),
            "pnl_pct": round(pnl_pct, 3),
            "closed_pct": round(closed_pct, 1)
        })
        self._save()

    def stats(self) -> Dict:
        if not self.records:
            return {"count": 0, "wins": 0, "win_rate": 0.0, "total_pnl": 0.0}
        wins = [r for r in self.records if r["pnl_usdt"] > 0]
        return {
            "count": len(self.records),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(self.records) * 100, 1),
            "total_pnl": round(sum(r["pnl_usdt"] for r in self.records), 2),
        }


# ==================== ماژول معامله مجازی (پرتفوی) ====================
class PaperPortfolio:
    def __init__(self, config: Config, journal: TradeJournal):
        self.config = config
        self.journal = journal
        self.file_path = config.STATE_FILE
        self.state = self._load()

    def _default_state(self) -> Dict:
        return {
            "cash": self.config.VIRTUAL_CAPITAL_USDT,
            "positions": {},
            "last_candle_ts": 0,
            "start_equity": self.config.VIRTUAL_CAPITAL_USDT,
            "start_date": date_cls.today().isoformat(),
            "last_prices": {},
            "equity_history": [],
            "current_mode": None,
        }

    def _load(self) -> Dict:
        state = self._default_state()
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    state.update(json.load(f))
            except Exception as e:
                logger.error(f"خطا در خواندن وضعیت ذخیره‌شده، با وضعیت جدید شروع می‌شه: {e}")
        return state

    def _save(self):
        try:
            tmp = self.file_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            os.replace(tmp, self.file_path)
        except Exception as e:
            logger.error(f"خطا در ذخیره وضعیت پرتفوی مجازی: {e}")

    def _equity(self, price_of) -> float:
        total = self.state["cash"]
        for sym, pos in self.state["positions"].items():
            p = price_of(sym) or pos["avg_cost"]
            total += pos["qty"] * p
        return total

    def rebalance(self, decision: Dict) -> Tuple[List[Dict], Dict]:
        cfg = self.config
        fee = cfg.FEE_SIDE
        st = self.state
        positions = st["positions"]
        last_prices = st["last_prices"]
        for sym, p in decision["prices"].items():
            last_prices[sym] = p

        def price_of(sym):
            p = last_prices.get(sym)
            return p if p and p > 0 else None

        equity = self._equity(price_of)
        targets = decision["targets"]
        trades: List[Dict] = []

        # ---------- فروش‌ها ----------
        for sym in list(positions.keys()):
            pos = positions[sym]
            p = price_of(sym)
            if p is None:
                continue
            tw = targets.get(sym, 0.0)
            cur_val = pos["qty"] * p
            if tw <= 0:
                sell_qty = pos["qty"]
            else:
                excess = cur_val - tw * equity
                if excess <= cfg.REBALANCE_BAND * equity:
                    continue
                sell_qty = excess / p
            sell_qty = min(sell_qty, pos["qty"])
            proceeds = sell_qty * p * (1 - fee)
            pnl_usdt = proceeds - sell_qty * pos["avg_cost"]
            pnl_pct = (p * (1 - fee) / pos["avg_cost"] - 1) * 100
            closed_pct = sell_qty / pos["qty"] * 100
            pos["qty"] -= sell_qty
            st["cash"] += proceeds
            full = tw <= 0 or pos["qty"] <= 1e-12
            if full:
                del positions[sym]
            reason = "خروج کامل طبق استراتژی" if full else "کاهش سهم طبق استراتژی"
            self.journal.record(sym, reason, pnl_usdt, pnl_pct, closed_pct)
            trades.append({"symbol": sym, "side": "SELL", "price": p, "full": full,
                           "pnl_usdt": pnl_usdt, "pnl_pct": pnl_pct})

        # ---------- خریدها ----------
        wants = []
        for sym, tw in targets.items():
            p = price_of(sym)
            if p is None:
                continue
            pos = positions.get(sym)
            cur_val = pos["qty"] * p if pos else 0.0
            diff = tw * equity - cur_val
            threshold = cfg.REBALANCE_BAND if pos else cfg.MIN_NEW_POSITION
            if diff > threshold * equity:
                wants.append((sym, p, diff))
        total_want = sum(d for _, _, d in wants)
        scale = min(1.0, max(0.0, st["cash"]) / total_want) if total_want > 0 else 0.0
        for sym, p, diff in wants:
            spend = diff * scale
            if spend <= 0:
                continue
            qty = spend / (p * (1 + fee))
            pos = positions.get(sym)
            is_new = pos is None
            if pos:
                new_qty = pos["qty"] + qty
                pos["avg_cost"] = (pos["qty"] * pos["avg_cost"] + spend) / new_qty
                pos["qty"] = new_qty
            else:
                positions[sym] = {"qty": qty, "avg_cost": spend / qty,
                                  "open_time": datetime.now().strftime('%Y-%m-%d %H:%M')}
            st["cash"] = max(0.0, st["cash"] - spend)
            trades.append({"symbol": sym, "side": "BUY", "price": p, "new": is_new})

        # ---------- وضعیت بعد از معاملات ----------
        equity_after = self._equity(price_of)
        for t in trades:
            pos = positions.get(t["symbol"])
            t["weight_after"] = (pos["qty"] * t["price"] / equity_after) if (pos and equity_after > 0) else 0.0

        history = st["equity_history"]
        if history and history[-1][0] == decision["date"]:
            history[-1][1] = round(equity_after, 2)
        else:
            history.append([decision["date"], round(equity_after, 2)])
        st["equity_history"] = history[-800:]
        st["last_candle_ts"] = decision["ts"]
        st["current_mode"] = decision["best_mode"]
        self._save()

        pos_list = []
        for sym, pos in positions.items():
            p = price_of(sym) or pos["avg_cost"]
            pos_list.append({
                "symbol": sym,
                "weight": pos["qty"] * p / equity_after if equity_after > 0 else 0.0,
                "pnl_pct": (p / pos["avg_cost"] - 1) * 100,
            })
        pos_list.sort(key=lambda x: -x["weight"])
        snapshot = {
            "equity": equity_after,
            "cash": st["cash"],
            "cash_weight": st["cash"] / equity_after if equity_after > 0 else 1.0,
            "positions": pos_list,
            "return_pct": (equity_after / st["start_equity"] - 1) * 100,
        }
        return trades, snapshot


# ==================== ارسال تلگرام ====================
class TelegramSender:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

    def test_connection(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/getMe", timeout=10)
            if resp.status_code == 200 and resp.json().get("ok"):
                bot_name = resp.json()["result"].get("username", "?")
                logger.info(f"✅ اتصال تلگرام تایید شد. ربات: @{bot_name}")
                return True
            logger.error(f"❌ توکن تلگرام معتبر نیست یا پاسخ غیرمنتظره: {resp.text}")
            return False
        except Exception as e:
            logger.error(f"❌ خطا در تست اتصال تلگرام: {e}")
            return False

    def _post(self, chat_id: str, text: str) -> bool:
        url = f"{self.base_url}/sendMessage"
        try:
            r = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
            if r.status_code == 200:
                return True
            if r.status_code == 400:
                # اگه فرمت Markdown مشکل داشت، بدون فرمت دوباره می‌فرستیم تا پیام گم نشه
                r2 = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
                if r2.status_code == 200:
                    return True
                logger.error(f"ارسال پیام ناموفق بود: {r2.status_code} {r2.text}")
                return False
            logger.error(f"ارسال پیام ناموفق بود: {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"خطای ارسال پیام به تلگرام: {e}")
        return False

    def send_system_status(self, text: str):
        self._post(self.config.TELEGRAM_CHAT_ID, text)

    def send_personal_message(self, text: str):
        self._post(self.config.PERSONAL_CHAT_ID or self.config.TELEGRAM_CHAT_ID, text)

    def send_error_alert(self, text: str):
        self.send_personal_message(f"🚨 **هشدار سیستم** 🚨\n\n{text}")

    @staticmethod
    def _portfolio_line(snapshot: Dict) -> str:
        parts = [f"{p['symbol'].replace('/USDT', '')} {p['weight'] * 100:.0f}%" for p in snapshot["positions"]]
        parts.append(f"نقد {snapshot['cash_weight'] * 100:.0f}%")
        return " | ".join(parts)

    def send_signal_update(self, decision: Dict, trades: List[Dict], snapshot: Dict):
        """سیگنال روزانه - فقط وقتی تغییری در پرتفوی هست."""
        label = self.config.MODE_LABELS.get(decision["best_mode"], decision["best_mode"])
        market = "✅ صعودی - اجازه‌ی خرید" if decision["btc_ok"] else "⛔ ضعیف - ترجیح با نقد"
        lines = [f"📡 **سیگنال روزانه - {decision['date']}**", "",
                 f"🧠 حالت فعال: {label}",
                 f"📊 وضعیت بازار (بیت‌کوین): {market}", ""]

        buys = [t for t in trades if t["side"] == "BUY"]
        sells = [t for t in trades if t["side"] == "SELL"]
        if buys:
            lines.append("🟢 **خرید / افزایش سهم:**")
            for t in buys:
                kind = "ورود جدید" if t.get("new") else "افزایش"
                lines.append(f"• {t['symbol']} | قیمت {fmt_price(t['price'])} | {kind} | سهم هدف {t['weight_after'] * 100:.0f}%")
            lines.append("")
        if sells:
            lines.append("🔴 **فروش / کاهش سهم:**")
            for t in sells:
                kind = "خروج کامل" if t["full"] else f"کاهش به {t['weight_after'] * 100:.0f}%"
                lines.append(f"• {t['symbol']} | قیمت {fmt_price(t['price'])} | {kind}")
            lines.append("")
        lines.append(f"💼 **پرتفوی بعد از تغییر:** {self._portfolio_line(snapshot)}")
        self.send_system_status("\n".join(lines))

    def send_daily_report(self, decision: Dict, trades: List[Dict], snapshot: Dict, journal_stats: Dict):
        """گزارش شخصی روزانه - همیشه ارسال می‌شه."""
        label = self.config.MODE_LABELS.get(decision["best_mode"], decision["best_mode"])
        weights_txt = " | ".join(f"{k} {v * 100:.0f}%" for k, v in decision["mode_weights"].items())
        recent_txt = " | ".join(f"{k} {v * 100:+.0f}%" for k, v in decision["mode_recent"].items())
        lines = [f"📊 **گزارش روزانه‌ی پرتفوی مجازی - {decision['date']}**", "",
                 f"💰 ارزش کل: {snapshot['equity']:,.2f} USDT ({snapshot['return_pct']:+.2f}% از شروع)",
                 f"💵 نقد: {snapshot['cash']:,.2f} USDT",
                 f"🧠 حالت فعال: {label}",
                 f"⚖️ وزن حالت‌ها: {weights_txt}",
                 f"📈 بازده {self.config.LOOKBACK} روزه‌ی حالت‌ها: {recent_txt}", ""]
        if snapshot["positions"]:
            lines.append("📦 **پوزیشن‌های باز:**")
            for p in snapshot["positions"]:
                lines.append(f"• {p['symbol']} | سهم {p['weight'] * 100:.0f}% | سود/زیان {p['pnl_pct']:+.2f}%")
        else:
            lines.append("📦 هیچ پوزیشنی باز نیست (۱۰۰٪ نقد)")
        closed = [t for t in trades if t["side"] == "SELL"]
        if closed:
            lines += ["", "✅ **فروش‌های امروز:**"]
            for t in closed:
                emoji = "✅" if t["pnl_usdt"] > 0 else "❌"
                lines.append(f"{emoji} {t['symbol']}: {t['pnl_pct']:+.2f}% ({t['pnl_usdt']:+.2f} USDT)")
        if journal_stats["count"]:
            lines += ["", f"🧾 کل فروش‌های ثبت‌شده: {journal_stats['count']} | نرخ برد {journal_stats['win_rate']}% | "
                          f"مجموع {journal_stats['total_pnl']:+.2f} USDT"]
        self.send_personal_message("\n".join(lines))


# ==================== سیستم اصلی ====================
class OnlineModeBot:
    def __init__(self):
        self.config = Config()
        self.config.validate()
        self.data = OKXData(self.config)
        self.engine = StrategyEngine(self.config)
        self.telegram = TelegramSender(self.config)
        self.journal = TradeJournal(self.config.JOURNAL_FILE)
        self.portfolio = PaperPortfolio(self.config, self.journal)
        self.running = True
        self.consecutive_failures = 0
        self.failure_alert_sent = False

    def _send_crash_alert(self, text: str):
        try:
            self.telegram.send_error_alert(text)
        except Exception:
            logger.error("حتی ارسال هشدار خطا هم ناموفق بود.")

    def _register_failure(self, message: str):
        self.consecutive_failures += 1
        logger.error(message)
        if self.consecutive_failures >= 3 and not self.failure_alert_sent:
            self._send_crash_alert(f"{message}\n\nچند بار پشت‌سرهم تکرار شده. ربات به تلاش ادامه می‌ده.")
            self.failure_alert_sent = True

    def _register_success(self):
        self.consecutive_failures = 0
        self.failure_alert_sent = False

    def run_once(self):
        last_ts = int(self.portfolio.state.get("last_candle_ts", 0))
        ts = self.data.latest_confirmed_ts()
        if ts is None:
            self._register_failure("دریافت کندل روزانه از OKX ناموفق بود - احتمالاً مشکل اتصال یا صرافی.")
            return
        if ts <= last_ts:
            self._register_success()
            logger.info("کندل روزانه‌ی جدیدی نیومده - منتظر بسته‌شدن کندل بعدی.")
            return

        logger.info("----- کندل روزانه‌ی جدید بسته شد: شروع تحلیل کامل -----")
        try:
            closes = self.data.load_closes()
            decision = self.engine.decide(closes)
            if decision is None:
                raise RuntimeError("تحلیل نتیجه‌ای نداد (تاریخچه‌ی ناکافی).")
        except Exception as e:
            self._register_failure(f"خطا در دریافت داده یا تحلیل: {e}")
            return

        if decision["ts"] <= last_ts:
            logger.info("داده‌ی کندل جدید هنوز کامل به دست نیومده - چرخه‌ی بعد دوباره تلاش می‌شه.")
            return

        trades, snapshot = self.portfolio.rebalance(decision)
        self._register_success()

        logger.info(f"حالت فعال: {decision['best_mode']} | هدف: {decision['targets']} | معاملات: {len(trades)}")
        if trades:
            self.telegram.send_signal_update(decision, trades, snapshot)
        self.telegram.send_daily_report(decision, trades, snapshot, self.journal.stats())

    def start(self):
        logger.info("بات انتخاب آنلاین حالت‌ها فعال شد")

        if not self.telegram.test_connection():
            logger.error("اتصال تلگرام برقرار نشد! توکن یا chat_id رو چک کن.")

        cfg = self.config
        start_message = f"""🛡 **ربات انتخاب آنلاین حالت‌ها فعال شد.**

• ۴ حالت استراتژی (تهاجمی/متعادل/کیفی/دفاعی) به‌صورت موازی محاسبه می‌شن
• هر {cfg.SELECT_EVERY} روز، بر اساس عملکرد {cfg.LOOKBACK} روز اخیر، وزن حالت‌ها به‌روز می‌شه
• حالت برتر، پرتفوی هدف رو مشخص می‌کنه (تعداد ارزهای رصدشده: {len(cfg.SYMBOLS)})
• تصمیم‌گیری روزی یک بار، بعد از بسته‌شدن کندل روزانه (چک هر {cfg.CHECK_INTERVAL} ثانیه)
• فقط اسپات، سقف مجموع پوزیشن‌ها {cfg.MAX_GROSS_EXPOSURE * 100:.0f}% سرمایه
• کارمزد در حساب مجازی: {cfg.FEE_SIDE * 100:.2f}% هر طرف | سرمایه‌ی مجازی: {cfg.VIRTUAL_CAPITAL_USDT:,.0f} USDT
"""
        self.telegram.send_system_status(start_message)

        while self.running:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"خطای پیش‌بینی‌نشده در چرخه‌ی اصلی: {e}")
                self._send_crash_alert(
                    f"خطای پیش‌بینی‌نشده در چرخه‌ی اصلی ربات:\n`{e}`\n\n"
                    "ربات همچنان روشنه و چرخه‌ی بعدی رو امتحان می‌کنه."
                )
            gc.collect()
            time.sleep(self.config.CHECK_INTERVAL)

    def stop(self):
        self.running = False
        logger.info("بات متوقف شد")


if __name__ == "__main__":
    bot = OnlineModeBot()
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.stop()
