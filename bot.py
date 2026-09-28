"""
CryptoMomentumBot
ربات تلگرامی معامله‌ی شبیه‌سازی‌شده (Paper Trading) با استراتژی مومنتوم نسبت به BTC.

هیچ سفارش واقعی ثبت نمی‌کند و هیچ API صرافی با کلید خصوصی استفاده نمی‌شود.
تمام داده‌های بازار از اندپوینت‌های عمومی Binance خوانده می‌شوند.

متغیرهای محیطی:
    TELEGRAM_BOT_TOKEN      (الزامی) توکن ربات تلگرام
    TELEGRAM_CHAT_ID        (الزامی) شناسه‌ی عددی چتی که اجازه‌ی کار با ربات را دارد
    DATABASE_URL            (اختیاری) اگر تعریف شود، وضعیت در PostgreSQL ذخیره می‌شود
    STATE_FILE              (اختیاری) مسیر فایل وضعیت وقتی دیتابیس ندارید (پیش‌فرض: portfolio_state.json)
    BOT_TIMEZONE            (اختیاری) منطقه‌ی زمانی نمایش (پیش‌فرض: Asia/Tehran)
    LOOKBACK_HOURS           (اختیاری) پنجره‌ی محاسبه‌ی مومنتوم به ساعت (پیش‌فرض: 240)
    REBALANCE_INTERVAL_HOURS (اختیاری) فاصله‌ی ریبالانس خودکار به ساعت (پیش‌فرض: 240)
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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────── لاگ ───────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx در سطح INFO آدرس کامل درخواست‌ها را لاگ می‌کند که شامل توکن ربات است
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("CryptoMomentumBot")

# ─────────────────────────── تنظیمات ───────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
STATE_FILE = os.getenv("STATE_FILE", "portfolio_state.json")

# اعتبارسنجی TELEGRAM_CHAT_ID: باید عدد صحیح باشد وگرنه برنامه نباید بالا بیاید.
# این کار از نشت احتمالی توکن به چت‌های غیرمجاز جلوگیری می‌کند.
if not TELEGRAM_CHAT_ID:
    raise SystemExit(
        "متغیر TELEGRAM_CHAT_ID تعریف نشده است. "
        "این متغیر باید شناسه‌ی عددی چت مجاز باشد. اجرای ربات متوقف می‌شود."
    )
try:
    ALLOWED_CHAT_ID: int = int(TELEGRAM_CHAT_ID)
except (TypeError, ValueError):
    raise SystemExit(
        f"متغیر TELEGRAM_CHAT_ID مقدار '{TELEGRAM_CHAT_ID}' عدد نیست. "
        "این متغیر باید شناسه‌ی عددی چت مجاز باشد. اجرای ربات متوقف می‌شود."
    )

try:
    BOT_TIMEZONE = ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Tehran"))
except (ZoneInfoNotFoundError, ValueError):
    BOT_TIMEZONE = timezone.utc

PAIRS = ["ETHBTC", "BNBBTC", "SOLBTC", "XRPBTC", "DOGEBTC", "ADABTC", "LTCBTC"]

# پارامترهای استراتژی (نباید تغییر کنند)
TOP_N = 2                  # تعداد ارزهای منتخب
WEIGHT_PER_ASSET = 0.20    # وزن هر ارز؛ با 2×0.20، ۶۰٪ سبد BTC نقد می‌ماند
REQUIRE_BOTH_POSITIVE = False   # True = شرط ورود سخت‌گیرانه (بازده و روند هر دو مثبت)

# پارامترهای قابل تنظیم با حفظ مقدار پیش‌فرض فعلی
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "240"))
REBALANCE_INTERVAL_HOURS = int(os.getenv("REBALANCE_INTERVAL_HOURS", "240"))
CHECK_INTERVAL_SECONDS = 3600             # هر چند وقت سررسید بررسی شود
MIN_TRADE_FRACTION = 0.01                 # معاملات کوچک‌تر از ۱٪ ارزش سبد انجام نمی‌شوند
FEE = 0.00075
INITIAL_BTC = 1.0
MAX_TRADE_HISTORY = 500                   # حداکثر ۵۰۰ رکورد آخر نگه داشته شود
HISTORY_SHOW = 10                         # تعداد معاملات نمایش‌داده‌شده در /history

REQUEST_TIMEOUT = 10
FETCH_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
KLINE_ENDPOINTS = (
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
)

REBALANCE_INTERVAL_SECONDS = REBALANCE_INTERVAL_HOURS * 3600

if TOP_N * WEIGHT_PER_ASSET > 1.0:
    raise ValueError("TOP_N × WEIGHT_PER_ASSET نباید از ۱ بیشتر باشد.")


# ─────────────────────────── خطاهای سفارشی ───────────────────────────
class StateError(RuntimeError):
    """خطای مربوط به وضعیت ذخیره‌شده (فایل خراب، رکورد نامعتبر، و غیره)."""


class MarketDataError(RuntimeError):
    """خطای مربوط به دریافت داده‌های بازار."""


# ─────────────────────────── ذخیره‌سازی ───────────────────────────
class FileStorage:
    """ذخیره در فایل JSON با نوشتن اتمیک (tmp + fsync + os.replace)."""

    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict | None:
        if not os.path.exists(self.path):
            return None
        # اگر فایل خراب باشد خطا می‌دهیم؛ عمداً سبد را بی‌صدا ریست نمی‌کنیم.
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise StateError(f"فایل وضعیت خراب است و قابل خواندن نیست: {self.path} ({exc})") from exc

    def save(self, state: dict) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp_path)
            raise


class PostgresStorage:
    """ذخیره در PostgreSQL (مناسب Heroku و سرویس‌هایی با فایل‌سیستم موقتی).

    از psycopg2 استفاده می‌کند. جدول bot_state به‌صورت خودکار ساخته می‌شود و
    رکورد با id=1 به‌صورت upsert ذخیره می‌شود.
    """

    def __init__(self, dsn: str):
        import psycopg2
        from psycopg2.extras import Json

        self._psycopg2 = psycopg2
        self._Json = Json
        self._dsn = dsn
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bot_state (
                        id         INTEGER PRIMARY KEY,
                        data       JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
            conn.commit()

    def _connect(self):
        return self._psycopg2.connect(self._dsn, connect_timeout=10)

    def load(self) -> dict | None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT data FROM bot_state WHERE id = 1")
                    row = cur.fetchone()
            return row[0] if row else None
        except self._psycopg2.Error as exc:
            raise StateError(f"خطا در خواندن وضعیت از PostgreSQL: {exc}") from exc

    def save(self, state: dict) -> None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO bot_state (id, data, updated_at) VALUES (1, %s, now())
                        ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()
                        """,
                        (self._Json(state),),
                    )
                conn.commit()
        except self._psycopg2.Error as exc:
            raise StateError(f"خطا در ذخیره‌ی وضعیت در PostgreSQL: {exc}") from exc


def build_storage() -> FileStorage | PostgresStorage:
    if DATABASE_URL:
        logger.info("وضعیت در PostgreSQL ذخیره می‌شود.")
        return PostgresStorage(DATABASE_URL)
    logger.warning(
        "DATABASE_URL تعریف نشده؛ وضعیت در فایل %s ذخیره می‌شود. "
        "روی Heroku این فایل با هر ری‌استارت پاک می‌شود.",
        STATE_FILE,
    )
    return FileStorage(STATE_FILE)


_storage: FileStorage | PostgresStorage | None = None


def get_storage() -> FileStorage | PostgresStorage:
    global _storage
    if _storage is None:
        _storage = build_storage()
    return _storage


# ─────────────────────────── وضعیت ───────────────────────────
def default_state() -> dict:
    return {
        "btc_cash": INITIAL_BTC,
        "holdings": {p: 0.0 for p in PAIRS},
        "last_rebalance_ts": 0,
        "trade_history": [],
    }


def normalize_state(raw: dict | None) -> dict:
    """اعتبارسنجی و نرمالایز کردن وضعیت بارگذاری‌شده.

    - اگر raw برابر None باشد، سبد پیش‌فرض ساخته می‌شود.
    - جفت‌ارزهایی که در holdings نیستند با مقدار 0 اضافه می‌شوند.
    - اگر برای جفت‌ارز خارج از PAIRS دارایی بیشتر از صفر ثبت شده باشد، StateError بالا می‌آید.
    - اگر فایل/رکورد خراب باشد، StateError بالا می‌آید (ریست بی‌صدا ممنوع است).
    """
    if raw is None:
        return default_state()
    if not isinstance(raw, dict):
        raise StateError("ساختار وضعیت ذخیره‌شده نامعتبر است (آبجکت JSON نیست).")

    try:
        btc_cash = float(raw.get("btc_cash", INITIAL_BTC))
        last_rebalance_ts = int(raw.get("last_rebalance_ts", 0))
    except (TypeError, ValueError) as exc:
        raise StateError(f"مقادیر عددی وضعیت نامعتبر هستند: {exc}") from exc

    raw_holdings = raw.get("holdings") or {}
    if not isinstance(raw_holdings, dict):
        raise StateError("فیلد holdings در وضعیت ذخیره‌شده نامعتبر است.")

    holdings = {p: 0.0 for p in PAIRS}
    for pair, qty in raw_holdings.items():
        try:
            qty_float = float(qty)
        except (TypeError, ValueError) as exc:
            raise StateError(f"مقدار دارایی برای {pair} عدد نیست: {exc}") from exc
        # جفت‌ارز خارج از PAIRS با مقدار مثبت → خطا
        if pair not in PAIRS and qty_float > 0:
            raise StateError(
                f"دارایی مثبت برای جفت‌ارز ناشناخته '{pair}' در وضعیت ثبت شده است. "
                "امکان ادامه‌ی امن وجود ندارد."
            )
        # جفت‌ارزهای داخل PAIRS یا جفت‌ارزهای خارج از PAIRS با مقدار صفر حفظ می‌شوند.
        holdings[pair] = qty_float

    raw_history = raw.get("trade_history") or []
    if not isinstance(raw_history, list):
        raise StateError("فیلد trade_history در وضعیت ذخیره‌شده لیست نیست.")
    trade_history = list(raw_history)[-MAX_TRADE_HISTORY:]

    return {
        "btc_cash": btc_cash,
        "holdings": holdings,
        "last_rebalance_ts": last_rebalance_ts,
        "trade_history": trade_history,
    }


def load_state() -> dict:
    return normalize_state(get_storage().load())


def save_state(state: dict) -> None:
    get_storage().save(state)


def portfolio_value(state: dict, prices: dict) -> tuple[float, list[str]]:
    total = float(state["btc_cash"])
    unknown = []
    for pair, qty in state["holdings"].items():
        if qty <= 0:
            continue
        if pair in prices:
            total += qty * prices[pair]
        else:
            unknown.append(pair)
    return total, unknown


def is_rebalance_due(state: dict, now: int | None = None) -> bool:
    now = now if now is not None else int(time.time())
    last_ts = state.get("last_rebalance_ts", 0)
    return last_ts == 0 or (now - last_ts) >= REBALANCE_INTERVAL_SECONDS


# ─────────────────────────── داده‌ی بازار ───────────────────────────
def fetch_pair_data(symbol: str) -> tuple[np.ndarray, float]:
    """خروجی: (قیمت‌های بسته‌شدن آخرین LOOKBACK_HOURS کندلِ بسته‌شده، آخرین قیمت لحظه‌ای).

    کندل جاری (بسته‌نشده) در محاسبه‌ی امتیاز استفاده نمی‌شود، فقط به‌عنوان قیمت اجرا.
    """
    params = {"symbol": symbol, "interval": "1h", "limit": LOOKBACK_HOURS + 5}
    last_error: Exception | None = None

    for attempt in range(FETCH_RETRIES):
        for url in KLINE_ENDPOINTS:
            try:
                res = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
                res.raise_for_status()
                data = res.json()
                if not isinstance(data, list) or not data:
                    raise ValueError(f"پاسخ غیرمنتظره: {str(data)[:120]}")

                now_ms = int(time.time() * 1000)
                closed = [k for k in data if int(k[6]) < now_ms]
                if len(closed) < LOOKBACK_HOURS:
                    raise ValueError(
                        f"فقط {len(closed)} کندل بسته‌شده موجود است (حداقل {LOOKBACK_HOURS} لازم است)"
                    )

                closes = np.array(
                    [float(k[4]) for k in closed[-LOOKBACK_HOURS:]], dtype=np.float64
                )
                # آخرین کندل (احتمالاً باز) فقط برای قیمت لحظه‌ای استفاده می‌شود
                last_price = float(data[-1][4])
                if (
                    not np.all(np.isfinite(closes))
                    or np.any(closes <= 0)
                    or last_price <= 0
                ):
                    raise ValueError("قیمت نامعتبر در داده‌ها")
                return closes, last_price

            except (requests.RequestException, ValueError, TypeError, IndexError) as exc:
                last_error = exc
                logger.warning(
                    "دریافت %s از %s ناموفق بود (تلاش %d/%d): %s",
                    symbol, url, attempt + 1, FETCH_RETRIES, exc,
                )

        if attempt < FETCH_RETRIES - 1:
            # backoff: 2s, 4s
            backoff = RETRY_BACKOFF_SECONDS * (2 ** attempt)
            time.sleep(backoff)

    raise MarketDataError(
        f"دریافت داده‌های بازار برای {symbol} پس از {FETCH_RETRIES} تلاش ناموفق بود: {last_error}"
    )


# ─────────────────────────── استراتژی ───────────────────────────
def compute_hybrid_scores() -> tuple[dict, dict]:
    """محاسبه‌ی امتیاز هیبریدی برای همه‌ی جفت‌ارزها.

    فرمول امتیاز (دقیقاً حفظ شده):
        ret = (c_now / c_old) - 1
        slope, r2 = رگرسیون خطی روی y = window / c_old
        reg_score = slope * r2
        z_ret = zscore(rets)
        z_reg = zscore(reg_scores)
        blend = 0.5 * z_ret + 0.5 * z_reg
        eligible = (ret > 0) OR (reg_score > 0)

    خروجی:
        scores[p] = {score, return_10d, trend_score, eligible, price}
        prices[p] = آخرین قیمت لحظه‌ای
    """
    rets = {}
    reg_scores = {}
    current_prices = {}

    x = np.arange(LOOKBACK_HOURS, dtype=np.float64)
    x_diff = x - np.mean(x)
    x_var = np.sum(x_diff ** 2)

    for p in PAIRS:
        closes, last_price = fetch_pair_data(p)
        window = closes[-LOOKBACK_HOURS:]
        c_now = float(window[-1])
        c_old = float(window[0])
        current_prices[p] = last_price  # قیمت لحظه‌ای برای اجرا

        # بازدهی ۱۰ روزه
        ret = (c_now / c_old) - 1.0
        rets[p] = ret

        # شیب رگرسیون پیوسته
        y = window / c_old
        y_diff = y - np.mean(y)
        slope = np.sum(x_diff * y_diff) / x_var
        ss_tot = np.sum(y_diff ** 2)
        r2 = (float(np.sum(x_diff * y_diff)) ** 2) / (x_var * ss_tot) if ss_tot > 0 else 0.0
        reg_scores[p] = float(slope * r2)

    r_arr = np.array(list(rets.values()), dtype=np.float64)
    s_arr = np.array(list(reg_scores.values()), dtype=np.float64)

    r_std = float(np.std(r_arr))
    s_std = float(np.std(s_arr))
    z_ret = (r_arr - np.mean(r_arr)) / (r_std + 1e-6)
    z_reg = (s_arr - np.mean(s_arr)) / (s_std + 1e-6)

    blend = 0.5 * z_ret + 0.5 * z_reg
    final_scores = {}
    for idx, p in enumerate(PAIRS):
        is_eligible = (rets[p] > 0) or (reg_scores[p] > 0)
        final_scores[p] = {
            "score": float(blend[idx]),
            "return_10d": float(rets[p] * 100),
            "trend_score": float(reg_scores[p]),
            "eligible": bool(is_eligible),
            "price": float(current_prices[p]),
        }

    return final_scores, current_prices


def execute_rebalance(
    state: dict,
    scores: dict,
    prices: dict,
    reason: str = "scheduled",
) -> tuple[list[str], list[str], float]:
    """اجرای یک چرخه‌ی ریبالانس روی یک کپی از state.

    مراحل (طبق مشخصات):
        (الف) فروش کامل ارزهایی که دیگر هدف نیستند.
        (ب) محاسبه‌ی target_val بر اساس ارزش سبد بعد از فروش‌ها.
        (ج) کاهش وزن ارزهای بیش‌وزن (کاهش وزن، نه سیو سود).
        (د) خرید با min(deficit, btc_cash).

    معاملات کوچک‌تر از MIN_TRADE_FRACTION × ارزش سبد انجام نمی‌شوند.
    btc_cash هیچ‌وقت منفی نمی‌شود.
    """
    # کار روی یک کپی از state؛ فقط در صورت موفقیت در خروجی ذخیره می‌شود.
    new_state = {
        "btc_cash": float(state.get("btc_cash", INITIAL_BTC)),
        "holdings": dict(state.get("holdings") or {}),
        "last_rebalance_ts": int(state.get("last_rebalance_ts", 0)),
        "trade_history": list(state.get("trade_history") or []),
    }
    # اطمینان از وجود همه‌ی PAIRS در holdings
    for p in PAIRS:
        new_state["holdings"].setdefault(p, 0.0)

    btc_cash = new_state["btc_cash"]
    holdings = new_state["holdings"]
    history = new_state["trade_history"]
    logs: list[str] = []

    # انتخاب هدف‌ها: از بین TOP_N رتبه‌ی اول فقط eligible ها
    sorted_pairs = sorted(scores.keys(), key=lambda p: scores[p]["score"], reverse=True)
    targets = [p for p in sorted_pairs[:TOP_N] if scores[p].get("eligible", False)]

    # محاسبه‌ی ارزش فعلی سبد (قبل از هر معامله) برای آستانه‌ی معامله
    def _portfolio_value_after() -> float:
        return btc_cash + sum(holdings.get(p, 0.0) * prices[p] for p in PAIRS)

    min_trade_value = MIN_TRADE_FRACTION * _portfolio_value_after()

    def _record(side: str, pair: str, qty: float, price: float, gross_btc_override: float | None = None) -> None:
        """ثبت یک معامله در trade_history.

        برای buy: gross_btc = مقدار BTC خرج‌شده (spend)، fee_btc = spend * FEE.
        برای sell/trim: gross_btc = qty * price، fee_btc = gross * FEE.
        """
        gross_btc = gross_btc_override if gross_btc_override is not None else qty * price
        fee_btc = gross_btc * FEE
        history.append({
            "ts": int(time.time()),
            "reason": reason,
            "side": side,
            "pair": pair,
            "qty": float(qty),
            "price": float(price),
            "gross_btc": float(gross_btc),
            "fee_btc": float(fee_btc),
        })
        # نگه‌داشتن حداکثر MAX_TRADE_HISTORY رکورد آخر
        if len(history) > MAX_TRADE_HISTORY:
            del history[: len(history) - MAX_TRADE_HISTORY]

    # (الف) فروش کامل ارزهای غیر هدف
    for p in PAIRS:
        qty = holdings.get(p, 0.0)
        if p not in targets and qty > 0:
            sold_value = qty * prices[p]
            if sold_value < min_trade_value:
                # معامله‌ی کوچک‌تر از آستانه انجام نمی‌شود؛ دارایی حفظ می‌شود
                logs.append(f"⚪ حفظ {p} (مقدار فروش زیر آستانه)")
                continue
            proceeds = sold_value * (1 - FEE)
            btc_cash += proceeds
            holdings[p] = 0.0
            _record("sell", p, qty, prices[p])
            logs.append(f"🔴 فروش کامل {p} → {proceeds:.6f} BTC")

    # (ب) محاسبه‌ی target_val بر اساس ارزش سبد بعد از فروش‌ها
    total_val = btc_cash + sum(holdings.get(p, 0.0) * prices[p] for p in PAIRS)
    target_val = total_val * WEIGHT_PER_ASSET
    min_trade_value = MIN_TRADE_FRACTION * total_val

    # (ج) کاهش وزن ارزهای هدف بیش‌وزن (کاهش وزن)
    for p in targets:
        curr_val = holdings.get(p, 0.0) * prices[p]
        excess_val = curr_val - target_val
        if excess_val > min_trade_value:
            excess_qty = excess_val / prices[p]
            excess_qty = min(excess_qty, holdings.get(p, 0.0))
            if excess_qty <= 0:
                continue
            sold_value = excess_qty * prices[p]
            proceeds = sold_value * (1 - FEE)
            btc_cash += proceeds
            holdings[p] = holdings.get(p, 0.0) - excess_qty
            _record("trim", p, excess_qty, prices[p])
            logs.append(f"🟡 کاهش وزن {p} → {proceeds:.6f} BTC")

    # (د) خرید با min(deficit, btc_cash)
    for p in targets:
        curr_val = holdings.get(p, 0.0) * prices[p]
        deficit = target_val - curr_val
        if deficit <= min_trade_value:
            continue
        spend = min(deficit, max(btc_cash, 0.0))
        if spend <= 0:
            continue
        bought_qty = (spend / prices[p]) * (1 - FEE)
        holdings[p] = holdings.get(p, 0.0) + bought_qty
        btc_cash -= spend
        btc_cash = max(btc_cash, 0.0)  # هرگز منفی نشود
        # برای buy: gross_btc = spend (مقدار خرج‌شده) و fee_btc = spend * FEE
        _record("buy", p, bought_qty, prices[p], gross_btc_override=spend)
        logs.append(f"🟢 خرید {p} → {spend:.6f} BTC")

    new_state["btc_cash"] = btc_cash
    new_state["last_rebalance_ts"] = int(time.time())

    # محاسبه‌ی ارزش نهایی
    new_total = btc_cash + sum(holdings.get(p, 0.0) * prices[p] for p in PAIRS)

    return targets, logs, new_total, new_state


def format_status_message(
    state: dict,
    scores: dict,
    prices: dict,
    targets: list[str] | None = None,
    logs: list[str] | None = None,
) -> str:
    total_val, _unknown = portfolio_value(state, prices)
    targets = targets or []

    msg = "📊 <b>گزارش وضعیت استراتژی هیبریدی ۱۰ روزه</b>\n\n"
    msg += f"💰 <b>ارزش کل پورتفو:</b> <code>{total_val:.4f} BTC</code>\n"
    cash_pct = (state["btc_cash"] / total_val * 100) if total_val > 0 else 0.0
    msg += f"💵 <b>موجودی نقد بیت‌کوین:</b> <code>{state['btc_cash']:.4f} BTC</code> ({cash_pct:.1f}%)\n"

    # زمان آخرین ریبالانس و ریبالانس بعدی به وقت BOT_TIMEZONE
    last_ts = state.get("last_rebalance_ts", 0)
    if last_ts > 0:
        last_dt = datetime.fromtimestamp(last_ts, tz=BOT_TIMEZONE)
        msg += f"🕒 <b>آخرین ریبالانس:</b> {html.escape(last_dt.strftime('%Y-%m-%d %H:%M %Z'))}\n"
        next_ts = last_ts + REBALANCE_INTERVAL_SECONDS
        next_dt = datetime.fromtimestamp(next_ts, tz=BOT_TIMEZONE)
        msg += f"🕒 <b>ریبالانس بعدی:</b> {html.escape(next_dt.strftime('%Y-%m-%d %H:%M %Z'))}\n"
    else:
        msg += "🕒 <b>آخرین ریبالانس:</b> هنوز انجام نشده (ریبالانس اولیه سررسید است)\n"

    msg += "\n📈 <b>سبد آلت‌کوین‌ها:</b>\n"
    has_alts = False
    for p in PAIRS:
        amt = state["holdings"].get(p, 0.0)
        if amt > 0:
            has_alts = True
            val = amt * prices[p]
            pct = (val / total_val * 100) if total_val > 0 else 0.0
            msg += f"▫️ <code>{html.escape(p)}</code>: {amt:.4f} واحد (<code>{val:.4f} BTC</code> | {pct:.1f}%)\n"
    if not has_alts:
        msg += "▫️ تمام سبد در حال حاضر بیت‌کوین نقد است.\n"

    msg += "\n🔍 <b>رتبه‌بندی ۱۰ روز اخیر:</b>\n"
    sorted_p = sorted(scores.keys(), key=lambda x: scores[x]["score"], reverse=True)
    for p in sorted_p:
        sc = scores[p]
        status = "✅" if p in targets else "▫️"
        eligible_marker = "" if sc.get("eligible") else " (غیرواجد)"
        msg += (
            f"{status} <code>{html.escape(p):&lt;7}</code> | "
            f"بازدهی: <code>{sc['return_10d']:>+5.1f}%</code> | "
            f"امتیاز: <code>{sc['score']:>+4.2f}</code>{eligible_marker}\n"
        )

    if logs:
        msg += "\n📝 <b>تراکنش‌های ریبالانس:</b>\n" + "\n".join(logs)

    return msg


# ─────────────────────────── دستورات تلگرام ───────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ربات معامله‌گر هیبریدی ۱۰ روزه فعال است.\n\n"
        "دستورات:\n"
        "/status - نمایش وضعیت فعلی پورتفو و رتبه‌بندی ارزها\n"
        "/rebalance - اجرای دستی چرخه ریبالانس همین لحظه\n"
        "/history - نمایش ۱۰ معامله‌ی آخر"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("در حال دریافت داده‌های بازار و محاسبه Z-Score...")
    try:
        state = await asyncio.to_thread(load_state)
        scores, prices = await asyncio.to_thread(compute_hybrid_scores)
        msg = format_status_message(state, scores, prices)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("خطا در /status")
        await update.message.reply_text(f"خطا در پردازش: {html.escape(str(exc))}")


async def manual_rebalance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # قفل ریبالانس: اگر در حال اجراست، پیام بده
    lock: asyncio.Lock | None = context.application.bot_data.get("rebalance_lock")
    if lock is None:
        lock = asyncio.Lock()
        context.application.bot_data["rebalance_lock"] = lock
    if lock.locked():
        await update.message.reply_text("⛔ ریبالانس دیگری در حال اجراست. لطفاً چند لحظه بعد تلاش کنید.")
        return

    async with lock:
        await update.message.reply_text("در حال اجرای ریبالانس...")
        try:
            state = await asyncio.to_thread(load_state)
            scores, prices = await asyncio.to_thread(compute_hybrid_scores)
            targets, logs, _new_total, new_state = await asyncio.to_thread(
                execute_rebalance, state, scores, prices, "manual"
            )
            # ذخیره‌ی فقط در صورت موفقیت
            await asyncio.to_thread(save_state, new_state)
            msg = format_status_message(new_state, scores, prices, targets=targets, logs=logs)
            await update.message.reply_text(
                f"⚡ <b>ریبالانس دستی اجرا شد:</b>\n\n{msg}",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("خطا در /rebalance")
            await update.message.reply_text(f"خطا در ریبالانس: {html.escape(str(exc))}")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        state = await asyncio.to_thread(load_state)
        history = state.get("trade_history") or []
        if not history:
            await update.message.reply_text("📭 هنوز هیچ معامله‌ای ثبت نشده است.")
            return
        last = history[-HISTORY_SHOW:]
        last.reverse()  # آخرین معامله اول نمایش داده شود
        lines = ["📜 <b>۱۰ معامله‌ی آخر:</b>\n"]
        for i, t in enumerate(last, start=1):
            ts = t.get("ts", 0)
            dt = datetime.fromtimestamp(ts, tz=BOT_TIMEZONE) if ts else None
            ts_str = dt.strftime("%Y-%m-%d %H:%M") if dt else "—"
            side = t.get("side", "?")
            pair = html.escape(str(t.get("pair", "?")))
            qty = float(t.get("qty", 0.0))
            price = float(t.get("price", 0.0))
            gross = float(t.get("gross_btc", 0.0))
            fee = float(t.get("fee_btc", 0.0))
            reason = html.escape(str(t.get("reason", "?")))
            side_emoji = {"buy": "🟢", "sell": "🔴", "trim": "🟡"}.get(side, "⚪")
            lines.append(
                f"{i}. {side_emoji} <code>{pair}</code> | {side} | {qty:.6f} @ {price:.8f} BTC"
                f" | gross={gross:.6f} BTC | fee={fee:.6f} BTC | {reason} | {ts_str}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("خطا در /history")
        await update.message.reply_text(f"خطا در نمایش تاریخچه: {html.escape(str(exc))}")


# تابع بازبینی خودکار متصل به JobQueue استاندارد تلگرام
async def check_scheduler_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    lock: asyncio.Lock | None = context.application.bot_data.get("rebalance_lock")
    if lock is None:
        lock = asyncio.Lock()
        context.application.bot_data["rebalance_lock"] = lock
    if lock.locked():
        logger.info("ریبالانس خودکار به‌خاطر قفل رد شد (ریبالانس دیگری در حال اجراست).")
        return

    async with lock:
        try:
            state = await asyncio.to_thread(load_state)
            # بررسی سررسید داخل قفل
            if not is_rebalance_due(state):
                return
            logger.info("سررسید ریبالانس فرا رسید. شروع ریبالانس خودکار...")
            scores, prices = await asyncio.to_thread(compute_hybrid_scores)
            targets, logs, _new_total, new_state = await asyncio.to_thread(
                execute_rebalance, state, scores, prices, "scheduled"
            )
            await asyncio.to_thread(save_state, new_state)
            msg = format_status_message(new_state, scores, prices, targets=targets, logs=logs)
            if ALLOWED_CHAT_ID is not None:
                await context.bot.send_message(
                    chat_id=ALLOWED_CHAT_ID,
                    text=f"🚨 <b>اجرای سررسید دوره ۱۰ روزه (ریبالانس خودکار)</b>\n\n{msg}",
                    parse_mode=ParseMode.HTML,
                )
        except Exception as exc:
            logger.exception("خطا در جاب دوره‌ای")


async def post_init(app) -> None:
    """ساخت قفل ریبالانس پس از مقداردهی اولیه‌ی Application."""
    app.bot_data.setdefault("rebalance_lock", asyncio.Lock())


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """هندلر سراسری خطا برای جلوگیری از کرش ربات."""
    logger.error("خطای پیش‌بینی‌نشده در ربات: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        with contextlib.suppress(Exception):
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ خطایی در پردازش درخواست رخ داد. لطفاً دوباره تلاش کنید.",
            )


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("متغیر TELEGRAM_BOT_TOKEN تعریف نشده است. اجرای ربات متوقف می‌شود.")
    if ALLOWED_CHAT_ID is None:
        raise SystemExit(
            "متغیر TELEGRAM_CHAT_ID تعریف نشده یا عدد نیست. اجرای ربات متوقف می‌شود."
        )

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    chat_filter = filters.Chat(chat_id=ALLOWED_CHAT_ID)
    app.add_handler(CommandHandler("start", start_command, filters=chat_filter))
    app.add_handler(CommandHandler("status", status_command, filters=chat_filter))
    app.add_handler(CommandHandler("rebalance", manual_rebalance_command, filters=chat_filter))
    app.add_handler(CommandHandler("history", history_command, filters=chat_filter))

    app.add_error_handler(global_error_handler)

    # زمان‌بندی با JobQueue استاندارد تلگرام؛ بدون حلقه‌ی while یا create_task
    if app.job_queue is not None:
        app.job_queue.run_repeating(
            check_scheduler_job,
            interval=CHECK_INTERVAL_SECONDS,
            first=10,
        )
        logger.info("JobQueue زمان‌بندی شد (هر %d ثانیه بررسی سررسید).", CHECK_INTERVAL_SECONDS)
    else:
        logger.warning("JobQueue در دسترس نیست؛ ریبالانس خودکار غیرفعال می‌شود.")

    logger.info("ربات با موفقیت استارت خورد.")
    app.run_polling()


if __name__ == "__main__":
    main()
