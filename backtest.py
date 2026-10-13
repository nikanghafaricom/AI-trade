"""
backtest.py - بک‌تست پرتفوی با همون هسته‌ی ربات زنده (engine.py)

مثال‌ها:
  python backtest.py --days 365                 # داده‌ی واقعی بایننس (کش می‌شه)
  python backtest.py --days 365 --segments 4    # تست پایداری (walk-forward ساده)
  python backtest.py --synthetic                # فقط تست سلامت کد روی داده‌ی مصنوعی
  python backtest.py --days 365 --risk 0.75 --min-score 7 --no-short
"""
import argparse
import os
import time
import numpy as np
import pandas as pd
import requests

from engine import Config, SignalEngine, TradingCore, prepare, resample_ohlcv, dir4

HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]


def fetch_klines(symbol: str, days: int, cache_dir: str = "data_cache") -> pd.DataFrame:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{symbol.replace('/', '')}_15m_{days}d.csv")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
        return pd.read_csv(path, parse_dates=["timestamp"])
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    rows, cur = [], start
    while cur < end:
        data = None
        for h in HOSTS:
            try:
                r = requests.get(f"{h}/api/v3/klines", params={"symbol": symbol.replace("/", ""), "interval": "15m",
                                                               "startTime": cur, "limit": 1000}, timeout=15)
                if r.status_code == 200:
                    data = r.json()
                    break
            except Exception:
                continue
        if data is None:
            raise RuntimeError(f"دریافت داده برای {symbol} ناموفق بود (شبکه/بلاک IP؟)")
        if not data:
            break
        rows += data
        cur = data[-1][0] + 15 * 60 * 1000
        time.sleep(0.15)
    df = pd.DataFrame([[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in rows],
                      columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.drop_duplicates("timestamp").iloc[:-1]
    df.to_csv(path, index=False)
    return df


def synthetic(symbols, days: int, seed: int = 7) -> dict:
    """داده‌ی مصنوعی رژیم‌دار (روند/رنج/نوسان) - فقط برای تست سلامت کد، نه ارزیابی سودآوری."""
    rng = np.random.default_rng(seed)
    n = days * 96
    idx = pd.date_range("2025-01-01", periods=n, freq="15min")
    reg = np.zeros(n)
    i = 0
    while i < n:
        L = int(rng.integers(300, 1500))
        reg[i:i + L] = rng.choice([-1, 0, 0, 1, 1])
        i += L
    btc_ret = rng.normal(0, 0.0016, n) + reg * 0.00012
    out = {}
    for k, s in enumerate(symbols):
        beta = 0.6 + 0.5 * rng.random()
        ret = beta * btc_ret + rng.normal(0, 0.0012, n) + reg * 0.00005 * rng.random()
        close = 100 * np.exp(np.cumsum(ret))
        open_ = np.r_[close[0], close[:-1]]
        spread = np.abs(rng.normal(0, 0.0012, n)) + np.abs(ret) * 0.5
        high, low = np.maximum(open_, close) * (1 + spread), np.minimum(open_, close) * (1 - spread)
        vol = rng.lognormal(10, 0.5, n) * (1 + 3 * np.abs(ret) / 0.002)
        out[s] = pd.DataFrame({"timestamp": idx, "open": open_, "high": high, "low": low, "close": close, "volume": vol})
    return out


def run(frames: dict, cfg: Config, start_ts=None, end_ts=None, warmup: int = 400, verbose=False):
    prepared, by_ts = {}, {}
    for s, df15 in frames.items():
        p = prepare(df15, resample_ohlcv(df15, "1h"), resample_ohlcv(df15, "4h"))
        prepared[s] = p.to_dict("records")
        by_ts[s] = {r["timestamp"]: i for i, r in enumerate(prepared[s])}
    timeline = sorted({r["timestamp"] for rows in prepared.values() for r in rows})
    if start_ts is not None:
        timeline = [t for t in timeline if start_ts <= t < end_ts]
    core, eng = TradingCore(cfg), SignalEngine(cfg)
    delta = pd.Timedelta(minutes=15)
    reasons = {}
    for ts in timeline:
        now = (ts + delta).to_pydatetime()
        for tid, t in list(core.open.items()):
            i = by_ts[t.symbol].get(ts)
            if i is not None:
                row = prepared[t.symbol][i]
                core.process_candle(tid, {"high": row["high"], "low": row["low"], "close": row["close"]}, now)
        btc_dir = 0
        if "BTC/USDT" in by_ts and ts in by_ts["BTC/USDT"]:
            br = prepared["BTC/USDT"][by_ts["BTC/USDT"][ts]]
            if br["h4_ema200"] == br["h4_ema200"]:
                btc_dir = dir4(br)
        cands = []
        for s in frames:
            i = by_ts[s].get(ts)
            if i is None or i < warmup:
                continue
            sig = eng.scan(s, prepared[s][i], prepared[s][i - 1], btc_dir, core.pstate.score_adj,
                           core.pstate.state == "DEFENSIVE")
            if sig:
                cands.append(sig)
        for sig in sorted(cands, key=lambda x: -x.score):
            t, why = core.try_open(sig, now)
            if not t:
                reasons[why] = reasons.get(why, 0) + 1
    return core, reasons, (timeline[0], timeline[-1]) if timeline else (None, None)


def metrics(core: TradingCore, cfg: Config, span) -> dict:
    j = core.journal
    if not j:
        return {"trades": 0}
    usdt = np.array([x["usdt"] for x in j])
    wins, losses = usdt[usdt > 0], usdt[usdt <= 0]
    curve = np.array([cfg.capital] + [e for _, e in core.eq_curve])
    peak = np.maximum.accumulate(curve)
    max_dd = float(((peak - curve) / peak).max() * 100)
    days = max((span[1] - span[0]).days, 1)
    per = {}
    for x in j:
        k = x["strategy"]
        per.setdefault(k, []).append(x["r_total"])
    return {
        "trades": len(j), "trades_per_day": round(len(j) / days, 2),
        "win_rate": round(len(wins) / len(j) * 100, 1),
        "avg_R": round(float(np.mean([x["r_total"] for x in j])), 3),
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 2) if losses.sum() != 0 else float("inf"),
        "return_pct": round((core.equity / cfg.capital - 1) * 100, 2),
        "max_dd_pct": round(max_dd, 2),
        "by_strategy": {k: {"n": len(v), "avg_R": round(float(np.mean(v)), 2)} for k, v in per.items()},
        "by_side": {s: sum(1 for x in j if x["side"] == s) for s in ("BUY", "SELL")},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--segments", type=int, default=1)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--risk", type=float)
    ap.add_argument("--min-score", type=float)
    ap.add_argument("--no-short", action="store_true")
    ap.add_argument("--strategies", type=str)
    ap.add_argument("--fee", type=float)
    a = ap.parse_args()

    cfg = Config.from_env()
    if a.risk: cfg.risk_pct = a.risk
    if a.min_score: cfg.min_score = a.min_score
    if a.no_short: cfg.allow_short = False
    if a.strategies: cfg.strategies = a.strategies.split(",")
    if a.fee is not None: cfg.fee_pct = a.fee

    frames = synthetic(cfg.symbols, a.days) if a.synthetic else {}
    if not a.synthetic:
        for s in cfg.symbols:
            try:
                frames[s] = fetch_klines(s, a.days)
                print(f"دریافت شد: {s} ({len(frames[s])} کندل)")
            except Exception as e:
                print(f"رد شد {s}: {e}")
    if not frames:
        print("هیچ داده‌ای در دسترس نیست.")
        return

    first = min(df["timestamp"].iloc[0] for df in frames.values())
    last = max(df["timestamp"].iloc[-1] for df in frames.values())
    edges = pd.date_range(first, last, periods=a.segments + 1)
    for k in range(a.segments):
        st, en = (edges[k], edges[k + 1] + pd.Timedelta(minutes=1)) if a.segments > 1 else (None, None)
        core, reasons, span = run(frames, cfg, st, en)
        m = metrics(core, cfg, span)
        tag = f"بخش {k + 1}/{a.segments}" if a.segments > 1 else "کل دوره"
        print(f"\n===== {tag} =====")
        for kk, v in m.items():
            print(f"{kk}: {v}")
        print("دلایل رد سیگنال:", dict(sorted(reasons.items(), key=lambda x: -x[1])[:5]))


if __name__ == "__main__":
    main()
