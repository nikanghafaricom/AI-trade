"""
engine.py - هسته‌ی مشترک ربات (هم برای ربات زنده، هم برای بک‌تست)
=====================================================================
اصل طراحی: «یک کد، دو دنیا». دقیقاً همین منطق سیگنال/ریسک/خروج هم در بک‌تست
اجرا می‌شه هم در ربات زنده؛ پس نتیجه‌ی بک‌تست واقعاً نماینده‌ی رفتار ربات زنده‌ست.

لایه‌ها:
  1) اندیکاتورها + هم‌ترازی چندتایم‌فریمی بدون look-ahead
  2) سه استراتژی مکمل (Pullback روند / Breakout حجمی / Reversion در رنج) - Long و Short
  3) Trade: مدیریت خروج پله‌ای + BE + تریلینگ + تایم‌استاپ + مدل‌سازی کارمزد/اسلیپیج
  4) PerformanceTracker: خودتنظیمی بر اساس عملکرد واقعی هر استراتژی/نماد (بدون AI)
  5) PortfolioState: حالت NORMAL/CAUTION/DEFENSIVE
  6) TradingCore: ریسک پرتفوی (heat، همبستگی، سقف روزانه، دراودان، پروبیشن)
"""
import os
import math
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _nan(x) -> bool:
    return x is None or x != x


# ============================ تنظیمات ============================
ALIASES = {
    "telegram_token": "TELEGRAM_BOT_TOKEN",
    "capital": "VIRTUAL_CAPITAL_USDT",
    "risk_pct": "RISK_PER_TRADE_PCT",
    "max_concurrent": "MAX_CONCURRENT_TRADES",
}


@dataclass
class Config:
    symbols: List[str] = field(default_factory=lambda: [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "AVAX/USDT",
        "NEAR/USDT", "ADA/USDT", "DOGE/USDT", "LINK/USDT", "PAXG/USDT", "LTC/USDT"])
    strategies: List[str] = field(default_factory=lambda: ["pullback", "breakout", "reversion"])
    allow_short: bool = True

    # هزینه‌ها (هر طرف)
    fee_pct: float = 0.05
    slippage_pct: float = 0.02

    # سرمایه و ریسک
    capital: float = 10000.0
    risk_pct: float = 1.0
    max_concurrent: int = 6
    max_same_direction: int = 3
    max_per_group: int = 2
    max_heat_pct: float = 5.0
    max_notional_pct: float = 40.0
    daily_loss_limit_pct: float = 3.0
    dd_half_pct: float = 5.0
    dd_quarter_pct: float = 10.0
    dd_pause_pct: float = 15.0
    pause_hours: float = 12.0
    cooldown_min: int = 60

    # سیگنال
    min_score: float = 6.5
    atr_pctl_max: float = 97.0
    min_risk_cost_mult: float = 3.0
    be_buffer_r: float = 0.05

    # AI مشورتی (فقط وتو یا کاهش حجم؛ هرگز افزایش)
    ai_enabled: bool = True
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    ai_veto_conf: int = 70
    ai_daily_call_cap: int = 300

    # تلگرام / اجرا
    telegram_token: str = ""
    telegram_chat_id: str = ""
    personal_chat_id: str = ""
    monitor_seconds: int = 20
    scan_seconds: int = 20
    state_path: str = "bot_state.json"

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()
        for f in fields(cls):
            env_name = ALIASES.get(f.name, f.name.upper())
            raw = os.getenv(env_name)
            if raw is None or raw == "":
                continue
            try:
                if getattr(f.type, "__origin__", None) is list:
                    val = [x.strip() for x in raw.split(",") if x.strip()]
                elif f.type is bool:
                    val = raw.strip().lower() in ("1", "true", "yes", "on")
                else:
                    val = f.type(raw)
                setattr(cfg, f.name, val)
            except Exception:
                pass
        return cfg


GROUPS = {
    "BTC/USDT": "majors", "ETH/USDT": "majors", "BNB/USDT": "majors", "LTC/USDT": "majors",
    "SOL/USDT": "L1", "AVAX/USDT": "L1", "NEAR/USDT": "L1", "ADA/USDT": "L1",
    "XRP/USDT": "payments", "DOGE/USDT": "meme", "LINK/USDT": "oracle", "PAXG/USDT": "gold",
}

TF_MIN = {"15m": 15, "1h": 60, "4h": 240}


# ============================ اندیکاتورها ============================
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rma(s, n):
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def _rsi(close, n=14):
    d = close.diff()
    up, dn = rma(d.clip(lower=0), n), rma((-d).clip(lower=0), n)
    out = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    out = out.where(dn != 0, 100.0)
    return out.fillna(50.0)


def _atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return rma(tr, n)


def _adx(df, n=14):
    up, dn = df["high"].diff(), -df["low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = _atr(df, n).replace(0, np.nan)
    pdi, mdi = 100 * rma(plus, n) / a, 100 * rma(minus, n) / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return rma(dx.fillna(0), n)


def add_indicators(df: pd.DataFrame, full: bool = True) -> pd.DataFrame:
    d = df.copy().reset_index(drop=True)
    c, h, l, o, v = d["close"], d["high"], d["low"], d["open"], d["volume"]
    d["ema20"], d["ema50"], d["ema200"] = ema(c, 20), ema(c, 50), ema(c, 200)
    d["rsi"] = _rsi(c)
    d["atr"] = _atr(d)
    d["adx"] = _adx(d)
    if not full:
        return d
    d["atr_pct"] = d["atr"] / c * 100
    macd = ema(c, 12) - ema(c, 26)
    d["macd_hist"] = macd - ema(macd, 9)
    d["vol_ratio"] = v / v.rolling(20).mean().replace(0, np.nan)
    mid, sd = c.rolling(20).mean(), c.rolling(20).std()
    d["bb_up"], d["bb_lo"] = mid + 2 * sd, mid - 2 * sd
    d["don_hi"], d["don_lo"] = h.rolling(20).max().shift(1), l.rolling(20).min().shift(1)
    rng = (h - l).replace(0, np.nan)
    d["body_ratio"] = ((c - o).abs() / rng).fillna(0)
    d["low8"], d["high8"] = l.rolling(8).min(), h.rolling(8).max()
    d["pb_long"] = ((l - d["ema50"]) / d["atr"]).rolling(8).min() <= 0.25
    d["pb_short"] = ((h - d["ema50"]) / d["atr"]).rolling(8).max() >= -0.25
    d["cross_up"] = (c > d["ema20"]) & (c.shift(1) <= d["ema20"].shift(1))
    d["cross_dn"] = (c < d["ema20"]) & (c.shift(1) >= d["ema20"].shift(1))
    d["lower_wick"] = ((np.minimum(o, c) - l) / rng).fillna(0)
    d["upper_wick"] = ((h - np.maximum(o, c)) / rng).fillna(0)
    d["atr_pctl"] = d["atr_pct"].rolling(200, min_periods=60).apply(
        lambda x: float((x[:-1] < x[-1]).mean() * 100), raw=True)
    return d


def _attach_htf(ltf: pd.DataFrame, htf: pd.DataFrame, htf_min: int, prefix: str, ltf_min: int) -> pd.DataFrame:
    """هر کندل ۱۵دقیقه فقط آخرین کندل «بسته‌شده»ی HTF رو می‌بینه (بدون look-ahead)."""
    h = htf.copy()
    h["avail"] = h["timestamp"] + pd.Timedelta(minutes=htf_min)
    h = h.drop(columns="timestamp").rename(columns={c: f"{prefix}_{c}" for c in h.columns if c not in ("timestamp", "avail")})
    l = ltf.copy()
    l["close_time"] = l["timestamp"] + pd.Timedelta(minutes=ltf_min)
    l["close_time"] = l["close_time"].astype("datetime64[ns]")
    h["avail"] = h["avail"].astype("datetime64[ns]")
    out = pd.merge_asof(l.sort_values("close_time"), h.sort_values("avail"),
                        left_on="close_time", right_on="avail", direction="backward")
    return out.drop(columns=["avail"]).reset_index(drop=True)


def prepare(df15: pd.DataFrame, df1h: pd.DataFrame, df4h: pd.DataFrame) -> pd.DataFrame:
    a = add_indicators(df15, full=True)
    a["timestamp"] = a["timestamp"].astype("datetime64[ns]")
    h1 = add_indicators(df1h, full=False)[["timestamp", "ema20", "ema50", "rsi", "adx", "close"]]
    h4 = add_indicators(df4h, full=False)[["timestamp", "ema50", "ema200", "adx", "close"]]
    h1["timestamp"] = h1["timestamp"].astype("datetime64[ns]")
    h4["timestamp"] = h4["timestamp"].astype("datetime64[ns]")
    a = _attach_htf(a, h1, 60, "h1", 15)
    a = _attach_htf(a, h4, 240, "h4", 15)
    return a


def resample_ohlcv(df15: pd.DataFrame, rule: str) -> pd.DataFrame:
    x = df15.set_index("timestamp").resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    return x.reset_index()


def dir4(r) -> int:
    c, e50, e200 = r["h4_close"], r["h4_ema50"], r["h4_ema200"]
    if c > e200 and e50 > e200:
        return 1
    if c < e200 and e50 < e200:
        return -1
    return 0


# ============================ استراتژی‌ها ============================
@dataclass
class Signal:
    symbol: str
    side: str
    strategy: str
    score: float
    entry: float
    sl: float
    atr: float
    ts: str
    meta: dict = field(default_factory=dict)


EXITS = {
    #            tp1_r tp1_frac tp2_r tp2_frac tp3_r trail  max_bars
    "pullback":  (1.0, 0.40, 2.0, 0.30, 5.0, 2.0, 96),
    "breakout":  (1.0, 0.40, 2.0, 0.30, 5.0, 2.2, 64),
    "reversion": (0.9, 0.50, 1.6, 0.35, 2.5, 1.5, 40),
}


def _gt(a, b, d):
    return a > b if d == 1 else a < b


def _rsi_d(x, d):
    return x if d == 1 else 100 - x


def strat_pullback(r, p, d):
    if dir4(r) != d or not _gt(r["h1_ema20"], r["h1_ema50"], d):
        return None
    if not (r["pb_long"] if d == 1 else r["pb_short"]):
        return None
    if not (r["cross_up"] if d == 1 else r["cross_dn"]):
        return None
    if not 40 <= _rsi_d(r["rsi"], d) <= 66 or not _gt(r["close"], r["open"], d):
        return None
    s = 5.0
    if r["h4_adx"] >= 22: s += 1
    if 48 <= _rsi_d(r["h1_rsi"], d) <= 72: s += 1
    if r["vol_ratio"] >= 1.0: s += 1
    if r["vol_ratio"] >= 1.6: s += 0.5
    if r["macd_hist"] * d > 0 or r["macd_hist"] * d > p["macd_hist"] * d: s += 1
    if 20 <= r["atr_pctl"] <= 85: s += 0.5
    a = r["atr"]
    sl = (min(r["low8"], r["close"] - a) - 0.1 * a) if d == 1 else (max(r["high8"], r["close"] + a) + 0.1 * a)
    return s, sl


def strat_breakout(r, p, d):
    d4 = dir4(r)
    if d4 == -d:
        return None
    lvl = r["don_hi"] if d == 1 else r["don_lo"]
    if not _gt(r["close"], lvl, d) or not _gt(r["close"], r["open"], d):
        return None
    if r["vol_ratio"] < 1.4 or r["body_ratio"] < 0.5 or r["adx"] < 18:
        return None
    if (r["close"] - r["ema20"]) * d > 2.5 * r["atr"] or _rsi_d(r["rsi"], d) > 78:
        return None
    s = 5.0
    if d4 == d: s += 1
    if _gt(r["h1_ema20"], r["h1_ema50"], d): s += 1
    if r["vol_ratio"] >= 2.0: s += 1
    if r["adx"] >= 25: s += 1
    if r["macd_hist"] * d > 0: s += 0.5
    if _rsi_d(r["h1_rsi"], d) > 55: s += 0.5
    a = r["atr"]
    sl = min(lvl - 0.5 * a, r["close"] - a) if d == 1 else max(lvl + 0.5 * a, r["close"] + a)
    return s, sl


def strat_reversion(r, p, d):
    d4 = dir4(r)
    if (d4 == -d and r["h4_adx"] >= 25) or r["adx"] > 25:
        return None
    if d == 1:
        ok = r["low"] <= r["bb_lo"] and r["close"] > r["bb_lo"]
    else:
        ok = r["high"] >= r["bb_up"] and r["close"] < r["bb_up"]
    if not ok or _rsi_d(r["rsi"], d) > 38 or not _gt(r["close"], r["open"], d):
        return None
    s = 5.0
    if _rsi_d(r["rsi"], d) <= 30: s += 1
    if d4 * d >= 0: s += 1
    if r["h4_adx"] < 20: s += 1
    if r["vol_ratio"] >= 1.2: s += 1
    if (r["lower_wick"] if d == 1 else r["upper_wick"]) >= 0.4: s += 1
    a = r["atr"]
    sl = (r["low"] - 0.4 * a) if d == 1 else (r["high"] + 0.4 * a)
    return s, sl


STRATS = {"pullback": strat_pullback, "breakout": strat_breakout, "reversion": strat_reversion}
REQUIRED = ["h4_ema200", "h1_ema50", "atr", "ema50", "rsi", "bb_up", "don_hi", "atr_pctl", "vol_ratio", "macd_hist"]


class SignalEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def scan(self, symbol: str, r: dict, p: dict, btc_dir: int = 0, score_adj: float = 0.0,
             defensive: bool = False) -> Optional[Signal]:
        cfg = self.cfg
        if any(_nan(r.get(k)) for k in REQUIRED) or _nan(p.get("macd_hist")):
            return None
        if r["atr_pctl"] > cfg.atr_pctl_max or r["vol_ratio"] < 0.5:
            return None
        best = None
        rt_cost_pct = 2 * (cfg.fee_pct + cfg.slippage_pct)
        for d in (1, -1):
            if d == -1 and not cfg.allow_short:
                continue
            for name in cfg.strategies:
                if defensive and name == "reversion":
                    continue
                res = STRATS[name](r, p, d)
                if not res:
                    continue
                score, sl = res
                if symbol not in ("BTC/USDT", "PAXG/USDT") and btc_dir:
                    score += 0.5 if btc_dir == d else -1.0
                if score < cfg.min_score + score_adj:
                    continue
                entry, a = float(r["close"]), float(r["atr"])
                risk = abs(entry - sl)
                if risk > 3.2 * a:
                    continue
                if risk < 0.9 * a:
                    sl = entry - d * 0.9 * a
                    risk = 0.9 * a
                if risk / entry * 100 < cfg.min_risk_cost_mult * rt_cost_pct:
                    continue
                if best is None or score > best.score:
                    best = Signal(symbol, "BUY" if d == 1 else "SELL", name, round(min(score, 10.0), 2),
                                  entry, float(sl), a, str(r["timestamp"]),
                                  {"dir4": dir4(r), "h4_adx": round(float(r["h4_adx"]), 1),
                                   "rsi": round(float(r["rsi"]), 1), "vol_ratio": round(float(r["vol_ratio"]), 2),
                                   "atr_pctl": round(float(r["atr_pctl"]), 0), "btc_dir": btc_dir})
        return best


# ============================ معامله و مدیریت خروج ============================
@dataclass
class Trade:
    id: str
    symbol: str
    side: str
    strategy: str
    score: float
    entry: float
    exec_entry: float
    sl: float
    init_sl: float
    risk: float
    qty: float
    risk_usdt: float
    tp1: float
    tp2: float
    tp3: float
    tp1_frac: float
    tp2_frac: float
    trail_mult: float
    atr: float
    max_bars: int
    open_time: str
    last_ts: Optional[str] = None
    remaining: float = 1.0
    tp1_hit: bool = False
    tp2_hit: bool = False
    best: float = 0.0
    bars: int = 0
    realized_r: float = 0.0
    realized_usdt: float = 0.0
    closed: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def d(self) -> int:
        return 1 if self.side == "BUY" else -1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, x: dict) -> "Trade":
        return cls(**x)

    def _exit(self, price: float, frac: float, reason: str, cfg: Config, stop: bool = False) -> dict:
        slip = cfg.slippage_pct / 100 * (2 if stop else 1)
        fee = cfg.fee_pct / 100
        exec_exit = price * (1 - self.d * slip)
        per_unit = self.d * (exec_exit - self.exec_entry) - fee * (self.exec_entry + exec_exit)
        usdt = per_unit * self.qty * frac
        r = usdt / self.risk_usdt if self.risk_usdt else 0.0
        self.realized_usdt += usdt
        self.realized_r += r
        return {"reason": reason, "frac": frac, "price": price, "usdt": usdt, "r": r}

    def step(self, c: dict, cfg: Config) -> List[dict]:
        """یک کندل رو پردازش می‌کنه. اگه SL و TP هم‌زمان در یک کندل باشن، محافظه‌کارانه SL اول فرض می‌شه."""
        d, ev = self.d, []
        hi, lo, cl = c["high"], c["low"], c["close"]
        self.bars += 1

        if (d == 1 and lo <= self.sl) or (d == -1 and hi >= self.sl):
            ev.append(self._exit(self.sl, self.remaining, "BE/Trail" if self.tp1_hit else "SL", cfg, stop=True))
            self.remaining, self.closed = 0.0, True
            return ev

        def reached(level):
            return hi >= level if d == 1 else lo <= level

        if not self.tp1_hit and reached(self.tp1):
            f = min(self.tp1_frac, self.remaining)
            ev.append(self._exit(self.tp1, f, "TP1", cfg))
            self.remaining -= f
            self.tp1_hit = True
            self.sl = self.entry + d * self.risk * cfg.be_buffer_r
        if self.tp1_hit and not self.tp2_hit and reached(self.tp2):
            f = min(self.tp2_frac, self.remaining)
            ev.append(self._exit(self.tp2, f, "TP2", cfg))
            self.remaining -= f
            self.tp2_hit = True
        if self.tp1_hit and self.remaining > 1e-9 and reached(self.tp3):
            ev.append(self._exit(self.tp3, self.remaining, "TP3", cfg))
            self.remaining, self.closed = 0.0, True
            return ev

        self.best = max(self.best, hi) if d == 1 else min(self.best, lo)
        if self.tp1_hit:
            trail = self.best - d * self.trail_mult * self.atr
            self.sl = max(self.sl, trail) if d == 1 else min(self.sl, trail)

        if (not self.tp1_hit and self.bars >= self.max_bars) or self.bars >= self.max_bars * 3:
            ev.append(self._exit(cl, self.remaining, "Time stop", cfg))
            self.remaining, self.closed = 0.0, True
        return ev


# ============================ خودتنظیمی بر اساس عملکرد ============================
class PerformanceTracker:
    """وزن‌دهی و خاموش/روشن‌کردن خودکار استراتژی/نماد بر اساس expectancy واقعی اخیر.
    استراتژی‌ای که خراب بشه کاملاً خاموش نمی‌شه (probation با حجم کم) تا بتونه برگرده."""

    def __init__(self):
        self.recs: List[dict] = []

    def record(self, strategy, symbol, side, r, ts: datetime):
        self.recs.append({"strategy": strategy, "symbol": symbol, "side": side, "r": float(r), "ts": ts.isoformat()})
        self.recs = self.recs[-600:]

    def evaluate(self, strategy, symbol, side, now: datetime) -> Tuple[bool, float, str]:
        strat = [x for x in self.recs if x["strategy"] == strategy][-20:]
        n = len(strat)
        exp = sum(x["r"] for x in strat) / (n + 5)
        sym = [x for x in self.recs if x["strategy"] == strategy and x["symbol"] == symbol][-8:]
        exp_s = sum(x["r"] for x in sym) / (len(sym) + 4)
        mult = 1 + 0.6 * exp + 0.4 * exp_s
        side_recs = [x for x in self.recs if x["strategy"] == strategy and x["side"] == side][-12:]
        if len(side_recs) >= 6 and sum(x["r"] for x in side_recs) / len(side_recs) < -0.3:
            mult *= 0.6
        mult = max(0.4, min(1.25, mult))

        if len(sym) >= 4 and sum(x["r"] for x in sym) <= -3.0:
            last = datetime.fromisoformat(sym[-1]["ts"])
            if now - last < timedelta(hours=6):
                return False, mult, "نماد/استراتژی موقتاً در استراحت (ضرر اخیر)"
        if n >= 10 and exp < -0.2:
            last = datetime.fromisoformat(strat[-1]["ts"])
            if now - last < timedelta(hours=12):
                return False, mult, "استراتژی در حالت خاموش موقت (expectancy منفی)"
            return True, 0.4, "probation"
        return True, mult, ""

    def summary(self) -> Dict[str, dict]:
        out = {}
        for name in EXITS:
            xs = [x["r"] for x in self.recs if x["strategy"] == name][-20:]
            if xs:
                out[name] = {"n": len(xs), "avg_r": round(sum(xs) / len(xs), 2),
                             "win": round(sum(1 for v in xs if v > 0) / len(xs) * 100)}
        return out


class PortfolioState:
    def __init__(self):
        self.state = "NORMAL"
        self.stats: dict = {}

    def recompute(self, journal: List[dict]) -> Optional[Tuple[str, str]]:
        recent = journal[-12:]
        n = len(recent)
        cons = 0
        for t in reversed(journal):
            if t["r_total"] <= 0:
                cons += 1
            else:
                break
        avg_r = sum(t["r_total"] for t in recent) / n if n else 0.0
        win = sum(1 for t in recent if t["r_total"] > 0) / n * 100 if n else 0.0
        self.stats = {"n": n, "avg_r": round(avg_r, 2), "win_rate": round(win, 1), "consec_losses": cons}
        if n < 6:
            new = "NORMAL"
        elif cons >= 4 or avg_r <= -0.35:
            new = "DEFENSIVE"
        elif avg_r < 0.0 or win < 38:
            new = "CAUTION"
        else:
            new = "NORMAL"
        prev, self.state = self.state, new
        return (prev, new) if prev != new else None

    @property
    def score_adj(self):
        return {"NORMAL": 0.0, "CAUTION": 0.75, "DEFENSIVE": 1.5}[self.state]

    @property
    def size_mult(self):
        return {"NORMAL": 1.0, "CAUTION": 0.7, "DEFENSIVE": 0.5}[self.state]


# ============================ هسته‌ی معاملاتی و ریسک پرتفوی ============================
class TradingCore:
    def __init__(self, cfg: Config, equity: Optional[float] = None):
        self.cfg = cfg
        self.equity = float(cfg.capital if equity is None else equity)
        self.peak = self.equity
        self.day = None
        self.day_start_equity = self.equity
        self.open: Dict[str, Trade] = {}
        self.journal: List[dict] = []
        self.perf = PerformanceTracker()
        self.pstate = PortfolioState()
        self.last_open: Dict[str, datetime] = {}
        self.pause_until: Optional[datetime] = None
        self.probation_left = 0
        self.listeners = []
        self.eq_curve: List[Tuple[str, float]] = []
        self._seq = 0

    def emit(self, kind, **kw):
        for fn in self.listeners:
            try:
                fn(kind, **kw)
            except Exception:
                pass

    def drawdown_pct(self) -> float:
        return (self.peak - self.equity) / self.peak * 100 if self.peak > 0 else 0.0

    def _roll_day(self, now):
        if self.day != now.date():
            self.day = now.date()
            self.day_start_equity = self.equity

    def _dd_mult(self) -> float:
        dd = self.drawdown_pct()
        if dd >= self.cfg.dd_quarter_pct: return 0.35
        if dd >= self.cfg.dd_half_pct: return 0.6
        return 1.0

    def try_open(self, sig: Signal, now: datetime, extra_mult: float = 1.0) -> Tuple[Optional[Trade], str]:
        cfg = self.cfg
        self._roll_day(now)
        if self.pause_until:
            if now < self.pause_until:
                return None, "توقف موقت به‌خاطر دراودان"
            self.pause_until, self.peak, self.probation_left = None, self.equity, 10
            self.emit("resume")
        if self.drawdown_pct() >= cfg.dd_pause_pct:
            self.pause_until = now + timedelta(hours=cfg.pause_hours)
            self.emit("pause", dd=self.drawdown_pct())
            return None, "فعال‌شدن توقف دراودان"
        if self.equity <= self.day_start_equity * (1 - cfg.daily_loss_limit_pct / 100):
            return None, "سقف ضرر روزانه"
        if any(t.symbol == sig.symbol for t in self.open.values()):
            return None, "معامله‌ی باز روی همین نماد"
        if len(self.open) >= cfg.max_concurrent:
            return None, "سقف تعداد معاملات هم‌زمان"
        same = [t for t in self.open.values() if t.side == sig.side]
        if len(same) >= cfg.max_same_direction:
            return None, "سقف اکسپوژر هم‌جهت (همبستگی کریپتو)"
        grp = GROUPS.get(sig.symbol, "other")
        if sum(1 for t in same if GROUPS.get(t.symbol, "other") == grp) >= cfg.max_per_group:
            return None, f"سقف اکسپوژر گروه {grp}"
        lo = self.last_open.get(sig.symbol)
        if lo and now - lo < timedelta(minutes=cfg.cooldown_min):
            return None, "کول‌داون نماد"
        ok, perf_mult, why = self.perf.evaluate(sig.strategy, sig.symbol, sig.side, now)
        if not ok:
            return None, why

        score_mult = max(0.85, min(1.2, 0.85 + 0.1 * (sig.score - cfg.min_score)))
        prob = 0.5 if self.probation_left > 0 else 1.0
        total_mult = self._dd_mult() * self.pstate.size_mult * perf_mult * score_mult * extra_mult * prob
        risk_usdt = self.equity * cfg.risk_pct / 100 * total_mult

        d = 1 if sig.side == "BUY" else -1
        risk = abs(sig.entry - sig.sl)
        rt = 2 * (cfg.fee_pct + cfg.slippage_pct) / 100
        qty = risk_usdt / (risk + sig.entry * rt)
        cap = self.equity * cfg.max_notional_pct / 100
        if qty * sig.entry > cap:
            scale = cap / (qty * sig.entry)
            qty, risk_usdt = qty * scale, risk_usdt * scale

        heat = sum(t.risk_usdt for t in self.open.values() if not t.tp1_hit)
        if heat + risk_usdt > self.equity * cfg.max_heat_pct / 100:
            return None, "سقف ریسک کل پرتفوی (heat)"

        tp1r, f1, tp2r, f2, tp3r, trail, max_bars = EXITS[sig.strategy]
        self._seq += 1
        t = Trade(
            id=f"{sig.symbol.replace('/', '')}-{int(now.timestamp())}-{self._seq}",
            symbol=sig.symbol, side=sig.side, strategy=sig.strategy, score=sig.score,
            entry=sig.entry, exec_entry=sig.entry * (1 + d * cfg.slippage_pct / 100),
            sl=sig.sl, init_sl=sig.sl, risk=risk, qty=qty, risk_usdt=risk_usdt,
            tp1=sig.entry + d * risk * tp1r, tp2=sig.entry + d * risk * tp2r, tp3=sig.entry + d * risk * tp3r,
            tp1_frac=f1, tp2_frac=f2, trail_mult=trail, atr=sig.atr, max_bars=max_bars,
            open_time=now.isoformat(), best=sig.entry, meta={**sig.meta, "size_mult": round(total_mult, 2),
                                                             "perf_mult": round(perf_mult, 2)})
        self.open[t.id] = t
        self.last_open[sig.symbol] = now
        self.emit("open", trade=t, signal=sig)
        return t, ""

    def process_candle(self, tid: str, candle: dict, now: datetime):
        t = self.open.get(tid)
        if t is None:
            return
        for e in t.step(candle, self.cfg):
            self.equity += e["usdt"]
            self.peak = max(self.peak, self.equity)
            self.eq_curve.append((now.isoformat(), self.equity))
            self.emit("exit", trade=t, event=e)
        if t.closed:
            self._finalize(t, now)

    def _finalize(self, t: Trade, now: datetime):
        self.open.pop(t.id, None)
        rec = {"date": now.date().isoformat(), "ts": now.isoformat(), "symbol": t.symbol, "side": t.side,
               "strategy": t.strategy, "r_total": round(t.realized_r, 3), "usdt": round(t.realized_usdt, 2)}
        self.journal.append(rec)
        self.perf.record(t.strategy, t.symbol, t.side, t.realized_r, now)
        if self.probation_left > 0:
            self.probation_left -= 1
        change = self.pstate.recompute(self.journal)
        if change:
            self.emit("state", prev=change[0], new=change[1], stats=self.pstate.stats)
        if self.drawdown_pct() >= self.cfg.dd_pause_pct and not self.pause_until:
            self.pause_until = now + timedelta(hours=self.cfg.pause_hours)
            self.emit("pause", dd=self.drawdown_pct())
        self.emit("closed", trade=t, record=rec)
