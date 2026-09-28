"""
CryptoMomentumBot
ربات تلگرامی معامله‌ی شبیه‌سازی‌شده (Paper Trading) با استراتژی مومنتوم نسبت به BTC.

متغیرهای محیطی:
    TELEGRAM_BOT_TOKEN   (الزامی) توکن ربات
    TELEGRAM_CHAT_ID     (الزامی) شناسه‌ی عددی چتی که اجازه‌ی کار با ربات را دارد
    DATABASE_URL         (اختیاری) اگر تعریف شود، وضعیت در PostgreSQL ذخیره می‌شود
    STATE_FILE           (اختیاری) مسیر فایل وضعیت وقتی دیتابیس نداری (پیش‌فرض: [portfolio_state.jso](https://portfolio_state.jso)n)
    DISPLAY_TZ           (اختیاری) منطقه‌ی زمانی نمایش (پیش‌فرض: Asia/Tehran)
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
import tempfile
import time
from [concurrent.futures](https://concurrent.futures) import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import requests
from telegram import Update
from [telegram.constants](https://telegram.constants) import ParseMode
from [telegram.ext](https://telegram.ext) import ApplicationBuilder, CommandHandler, ContextTypes, filters

# ─────────────────────────── لاگ ───────────────────────────
[logging.basicConfig](https://logging.basicConfig)(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx در سطح INFO آدرس کامل درخواست‌ها را لاگ می‌کند که شامل توکن ربات است
[logging.getLogger](https://logging.getLogger)("httpx").setLevel([logging.WARNIN](https://logging.WARNIN)G)
logger = [logging.getLogger](https://logging.getLogger)("CryptoMomentumBot")

# ─────────────────────────── تنظیمات ───────────────────────────
TELEGRAM_BOT_TOKEN = [os.getenv](https://os.getenv)("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = [os.getenv](https://os.getenv)("TELEGRAM_CHAT_ID", "").strip()
DATABASE_URL = [os.getenv](https://os.getenv)("DATABASE_URL", "").strip()
STATE_FILE = [os.getenv](https://os.getenv)("STATE_FILE", "portfolio_state.json")

try:
    DISPLAY_TZ = ZoneInfo([os.getenv](https://os.getenv)("DISPLAY_TZ", "Asia/Tehran"))
except (ZoneInfoNotFoundError, ValueError):
    DISPLAY_TZ = [timezone.utc](https://timezone.utc)

PAIRS = ["ETHBTC", "BNBBTC", "SOLBTC", "XRPBTC", "DOGEBTC", "ADABTC", "LTCBTC"]

LOOKBACK_HOURS = 240                      # پنجره‌ی محاسبه‌ی مومنتوم (۱۰ روز)
REBALANCE_INTERVAL_SECONDS = 240 * 3600   # فاصله‌ی ریبالانس خودکار (۱۰ روز)
CHECK_INTERVAL_SECONDS = 3600             # هر چند وقت سررسید بررسی شود

TOP_N = 2                  # تعداد ارزهای منتخب
WEIGHT_PER_ASSET = 0.20    # وزن هر ارز؛ با 2×0.20، ۶۰٪ سبد BTC نقد می‌ماند. برای سرمایه‌گذاری کامل: 0.50
REQUIRE_BOTH_POSITIVE = False   # True = شرط ورود سخت‌گیرانه (بازده و روند هر دو مثبت)
MIN_TRADE_FRACTION = 0.01  # معاملات کوچک‌تر از ۱٪ ارزش سبد انجام نمی‌شوند
FEE = 0.00075
INITIAL_BTC = 1.0
MAX_TRADE_HISTORY = 1000
HISTORY_SHOW = 15

REQUEST_TIMEOUT = 10
FETCH_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
KLINE_ENDPOINTS = (
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
)

if TOP_N * WEIGHT_PER_ASSET > 1.0:
    raise ValueError("TOP_N × WEIGHT_PER_ASSET نباید از ۱ بیشتر باشد.")

class DataFetchError(RuntimeError):
    pass

# ─────────────────────────── ذخیره‌سازی ───────────────────────────
class FileStorage:
    """ذخیره در فایل JSON با نوشتن اتمیک."""

    def __init__(self, path: str):
        [self.path](https://self.path) = path

    def load(self) -> dict | None:
        if not [os.path.exists](https://os.path.exists)([self.pat](https://self.pat)h):
            return None
        # اگر فایل خراب باشد خطا می‌دهد؛ عمداً سبد را بی‌صدا ریست نمی‌کنیم
        with open([self.path](https://self.path), "r", encoding="utf-8") as f:
            return [json.load](https://json.load)(f)

    def save(self, state: dict) -> None:
        directory = [os.path.dirname](https://os.path.dirname)([os.path.abspath](https://os.path.abspath)([self.pat](https://self.pat)h))
        [os.makedirs](https://os.makedirs)(directory, exist_ok=True)
        fd, tmp_path = [tempfile.mkstemp](https://tempfile.mkstemp)(dir=directory, prefix=".state-", suffix=".tmp")
        try:
            with [os.fdopen](https://os.fdopen)(fd, "w", encoding="utf-8") as f:
                [json.dump](https://json.dump)(state, f, indent=2, ensure_ascii=False)
                [f.flush](https://f.flush)()
                [os.fsync](https://os.fsync)([f.fileno](https://f.fileno)())
            [os.replace](https://os.replace)(tmp_path, [self.pat](https://self.pat)h)
        except BaseException:
            with [contextlib.suppress](https://contextlib.suppress)(FileNotFoundError):
                [os.remove](https://os.remove)(tmp_path)
            raise

class PostgresStorage:
    """ذخیره در PostgreSQL (مناسب Heroku و سرویس‌هایی با فایل‌سیستم موقتی)."""

    def __init__(self, dsn: str):
        import psycopg
        from [psycopg.types.json](https://psycopg.types.json) import Jsonb

        self._psycopg = psycopg
        self._Jsonb = Jsonb
        self._dsn = dsn
        with self._connect() as conn:
            [conn.execute](https://conn.execute)(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    id         INTEGER PRIMARY KEY,
                    data       JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )

    def _connect(self):
        return [self._psycopg.connect](https://self._psycopg.connect)(self._dsn, connect_timeout=10)

    def load(self) -> dict | None:
        with self._connect() as conn:
            row = [conn.execute](https://conn.execute)("SELECT data FROM bot_state WHERE id = 1").fetchone()
        return row[0] if row else None

    def save(self, state: dict) -> None:
        with self._connect() as conn:
            [conn.execute](https://conn.execute)(
                """
                INSERT INTO bot_state (id, data, updated_at) VALUES (1, %s, now())
                ON CONFLICT (id) DO UPDATE SET data = [EXCLUDED.data](https://EXCLUDED.data), updated_at = now()
                """,
                (self._Jsonb(state),),
            )

def build_storage():
    if DATABASE_URL:
        [logger.info](https://logger.info)("وضعیت در PostgreSQL ذخیره می‌شود.")
        return PostgresStorage(DATABASE_URL)
    [logger.warning](https://logger.warning)(
        "DATABASE_URL تعریف نشده؛ وضعیت در فایل %s ذخیره می‌شود. "
        "روی Heroku این فایل با هر ری‌استارت پاک می‌شود.",
        STATE_FILE,
    )
    return FileStorage(STATE_FILE)

_storage: FileStorage | PostgresStorage | None = None

# ─────────────────────────── وضعیت ───────────────────────────
def default_state() -> dict:
    return {
        "btc_cash": INITIAL_BTC,
        "holdings": {p: 0.0 for p in PAIRS},
        "last_rebalance_ts": 0,
        "trade_history": [],
    }

def normalize_state(raw: dict | None) -> dict:
    if raw is None:
        return default_state()
    if not isinstance(raw, dict):
        raise ValueError("ساختار وضعیت ذخیره‌شده نامعتبر است.")

    holdings = {p: 0.0 for p in PAIRS}
    for pair, qty in ([raw.get](https://raw.get)("holdings") or {}).items():
        holdings[pair] = float(qty)   # دارایی‌های خارج از PAIRS هم حفظ می‌شوند تا فروخته شوند

    return {
        "btc_cash": float([raw.get](https://raw.get)("btc_cash", INITIAL_BTC)),
        "holdings": holdings,
        "last_rebalance_ts": int([raw.get](https://raw.get)("last_rebalance_ts", 0)),
        "trade_history": list([raw.get](https://raw.get)("trade_history") or [])[-MAX_TRADE_HISTORY:],
    }

def load_state() -> dict:
    return normalize_state([_storage.load](https://_storage.load)())

def save_state(state: dict) -> None:
    [_storage.save](https://_storage.save)(state)

def portfolio_value(state: dict, prices: dict) -> tuple[float, list[str]]:
    total = float(state["btc_cash"])
    unknown = []
    for pair, qty in state["holdings"].items():
        if qty <= 0:
            continue
        if pair in prices:
            total += qty * prices[pair]
        else:
            [unknown.append](https://unknown.append)(pair)
    return total, unknown

def is_rebalance_due(state: dict, now: int | None = None) -> bool:
    now = now or int([time.time](https://time.time)())
    last_ts = [state.get](https://state.get)("last_rebalance_ts", 0)
    return last_ts == 0 or (now - last_ts) >= REBALANCE_INTERVAL_SECONDS

# ─────────────────────────── داده‌ی بازار ───────────────────────────
def fetch_pair_data(symbol: str) -> tuple[np.ndarray, float]:
    """
    خروجی: (قیمت‌های بسته‌شدن آخرین LOOKBACK_HOURS کندلِ بسته‌شده، آخرین قیمت لحظه‌ای)
    کندل جاری (بسته‌نشده) در محاسبه‌ی امتیاز استفاده نمی‌شود، فقط به‌عنوان قیمت اجرا.
    """
    params = {"symbol": symbol, "interval": "1h", "limit": LOOKBACK_HOURS + 5}
    last_error: Exception | None = None

    for attempt in range(FETCH_RETRIES):
        for url in KLINE_ENDPOINTS:
            try:
                res = [requests.get](https://requests.get)(url, params=params, timeout=REQUEST_TIMEOUT)
                [res.raise](https://res.raise)_for_status()
                data = [res.json](https://res.json)()
                if not isinstance(data, list) or not data:
                    raise ValueError(f"پاسخ غیرمنتظره: {str(data)[:120]}")

                now_ms = int([time.time](https://time.time)() * 1000)
                closed = [k for k in data if int(k[6]) < now_ms]
                if len(closed) < LOOKBACK_HOURS:
                    raise ValueError(
                        f"فقط {len(closed)} کندل بسته‌شده موجود است (حداقل {LOOKBACK_HOURS} لازم است)"
                    )

                closes = [np.array](https://np.array)(
                    [float(k[4]) for k in closed[-LOOKBACK_HOURS:]], dtype=np.float64
                )
                last_price = float(data[-1][4])
                if not [np.all](https://np.all)([np.isfinite](https://np.isfinite)(closes)) or [np.any](https://np.any)(closes <= 0) or last_price <= 0:
                    raise ValueError("قیمت نامعتبر در داده‌ها")
                return closes, last_price

            except ([requests.RequestException](https://requests.RequestException), ValueError, TypeError, IndexError) as exc:
                last_error = exc
                [logger.warning](https://logger.warning)(
                    "دریافت %s از %s ناموفق بود (تلاش %d/%d): %s",
                    symbol, url, attempt + 1, FETCH_RETRIES, exc,
                )

        if attempt < FETCH_RETRIES - 1:
            [time.sleep](https://time.sleep)(RETRY_BACKOFF_SECONDS * (2 ** attempt))

    raise DataFetchError(f"{symbol}: {last_error}")

def trend_metrics(closes: [np.ndarra](https://np.ndarra)y) -> tuple[float, float, float]:
    """بازده کل، شیب رگرسیون روی قیمت نرمال‌شده، و R²."""
    c_old, c_now = float(closes[0]), float(closes[-1])
    ret = c_now / c_old - 1.0

    y = closes / c_old
    x = [np.arange](https://np.arange)(len(y), dtype=np.float64)
    x_diff = x - [x.mean](https://x.mean)()
    y_diff = y - [y.mean](https://y.mean)()

    x_var = float([np.sum](https://np.sum)(x_diff ** 2))
    cov = float([np.sum](https://np.sum)(x_diff * y_diff))
    ss_tot = float([np.sum](https://np.sum)(y_diff ** 2))

    slope = cov / x_var
    r2 = (cov ** 2) / (x_var * ss_tot) if ss_tot > 0 else 0.0
    return ret, slope, r2

def zscore(a: [np.ndarra](https://np.ndarra)y) -> [np.ndarray](https://np.ndarray):
    std = float([np.std](https://np.std)(a))
    if std == 0 or not [np.isfinite](https://np.isfinite)(std):
        return [np.zeros](https://np.zeros)_like(a)
    return (a - [np.mean](https://np.mean)(a)) / std

def compute_scores(pair_data: dict[str, tuple[np.ndarray, float]]) -> dict:
    pairs = [p for p in PAIRS if p in pair_data]
    rets, trends, r2s = [], [], []

    for p in pairs:
        ret, slope, r2 = trend_metrics(pair_data[p][0])
        [rets.append](https://rets.append)(ret)
        [trends.append](https://trends.append)(slope * r2)
        [r2s.append](https://r2s.append)(r2)

    blend = 0.5 * zscore([np.array](https://np.array)(rets)) + 0.5 * zscore([np.array](https://np.array)(trends))

    scores = {}
    for i, p in enumerate(pairs):
        if REQUIRE_BOTH_POSITIVE:
            eligible = rets[i] > 0 and trends[i] > 0
        else:
            eligible = rets[i] > 0 or trends[i] > 0
        scores[p] = {
            "score": float(blend[i]),
            "eligible": bool(eligible),
            "return_pct": float(rets[i] * 100),
            "trend_score": float(trends[i]),
            "r2": float(r2s[i]),
            "price": float(pair_data[p][1]),
        }
    return scores

def fetch_and_score() -> tuple[dict, dict, dict]:
    """دریافت موازی داده‌ی همه‌ی جفت‌ها. خروجی: (scores, prices, failed)"""
    pair_data: dict[str, tuple[np.ndarray, float]] = {}
    failed: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(PAIRS)) as pool:
        futures = {pool.submit(fetch_pair_data, p): p for p in PAIRS}
        for future in as_completed(futures):
            pair = futures[future]
            try:
                pair_data[pair] = future.result()
            except Exception as exc:
                failed[pair] = str(exc)
                [logger.error](https://logger.error)("داده‌ی %s دریافت نشد: %s", pair, exc)

    if len(pair_data) < 2:
        raise DataFetchError("داده‌ی کافی برای رتبه‌بندی دریافت نشد (کمتر از ۲ جفت‌ارز).")

    scores = compute_scores(pair_data)
    prices = {p: s["price"] for p, s in [scores.items](https://scores.items)()}
    return scores, prices, failed

# ─────────────────────────── ریبالانس ───────────────────────────
def execute_rebalance(state: dict, scores: dict, prices: dict) -> tuple[dict, list[str], list[str]]:
    """
    تابع خالص: وضعیت ورودی را تغییر نمی‌دهد و وضعیت جدید را برمی‌گرداند.
    ترتیب: ۱) فروش ارزهای خارج از هدف ۲) کاهش وزنِ هدف‌های اضافه‌وزن ۳) خرید.
    """
    missing = [p for p, q in state["holdings"].items() if q > 0 and p not in prices]
    if missing:
        raise RuntimeError(
            "قیمت این دارایی‌ها دریافت نشد و ریبالانس لغو شد: " + ", ".join(missing)
        )

    ranked = sorted(
        (p for p in scores if scores[p]["eligible"]),
        key=lambda p: scores[p]["score"],
        reverse=True,
    )
    targets = ranked[:TOP_N]

    holdings = {p: float(q) for p, q in state["holdings"].items()}
    btc_cash = float(state["btc_cash"])
    total_before, _ = portfolio_value(state, prices)
    min_trade = total_before * MIN_TRADE_FRACTION
    target_value = total_before * WEIGHT_PER_ASSET
    now = int([time.time](https://time.time)())

    trades: list[dict] = []
    logs: list[str] = []

    def sell(pair: str, qty: float, full: bool) -> None:
        nonlocal btc_cash
        price = prices[pair]
        gross = qty * price
        fee = gross * FEE
        net = gross - fee
        holdings[pair] = 0.0 if full else max(holdings[pair] - qty, 0.0)
        btc_cash += net
        [trades.append](https://trades.append)({
            "ts": now, "side": "SELL", "pair": pair, "qty": qty,
            "price": price, "btc": net, "fee_btc": fee,
            "reason": "exit" if full else "trim",
        })
        label = "🔴 فروش کامل" if full else "🟡 کاهش وزن"
        [logs.append](https://logs.append)(f"{label} {pair}: {qty:,.4f} واحد ← {net:.6f} BTC")

    # ۱) فروش ارزهایی که دیگر در هدف نیستند
    for pair, qty in list([holdings.items](https://holdings.items)()):
        if qty > 0 and pair not in targets:
            sell(pair, qty, full=True)

    # ۲) کاهش وزن هدف‌هایی که بیش از وزن هدف رشد کرده‌اند
    for pair in targets:
        excess_value = [holdings.get](https://holdings.get)(pair, 0.0) * prices[pair] - target_value
        if excess_value > min_trade:
            qty = min(excess_value / prices[pair], holdings[pair])
            sell(pair, qty, full=False)

    # ۳) خرید تا رسیدن به وزن هدف (در صورت کمبود نقدینگی، خرید جزئی)
    for pair in targets:
        deficit = target_value - [holdings.get](https://holdings.get)(pair, 0.0) * prices[pair]
        if deficit <= min_trade:
            continue
        spend = min(deficit, btc_cash)
        if spend <= min_trade:
            [logs.append](https://logs.append)(f"⚠️ خرید {pair} انجام نشد: نقدینگی کافی نیست.")
            continue

        price = prices[pair]
        fee = spend * FEE
        qty = (spend - fee) / price
        holdings[pair] = holdings.get(pair, 0.0) + qty
        btc_cash -= spend
        [trades.append](https://trades.append)({
            "ts": now, "side": "BUY", "pair": pair, "qty": qty,
            "price": price, "btc": spend, "fee_btc": fee, "reason": "entry",
        })
        note = " (خرید جزئی)" if spend < deficit else ""
        [logs.append](https://logs.append)(f"🟢 خرید {pair}: {spend:.6f} BTC ← {qty:,.4f} واحد{note}")

    # حذف دارایی‌های صفرشده‌ای که دیگر جزو PAIRS نیستند
    for pair in list(holdings):
        if pair not in PAIRS and holdings[pair] == 0:
            del holdings[pair]

    new_state = {
        "btc_cash": btc_cash,
        "holdings": holdings,
        "last_rebalance_ts": now,
        "trade_history": (state["trade_history"] + trades)[-MAX_TRADE_HISTORY:],
    }
    return new_state, targets, logs

REBALANCE_LOCK = [asyncio.Lock](https://asyncio.Lock)()

async def run_rebalance(force: bool):
    """
    ریبالانس را زیر قفل اجرا می‌کند تا اجرای دستی و خودکار هم‌زمان نشوند.
    اگر force=False باشد و سررسید نرسیده باشد، None برمی‌گرداند.
    """
    async with REBALANCE_LOCK:
        state = await [asyncio.to](https://asyncio.to)_thread(load_state)
        if not force and not is_rebalance_due(state):
            return None

        scores, prices, failed = await [asyncio.to](https://asyncio.to)_thread(fetch_and_score)
        new_state, targets, logs = execute_rebalance(state, scores, prices)
        await [asyncio.to](https://asyncio.to)_thread(save_state, new_state)

        [logger.info](https://logger.info)("ریبالانس انجام شد. اهداف: %s | %d معامله", targets, len(logs))
        return new_state, scores, prices, failed, targets, logs

# ─────────────────────────── قالب‌بندی پیام‌ها (HTML) ───────────────────────────
def fmt_ts(ts: int) -> str:
    if not ts:
        return "هنوز انجام نشده"
    return [datetime.fromtimestamp](https://datetime.fromtimestamp)(ts, tz=DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")

def pct(part: float, total: float) -> float:
    return (part / total * 100) if total > 0 else 0.0

def format_status_message(
    state: dict,
    scores: dict,
    prices: dict,
    failed: dict | None = None,
    targets: list[str] | None = None,
    logs: list[str] | None = None,
    title: str | None = None,
) -> str:
    e = [html.escape](https://html.escape)
    total, unknown = portfolio_value(state, prices)
    cash = state["btc_cash"]
    last_ts = [state.get](https://state.get)("last_rebalance_ts", 0)
    pnl = (total / INITIAL_BTC - 1) * 100

    lines: list[str] = []
    if title:
        lines += [f"<b>{e(title)}</b>", ""]

    [lines.append](https://lines.append)("📊 <b>گزارش وضعیت استراتژی مومنتوم ۱۰ روزه</b>")
    [lines.append](https://lines.append)("")
    [lines.append](https://lines.append)(f"💰 <b>ارزش کل پورتفو:</b> <code>{total:.6f} BTC</code> ({pnl:+.2f}% از شروع)")
    if unknown:
        [lines.append](https://lines.append)(f"⚠️ قیمت {e(', '.join(unknown))} دریافت نشد؛ ارزش کل ناقص است.")
    [lines.append](https://lines.append)(f"💵 <b>بیت‌کوین نقد:</b> <code>{cash:.6f} BTC</code> ({pct(cash, total):.1f}%)")
    [lines.append](https://lines.append)(f"🕒 <b>آخرین ریبالانس:</b> {fmt_ts(last_ts)}")
    if last_ts:
        [lines.append](https://lines.append)(f"⏭ <b>ریبالانس خودکار بعدی:</b> {fmt_ts(last_ts + REBALANCE_INTERVAL_SECONDS)}")

    lines += ["", "📈 <b>سبد آلت‌کوین‌ها:</b>"]
    held = [(p, q) for p, q in state["holdings"].items() if q > 0]
    if not held:
        [lines.append](https://lines.append)("▫️ <i>تمام سبد در حال حاضر بیت‌کوین نقد است.</i>")
    for pair, qty in held:
        if pair in prices:
            val = qty * prices[pair]
            [lines.append](https://lines.append)(
                f"▫️ <code>{e(pair)}</code>: {qty:,.4f} واحد "
                f"(<code>{val:.6f} BTC</code> | {pct(val, total):.1f}%)"
            )
        else:
            [lines.append](https://lines.append)(f"▫️ <code>{e(pair)}</code>: {qty:,.4f} واحد (قیمت نامشخص)")

    lines += ["", "🔍 <b>رتبه‌بندی ۱۰ روز اخیر:</b>"]
    target_set = set(targets or [])
    ranked = sorted(
        scores, key=lambda p: (scores[p]["eligible"], scores[p]["score"]), reverse=True
    )
    for pair in ranked:
        sc = scores[pair]
        icon = "✅" if pair in target_set else ("▫️" if sc["eligible"] else "⛔️")
        score_txt = f"{sc['score']:+.2f}" if sc["eligible"] else "—"
        [lines.append](https://lines.append)(
            f"{icon} <code>{e(pair):<8}</code> | بازده: <code>{sc['return_pct']:+.2f}%</code>"
            f" | R²: <code>{sc['r2']:.2f}</code> | امتیاز: <code>{score_txt}</code>"
        )

    if failed:
        lines += ["", f"⚠️ <b>دریافت داده ناموفق:</b> {e(', '.join(sorted(failed)))}"]

    if logs is not None:
        lines += ["", "📝 <b>تراکنش‌های ریبالانس:</b>"]
        lines += [e(line) for line in logs] if logs else ["بدون معامله؛ سبد در وزن هدف بود."]

    return "\n".join(lines)

def format_history(trades: list[dict]) -> str:
    if not trades:
        return "هنوز معامله‌ای ثبت نشده است."
    lines = [f"🧾 <b>آخرین {len(trades)} معامله:</b>", ""]
    for t in reversed(trades):
        side = "🟢 خرید" if t["side"] == "BUY" else "🔴 فروش"
        [lines.append](https://lines.append)(
            f"{fmt_ts(t['ts'])} | {side} <code>{html.escape(t['pair'])}</code> | "
            f"{t['qty']:,.4f} @ {t['price']:.8f} | <code>{t['btc']:.6f} BTC</code>"
        )
    return "\n".join(lines)

# ─────────────────────────── دستورات تلگرام ───────────────────────────
async def start_command(update: Update, context: [ContextTypes.DEFAULT](https://ContextTypes.DEFAULT)_TYPE):
    await [update.effective_message.reply](https://update.effective_message.reply)_text(
        "ربات معامله‌گر مومنتوم ۱۰ روزه فعال است (حالت شبیه‌سازی).\n\n"
        "دستورات:\n"
        "/status - وضعیت فعلی پورتفو و رتبه‌بندی ارزها\n"
        "/rebalance - اجرای دستی ریبالانس (تایمر ۱۰ روزه را از نو شروع می‌کند)\n"
        "/history - آخرین معاملات"
    )

async def status_command(update: Update, context: [ContextTypes.DEFAULT](https://ContextTypes.DEFAULT)_TYPE):
    message = [update.effective](https://update.effective)_message
    await [message.reply](https://message.reply)_text("⏳ در حال دریافت داده‌های بازار و محاسبه‌ی امتیازها...")
    try:
        state = await [asyncio.to](https://asyncio.to)_thread(load_state)
        scores, prices, failed = await [asyncio.to](https://asyncio.to)_thread(fetch_and_score)
    except Exception as exc:
        [logger.exception](https://logger.exception)("خطا در /status")
        await [message.reply](https://message.reply)_text(f"❌ خطا در دریافت وضعیت: {exc}")
        return

    await [message.reply](https://message.reply)_text(
        format_status_message(state, scores, prices, failed=failed),
        parse_mode=ParseMode.HTML,
    )

async def rebalance_command(update: Update, context: [ContextTypes.DEFAULT](https://ContextTypes.DEFAULT)_TYPE):
    message = [update.effective](https://update.effective)_message
    await [message.reply](https://message.reply)_text("⏳ در حال اجرای ریبالانس...")
    try:
        new_state, scores, prices, failed, targets, logs = await run_rebalance(force=True)
    except Exception as exc:
        [logger.exception](https://logger.exception)("خطا در /rebalance")
        await [message.reply](https://message.reply)_text(f"❌ ریبالانس انجام نشد و وضعیت تغییری نکرد: {exc}")
        return

    await [message.reply](https://message.reply)_text(
        format_status_message(
            new_state, scores, prices, failed=failed, targets=targets, logs=logs,
            title="⚡ ریبالانس دستی اجرا شد",
        ),
        parse_mode=ParseMode.HTML,
    )

async def history_command(update: Update, context: [ContextTypes.DEFAULT](https://ContextTypes.DEFAULT)_TYPE):
    message = [update.effective](https://update.effective)_message
    try:
        state = await [asyncio.to](https://asyncio.to)_thread(load_state)
    except Exception as exc:
        [logger.exception](https://logger.exception)("خطا در /history")
        await [message.reply](https://message.reply)_text(f"❌ خطا در خواندن وضعیت: {exc}")
        return

    await [message.reply](https://message.reply)_text(
        format_history(state["trade_history"][-HISTORY_SHOW:]),
        parse_mode=ParseMode.HTML,
    )

# ─────────────────────────── زمان‌بند خودکار ───────────────────────────
async def scheduled_check(context: [ContextTypes.DEFAULT](https://ContextTypes.DEFAULT)_TYPE):
    chat_id = [context.job.chat](https://context.job.chat)_id
    try:
        result = await run_rebalance(force=False)
    except Exception as exc:
        [logger.exception](https://logger.exception)("ریبالانس خودکار ناموفق بود")
        # فقط در اولین خطای پیاپی اطلاع بده تا هر ساعت پیام تکراری نیاید
        if not [context.bot_data.get](https://context.bot_data.get)("scheduler_error_notified"):
            [context.bot](https://context.bot)_data["scheduler_error_notified"] = True
            with [contextlib.suppress](https://contextlib.suppress)(Exception):
                await [context.bot.send](https://context.bot.send)_message(
                    chat_id=chat_id,
                    text=f"⚠️ ریبالانس خودکار ناموفق بود و هر ساعت دوباره تلاش می‌شود:\n{exc}",
                )
        return

    [context.bot](https://context.bot)_data["scheduler_error_notified"] = False
    if result is None:
        return

    new_state, scores, prices, failed, targets, logs = result
    await [context.bot.send](https://context.bot.send)_message(
        chat_id=chat_id,
        text=format_status_message(
            new_state, scores, prices, failed=failed, targets=targets, logs=logs,
            title="🚨 ریبالانس خودکار سررسید ۱۰ روزه",
        ),
        parse_mode=ParseMode.HTML,
    )

async def error_handler(update: object, context: [ContextTypes.DEFAULT](https://ContextTypes.DEFAULT)_TYPE):
    [logger.error](https://logger.error)("خطای پیش‌بینی‌نشده", exc_info=context.error)

# ─────────────────────────── اجرا ───────────────────────────
def main():
    global _storage

    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("متغیر TELEGRAM_BOT_TOKEN تعریف نشده است.")
    if not TELEGRAM_CHAT_ID:
        raise SystemExit("متغیر TELEGRAM_CHAT_ID تعریف نشده است (برای کنترل دسترسی الزامی است).")
    try:
        chat_id = int(TELEGRAM_CHAT_ID)
    except ValueError:
        raise SystemExit("TELEGRAM_CHAT_ID باید یک عدد باشد.")

    _storage = build_storage()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    if [app.job](https://app.job)_queue is None:
        raise SystemExit("JobQueue فعال نیست. نصب کن: pip install 'python-telegram-bot[job-queue]'")

    # فقط چت مجاز می‌تواند دستورات را اجرا کند؛ پیام بقیه نادیده گرفته می‌شود
    allowed = [filters.Chat](https://filters.Chat)(chat_id=chat_id)
    [app.add](https://app.add)_handler(CommandHandler(["start", "help"], start_command, filters=allowed))
    [app.add](https://app.add)_handler(CommandHandler("status", status_command, filters=allowed))
    [app.add](https://app.add)_handler(CommandHandler("rebalance", rebalance_command, filters=allowed))
    [app.add](https://app.add)_handler(CommandHandler("history", history_command, filters=allowed))
    [app.add](https://app.add)_error_handler(error_handler)

    [app.job_queue.run](https://app.job_queue.run)_repeating(
        scheduled_check,
        interval=CHECK_INTERVAL_SECONDS,
        first=30,
        chat_id=chat_id,
        name="rebalance-check",
    )

    [logger.info](https://logger.info)("ربات آماده‌ی دریافت دستورات است.")
    [app.run](https://app.run)_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
