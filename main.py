"""
bot.py - ربات سیگنال زنده (نسخه‌ی مهندسی‌شده)
هسته‌ی تصمیم‌گیری در engine.py (مشترک با backtest.py). این فایل فقط: داده‌ی زنده، تلگرام،
مانیتور معاملات، AI مشورتی (اختیاری، fail-open) و ذخیره‌ی وضعیت رو مدیریت می‌کنه.
"""
import html
import json
import logging
import os
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Optional

import pandas as pd
import requests
from dotenv import load_dotenv

from engine import (Config, SignalEngine, Trade, TradingCore, dir4, prepare, utcnow, TF_MIN)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s",
                    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])
log = logging.getLogger("bot")

HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]


# ============================ داده‌ی زنده ============================
class DataFeed:
    def __init__(self):
        self.cache: Dict[tuple, tuple] = {}

    def _get(self, path, params):
        err = None
        for h in HOSTS:
            try:
                r = requests.get(f"{h}{path}", params=params, timeout=10)
                if r.status_code == 200:
                    return r.json()
                err = f"{h} HTTP {r.status_code}"
            except Exception as e:
                err = f"{h} {e}"
        raise RuntimeError(err)

    def klines(self, symbol: str, tf: str, limit: int, ttl: int = 0) -> pd.DataFrame:
        key = (symbol, tf, limit)
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        raw = self._get("/api/v3/klines", {"symbol": symbol.replace("/", ""), "interval": tf, "limit": limit})
        df = pd.DataFrame([[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in raw],
                          columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        mins = {"1m": 1, "15m": 15, "1h": 60, "4h": 240}[tf]
        # کندل آخر معمولاً هنوز بسته نشده؛ فقط کندل‌های کاملاً بسته‌شده نگه داشته می‌شن
        if not df.empty and df["timestamp"].iloc[-1] + pd.Timedelta(minutes=mins) > pd.Timestamp(utcnow()):
            df = df.iloc[:-1]
        df = df.reset_index(drop=True)
        self.cache[key] = (time.time(), df)
        return df


# ============================ تلگرام ============================
class Telegram:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"

    def _send(self, chat, text):
        if not chat or not self.cfg.telegram_token:
            return
        for mode in ("HTML", None):
            try:
                payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
                if mode:
                    payload["parse_mode"] = mode
                r = requests.post(self.url, json=payload, timeout=10)
                if r.status_code == 200:
                    return
                log.error(f"telegram {r.status_code}: {r.text[:150]}")
            except Exception as e:
                log.error(f"telegram error: {e}")
                return

    def channel(self, text):
        self._send(self.cfg.telegram_chat_id, text)

    def personal(self, text):
        self._send(self.cfg.personal_chat_id or self.cfg.telegram_chat_id, text)


def esc(x) -> str:
    return html.escape(str(x))


def fmt(x: float) -> str:
    return f"{x:,.2f}" if x >= 100 else f"{x:,.4f}" if x >= 1 else f"{x:.6f}"


# ============================ AI مشورتی ============================
class AIAdvisor:
    """AI فقط می‌تونه وتو کنه (با اطمینان بالا) یا حجم رو کم کنه؛ هیچ‌وقت حجم رو زیاد نمی‌کنه.
    اگه AI خراب/قطع/تموم‌شده باشه، سیستم بدون اون و با همون منطق کمی ادامه می‌ده (fail-open)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.calls_today, self.day = 0, utcnow().date()
        self.fail_streak, self.skip_until = 0, 0.0

    def available(self) -> bool:
        if not (self.cfg.ai_enabled and self.cfg.groq_api_key):
            return False
        if utcnow().date() != self.day:
            self.day, self.calls_today = utcnow().date(), 0
        return self.calls_today < self.cfg.ai_daily_call_cap and time.time() >= self.skip_until

    def review(self, sig, ctx: dict) -> dict:
        neutral = {"veto": False, "size_mult": 1.0, "note": ""}
        if not self.available():
            return neutral
        prompt = (
            "You are a risk-focused veteran crypto trader. A systematic strategy produced the trade below and it "
            "already passed quantitative filters. Only flag a problem if you see a concrete contextual red flag "
            "(exhaustion, conflicting macro, recent portfolio losing streak, thin volume). Otherwise approve.\n"
            f"Trade: {json.dumps({'symbol': sig.symbol, 'side': sig.side, 'strategy': sig.strategy, 'score': sig.score, **sig.meta}, ensure_ascii=False)}\n"
            f"Portfolio: {json.dumps(ctx, ensure_ascii=False)}\n"
            'Reply ONLY JSON: {"approve": true|false, "confidence": 0-100, "size_mult": 0.5-1.0, "note": "one short Persian sentence"}')
        try:
            self.calls_today += 1
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                              headers={"Authorization": f"Bearer {self.cfg.groq_api_key}"},
                              json={"model": self.cfg.groq_model, "messages": [{"role": "user", "content": prompt}],
                                    "temperature": 0.2, "max_tokens": 500, "reasoning_effort": "low"}, timeout=15)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            txt = r.json()["choices"][0]["message"]["content"] or ""
            a, b = txt.find("{"), txt.rfind("}")
            res = json.loads(txt[a:b + 1])
            self.fail_streak = 0
            veto = (not bool(res.get("approve", True))) and int(res.get("confidence", 0)) >= self.cfg.ai_veto_conf
            return {"veto": veto, "size_mult": max(0.5, min(1.0, float(res.get("size_mult", 1.0)))),
                    "note": str(res.get("note", ""))[:200]}
        except Exception as e:
            self.fail_streak += 1
            log.warning(f"AI review failed ({e}) - ادامه بدون AI")
            if self.fail_streak >= 3:
                self.skip_until = time.time() + 900
                self.fail_streak = 0
            return neutral


# ============================ ربات ============================
class Bot:
    def __init__(self):
        self.cfg = Config.from_env()
        if not self.cfg.telegram_token or not self.cfg.telegram_chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID باید تنظیم بشن")
        self.feed, self.tg, self.ai = DataFeed(), Telegram(self.cfg), AIAdvisor(self.cfg)
        self.engine = SignalEngine(self.cfg)
        self.core = TradingCore(self.cfg)
        self.lock = threading.RLock()
        self.last_scanned: Dict[str, pd.Timestamp] = {}
        self.last_report_day = utcnow().date()
        self.running = True
        self._load()
        self.core.listeners.append(self._on_event)

    # ---- ذخیره/بازیابی ----
    def _save(self):
        c = self.core
        data = {"equity": c.equity, "peak": c.peak, "journal": c.journal[-1000:], "perf": c.perf.recs,
                "open": {k: t.to_dict() for k, t in c.open.items()},
                "last_open": {k: v.isoformat() for k, v in c.last_open.items()},
                "pause_until": c.pause_until.isoformat() if c.pause_until else None,
                "probation_left": c.probation_left, "state": c.pstate.state}
        tmp = self.cfg.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.cfg.state_path)
        except Exception as e:
            log.error(f"save failed: {e}")

    def _load(self):
        if not os.path.exists(self.cfg.state_path):
            return
        try:
            with open(self.cfg.state_path, encoding="utf-8") as f:
                d = json.load(f)
            c = self.core
            c.equity, c.peak = d["equity"], d["peak"]
            c.journal, c.perf.recs = d.get("journal", []), d.get("perf", [])
            c.open = {k: Trade.from_dict(v) for k, v in d.get("open", {}).items()}
            from datetime import datetime
            c.last_open = {k: datetime.fromisoformat(v) for k, v in d.get("last_open", {}).items()}
            c.pause_until = datetime.fromisoformat(d["pause_until"]) if d.get("pause_until") else None
            c.probation_left = d.get("probation_left", 0)
            c.pstate.recompute(c.journal)
            log.info(f"وضعیت قبلی بازیابی شد: سرمایه {c.equity:.2f} | {len(c.open)} معامله‌ی باز")
        except Exception as e:
            log.error(f"load failed (شروع تازه): {e}")

    # ---- رویدادها -> تلگرام ----
    def _on_event(self, kind, **kw):
        if kind == "open":
            self._notify_open(kw["trade"], kw["signal"])
        elif kind == "exit":
            t, e = kw["trade"], kw["event"]
            icon = "✅" if e["usdt"] > 0 else "🔻"
            self.tg.channel(f"{icon} <b>{esc(t.symbol)}</b> {esc(t.side)} — {esc(e['reason'])}\n"
                            f"قیمت: {fmt(e['price'])} | سهم بسته‌شده: {e['frac'] * 100:.0f}% | {e['r']:+.2f}R")
        elif kind == "closed":
            t, rec = kw["trade"], kw["record"]
            self.tg.channel(f"📌 <b>{esc(t.symbol)}</b> بسته شد — نتیجه‌ی کل: {rec['r_total']:+.2f}R "
                            f"({rec['usdt']:+.2f} USDT)")
        elif kind == "state":
            names = {"NORMAL": "🟢 عادی", "CAUTION": "🟡 محتاط", "DEFENSIVE": "🔴 تدافعی"}
            s = kw["stats"]
            self.tg.personal(f"🧭 حالت پرتفوی: {names[kw['prev']]} ← {names[kw['new']]}\n"
                             f"{s['n']} معامله‌ی اخیر | برد {s['win_rate']}% | میانگین {s['avg_r']:+.2f}R | "
                             f"ضرر متوالی {s['consec_losses']}")
        elif kind == "pause":
            self.tg.personal(f"⛔ دراودان {kw['dd']:.1f}% — ورود جدید {self.cfg.pause_hours:g} ساعت متوقف شد.")
        elif kind == "resume":
            self.tg.personal("▶️ توقف تموم شد؛ ۱۰ معامله‌ی بعدی با نصف ریسک (پروبیشن).")
        self._save()

    def _notify_open(self, t: Trade, sig):
        d = "LONG 🟢" if t.side == "BUY" else "SHORT 🔴"
        names = {"pullback": "Pullback روند", "breakout": "Breakout حجمی", "reversion": "Reversion رنج"}
        ai = f"\n🧠 AI: {esc(sig.meta.get('ai_note'))}" if sig.meta.get("ai_note") else ""
        self.tg.channel(
            f"<b>{d} — {esc(t.symbol)}</b>\n"
            f"استراتژی: {names.get(t.strategy, t.strategy)} | امتیاز {t.score}/10\n\n"
            f"ورود: <code>{fmt(t.entry)}</code>\n"
            f"استاپ: <code>{fmt(t.init_sl)}</code>\n"
            f"TP1 ({t.tp1_frac * 100:.0f}% + استاپ به سر‌به‌سر): <code>{fmt(t.tp1)}</code>\n"
            f"TP2 ({t.tp2_frac * 100:.0f}%): <code>{fmt(t.tp2)}</code>\n"
            f"TP3 / تریلینگ باقیمانده: <code>{fmt(t.tp3)}</code>\n\n"
            f"ریسک: {t.risk_usdt:.2f} USDT ({t.risk_usdt / self.core.equity * 100:.2f}% سرمایه) | "
            f"حجم: {t.qty:.6g} | ارزش: {t.qty * t.entry:,.0f} USDT{ai}\n"
            f"⏰ {utcnow().strftime('%Y-%m-%d %H:%M')} UTC")

    # ---- اسکن سیگنال ----
    def _btc_dir(self) -> int:
        try:
            b = prepare(self.feed.klines("BTC/USDT", "15m", 500, ttl=30),
                        self.feed.klines("BTC/USDT", "1h", 300, ttl=120),
                        self.feed.klines("BTC/USDT", "4h", 500, ttl=600))
            r = b.iloc[-1]
            return dir4(r) if r["h4_ema200"] == r["h4_ema200"] else 0
        except Exception:
            return 0

    def scan_cycle(self):
        with self.lock:
            adj, defensive = self.core.pstate.score_adj, self.core.pstate.state == "DEFENSIVE"
        btc_dir = self._btc_dir()
        cands = []
        for s in self.cfg.symbols:
            try:
                d15 = self.feed.klines(s, "15m", 500)
                if d15.empty:
                    continue
                ts = d15["timestamp"].iloc[-1]
                if self.last_scanned.get(s) == ts:
                    continue
                self.last_scanned[s] = ts
                if pd.Timestamp(utcnow()) - (ts + pd.Timedelta(minutes=15)) > pd.Timedelta(minutes=20):
                    continue  # کندل کهنه (مثلاً بعد از قطعی) - سیگنال نده
                p = prepare(d15, self.feed.klines(s, "1h", 300, ttl=120), self.feed.klines(s, "4h", 500, ttl=600))
                sig = self.engine.scan(s, p.iloc[-1].to_dict(), p.iloc[-2].to_dict(), btc_dir, adj, defensive)
                if sig:
                    cands.append(sig)
            except Exception as e:
                log.error(f"scan {s}: {e}")
            time.sleep(0.3)
        for sig in sorted(cands, key=lambda x: -x.score):
            with self.lock:
                ctx = {"state": self.core.pstate.state, **self.core.pstate.stats,
                       "open_positions": len(self.core.open), "drawdown_pct": round(self.core.drawdown_pct(), 1)}
            rev = self.ai.review(sig, ctx)
            if rev["veto"]:
                log.info(f"{sig.symbol} {sig.side}: وتوی AI - {rev['note']}")
                continue
            sig.meta["ai_note"] = rev["note"]
            with self.lock:
                t, why = self.core.try_open(sig, utcnow(), extra_mult=rev["size_mult"])
            if t:
                log.info(f"سیگنال {sig.symbol} {sig.side} [{sig.strategy}] امتیاز {sig.score}")
            else:
                log.info(f"{sig.symbol} {sig.side} رد شد: {why}")

    # ---- مانیتور معاملات باز ----
    def monitor_cycle(self):
        with self.lock:
            snap = [(t.id, t.symbol, t.open_time, t.last_ts) for t in self.core.open.values()]
        for tid, sym, open_time, last_ts in snap:
            try:
                start = pd.Timestamp(last_ts or open_time)
                gap = int((pd.Timestamp(utcnow()) - start).total_seconds() / 60) + 3
                df = self.feed.klines(sym, "1m", min(1000, max(gap, 5)))
                df = df[df["timestamp"] >= pd.Timestamp(open_time)]
                if last_ts:
                    df = df[df["timestamp"] > pd.Timestamp(last_ts)]
                with self.lock:
                    t = self.core.open.get(tid)
                    if t is None:
                        continue
                    for _, row in df.iterrows():
                        t.last_ts = row["timestamp"].isoformat()
                        self.core.process_candle(tid, {"high": row["high"], "low": row["low"], "close": row["close"]},
                                                 (row["timestamp"] + pd.Timedelta(minutes=1)).to_pydatetime())
                        if tid not in self.core.open:
                            break
            except Exception as e:
                log.error(f"monitor {sym}: {e}")
        self._save()

    # ---- گزارش روزانه ----
    def daily_report(self):
        today = utcnow().date()
        if today == self.last_report_day:
            return
        day = self.last_report_day.isoformat()
        self.last_report_day = today
        with self.lock:
            rec = [r for r in self.core.journal if r["date"] == day]
            c = self.core
            lines = [f"📊 <b>گزارش روز {day}</b>",
                     f"معاملات بسته‌شده: {len(rec)} | PnL: {sum(r['usdt'] for r in rec):+.2f} USDT | "
                     f"R کل: {sum(r['r_total'] for r in rec):+.2f}",
                     f"سرمایه مجازی: {c.equity:,.2f} | دراودان: {c.drawdown_pct():.1f}% | حالت: {c.pstate.state}"]
            for k, v in c.perf.summary().items():
                lines.append(f"• {k}: {v['n']} معامله، میانگین {v['avg_r']:+.2f}R، برد {v['win']}%")
        self.tg.personal("\n".join(lines))

    def status(self) -> dict:
        with self.lock:
            c = self.core
            return {"equity": round(c.equity, 2), "drawdown_pct": round(c.drawdown_pct(), 2), "state": c.pstate.state,
                    "open": [{"symbol": t.symbol, "side": t.side, "strategy": t.strategy} for t in c.open.values()],
                    "closed_trades": len(c.journal), "ai_calls_today": self.ai.calls_today,
                    "strategies": c.perf.summary()}

    def run(self):
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        self.tg.personal("🛡 ربات نسخه‌ی مهندسی‌شده فعال شد.\n"
                         f"سرمایه: {self.core.equity:,.2f} | ریسک پایه {self.cfg.risk_pct}%/معامله | "
                         f"استراتژی‌ها: {', '.join(self.cfg.strategies)} | Short: {'روشن' if self.cfg.allow_short else 'خاموش'} | "
                         f"AI: {'فعال (مشورتی)' if self.ai.available() else 'غیرفعال'}")
        while self.running:
            try:
                self.scan_cycle()
                self.daily_report()
            except Exception as e:
                log.error(f"loop: {e}")
            time.sleep(self.cfg.scan_seconds)

    def _monitor_loop(self):
        while self.running:
            try:
                self.monitor_cycle()
            except Exception as e:
                log.error(f"monitor loop: {e}")
            time.sleep(self.cfg.monitor_seconds)


def serve(bot: Bot):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(bot.status(), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), H).serve_forever()


if __name__ == "__main__":
    b = Bot()
    threading.Thread(target=serve, args=(b,), daemon=True).start()
    try:
        b.run()
    except KeyboardInterrupt:
        b.running = False
