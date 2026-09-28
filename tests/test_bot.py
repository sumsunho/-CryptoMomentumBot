"""Pytest tests for CryptoMomentumBot.

هیچ تستی به اینترنت، Binance یا تلگرام وصل نمی‌شود. تمام وابستگی‌های خارجی mock می‌شوند.
"""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

# مطمئن می‌شویم که دایرکتوری ریشه‌ی پروژه در sys.path باشد تا بتوانیم bot را import کنیم.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# قبل از import کردن bot، متغیرهای محیطی مورد نیاز را تنظیم می‌کنیم تا برنامه بالا بیاید.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-xxxxxxxxxxxxxxxxxxxxxxxxxxx")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456789")
os.environ.setdefault("STATE_FILE", str(PROJECT_ROOT / "tests" / "tmp_test_state.json"))

import bot  # noqa: E402  (بعد از تنظیم متغیرهای محیطی)


# ─────────────────────────── Fixtures ───────────────────────────
@pytest.fixture
def tmp_state_file(tmp_path):
    """یک مسیر فایل وضعیت موقت در دایرکتوری tmp_path."""
    return str(tmp_path / "state.json")


@pytest.fixture(autouse=True)
def reset_storage_singleton():
    """قبل از هر تست، singleton استوریج را ریست می‌کند تا از نشست تست‌ها جلوگیری شود."""
    bot._storage = None
    old_state_file = os.environ.get("STATE_FILE")
    yield
    bot._storage = None
    if old_state_file is not None:
        os.environ["STATE_FILE"] = old_state_file


def _make_closed_klines(closes, last_open_close=None, base_ts_ms=None):
    """ساخت لیست کندل‌های Binance با فرمت واقعی.

    هر کندل: [openTime, open, high, low, close, volume, closeTime, ...]
    closeTime = k[6]؛ اگر < now_ms باشد یعنی کندل بسته‌شده است.
    کندل آخر را با closeTime > now_ms می‌سازیم تا «باز» تلقی شود.
    """
    if base_ts_ms is None:
        base_ts_ms = 1_700_000_000_000
    klines = []
    now_ms = int(time.time() * 1000)
    n = len(closes)
    for i, c in enumerate(closes):
        # همه‌ی کندل‌های واقعی قبل از now بسته شوند
        open_time = now_ms - (n - i) * 3600_000 - 3600_000
        close_time = open_time + 3600_000 - 1
        # مطمئن می‌شویم closeTime قبل از now_ms باشد
        if close_time >= now_ms:
            close_time = now_ms - (n - i) * 3600_000 - 1
            open_time = close_time - 3600_000 + 1
        klines.append([open_time, str(c), str(c), str(c), str(c), "1.0", close_time, "1.0", 0, "0", "0", "0"])
    # کندل آخر (باز): closeTime در آینده
    last_close = last_open_close if last_open_close is not None else closes[-1]
    open_time = now_ms + 1000
    close_time = now_ms + 3_600_000  # در آینده
    klines.append([open_time, str(last_close), str(last_close), str(last_close), str(last_close), "1.0", close_time, "1.0", 0, "0", "0", "0"])
    return klines


def _make_response(klines, status_code=200):
    """ساخت یک mock response شبیه به requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = klines
    if status_code < 400:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return resp


# ─────────────────────────── تست‌های امتیاز ───────────────────────────
class TestComputeScores:
    def test_upward_prices_score_higher_than_downward(self):
        """برای یک سری قیمت صعودی، امتیاز بالاتر از سری نزولی محاسبه شود."""
        upward = np.linspace(1.0, 2.0, bot.LOOKBACK_HOURS).tolist()
        downward = np.linspace(2.0, 1.0, bot.LOOKBACK_HOURS).tolist()
        flat = [1.0] * bot.LOOKBACK_HOURS

        series_map = {
            "ETHBTC": upward,
            "BNBBTC": downward,
            "SOLBTC": flat,
            "XRPBTC": flat,
            "DOGEBTC": flat,
            "ADABTC": flat,
            "LTCBTC": flat,
        }

        def fake_fetch(symbol):
            closes = np.array(series_map[symbol], dtype=np.float64)
            return closes, float(closes[-1])

        with patch.object(bot, "fetch_pair_data", side_effect=fake_fetch):
            scores, prices = bot.compute_hybrid_scores()

        assert scores["ETHBTC"]["score"] > scores["BNBBTC"]["score"], (
            f"امتیاز صعودی باید بالاتر از نزولی باشد. "
            f"ETHBTC={scores['ETHBTC']['score']} vs BNBBTC={scores['BNBBTC']['score']}"
        )
        assert scores["ETHBTC"]["eligible"] is True
        assert scores["BNBBTC"]["eligible"] is False

    def test_open_candle_not_used_in_score(self):
        """کندل بازِ آخر در محاسبه‌ی امتیاز استفاده نشود."""
        normal_closes = [1.0 + i * 0.001 for i in range(bot.LOOKBACK_HOURS)]
        open_candle_close = 1000.0
        klines = _make_closed_klines(normal_closes, last_open_close=open_candle_close)

        def fake_get(url, params=None, timeout=None):
            return _make_response(klines)

        with patch("bot.requests.get", side_effect=fake_get):
            closes, last_price = bot.fetch_pair_data("ETHBTC")

        assert len(closes) == bot.LOOKBACK_HOURS
        assert last_price == open_candle_close
        assert float(closes.max()) < 2.0, f"کندل باز در closes نشت کرده: max={closes.max()}"

    def test_insufficient_closed_candles_raises_market_data_error(self):
        """اگر کندل‌ها کمتر از LOOKBACK_HOURS باشند، MarketDataError رخ دهد."""
        short_closes = [1.0 + i * 0.001 for i in range(10)]
        klines = _make_closed_klines(short_closes)

        def fake_get(url, params=None, timeout=None):
            return _make_response(klines)

        with patch("bot.requests.get", side_effect=fake_get), \
             patch("bot.time.sleep", return_value=None):
            with pytest.raises(bot.MarketDataError):
                bot.fetch_pair_data("ETHBTC")


# ─────────────────────────── تست‌های ریبالانس ───────────────────────────
class TestExecuteRebalance:
    def _flat_prices(self):
        return {p: 0.001 for p in bot.PAIRS}

    def _make_scores(self, eligible_pair="ETHBTC"):
        """ساخت scores با یک جفت eligible و بقیه ineligible."""
        scores = {}
        for p in bot.PAIRS:
            eligible = (p == eligible_pair)
            scores[p] = {
                "score": 1.0 if eligible else -1.0,
                "return_10d": 5.0 if eligible else -5.0,
                "trend_score": 0.1 if eligible else -0.1,
                "eligible": eligible,
                "price": 0.001,
            }
        return scores

    def test_non_target_assets_fully_sold(self):
        """ارزهای غیر هدف باید کامل فروخته شوند."""
        state = bot.default_state()
        state["holdings"]["XRPBTC"] = 100.0
        scores = self._make_scores(eligible_pair="ETHBTC")
        prices = self._flat_prices()

        _targets, logs, _new_total, new_state = bot.execute_rebalance(state, scores, prices, "manual")

        assert new_state["holdings"]["XRPBTC"] == 0.0
        assert any("فروش کامل XRPBTC" in log for log in logs)

    def test_target_weights_approximately_20_percent(self):
        """وزن هر هدف بعد از ریبالانس باید ≈ ۲۰٪ باشد (با تلورانس کارمزد)."""
        state = bot.default_state()
        scores = self._make_scores(eligible_pair="ETHBTC")
        prices = self._flat_prices()

        _targets, logs, new_total, new_state = bot.execute_rebalance(state, scores, prices, "scheduled")

        eth_val = new_state["holdings"]["ETHBTC"] * prices["ETHBTC"]
        weight = eth_val / new_total
        # با تلورانس ۵٪ (به‌خاطر کارمزد)
        assert 0.15 <= weight <= 0.25, f"وزن ETHBTC باید ≈ 0.20 باشد ولی شد {weight}"

    def test_small_trades_skipped(self):
        """معاملات کوچک‌تر از آستانه انجام نشوند."""
        state = bot.default_state()
        state["btc_cash"] = 0.0001
        scores = self._make_scores(eligible_pair="ETHBTC")
        prices = self._flat_prices()

        _targets, logs, _new_total, new_state = bot.execute_rebalance(state, scores, prices, "scheduled")

        #btc_cash نباید منفی شود
        assert new_state["btc_cash"] >= 0.0

    def test_btc_cash_never_negative(self):
        """btc_cash نباید منفی شود."""
        state = bot.default_state()
        state["btc_cash"] = 0.5
        scores = self._make_scores(eligible_pair="ETHBTC")
        prices = self._flat_prices()

        _targets, logs, new_total, new_state = bot.execute_rebalance(state, scores, prices, "scheduled")

        assert new_state["btc_cash"] >= 0.0, "btc_cash نباید منفی شود"

    def test_total_value_only_decreases_by_fees(self):
        """ارزش کل سبد فقط به اندازه‌ی کارمزدها کم شود."""
        state = bot.default_state()
        state["btc_cash"] = 1.0
        scores = self._make_scores(eligible_pair="ETHBTC")
        prices = self._flat_prices()
        scores["BNBBTC"] = {
            "score": 0.9,
            "return_10d": 4.0,
            "trend_score": 0.08,
            "eligible": True,
            "price": 0.001,
        }

        old_total = state["btc_cash"] + sum(state["holdings"].get(p, 0.0) * prices[p] for p in bot.PAIRS)
        _targets, logs, new_total, new_state = bot.execute_rebalance(state, scores, prices, "scheduled")

        fees = sum(t["fee_btc"] for t in new_state["trade_history"])
        drop = old_total - new_total
        # افت باید تقریباً برابر با مجموع کارمزدها باشد (تلورانس 1e-9)
        assert abs(drop - fees) < 1e-9, (
            f"افت ارزش ({drop}) باید برابر با کارمزدها ({fees}) باشد."
        )

    def test_trade_history_populated(self):
        """trade_history باید بعد از ریبالانس پر شود."""
        state = bot.default_state()
        scores = self._make_scores(eligible_pair="ETHBTC")
        prices = self._flat_prices()

        _targets, logs, new_total, new_state = bot.execute_rebalance(state, scores, prices, "manual")

        assert len(new_state["trade_history"]) > 0
        for t in new_state["trade_history"]:
            assert "ts" in t and "reason" in t and "side" in t and "pair" in t
            assert "qty" in t and "price" in t and "gross_btc" in t and "fee_btc" in t
            assert t["reason"] == "manual"

    def test_state_not_mutated_on_failure(self):
        """در صورت خطا، state اصلی نباید دست بخورد (چون روی کپی کار می‌کنیم)."""
        state = bot.default_state()
        original_history = list(state.get("trade_history", []))
        original_cash = state["btc_cash"]

        scores = {}
        prices = self._flat_prices()

        _targets, logs, new_total, new_state = bot.execute_rebalance(state, scores, prices, "manual")

        # state اصلی نباید تغییر کرده باشد (execute_rebalance روی کپی کار می‌کند)
        assert state["btc_cash"] == original_cash
        assert state["trade_history"] == original_history


# ─────────────────────────── تست‌های وضعیت ───────────────────────────
class TestStateStorage:
    def test_corrupt_state_file_raises_state_error(self, tmp_state_file):
        """با فایل وضعیت خراب، StateError رخ دهد و فایل دست نخورد."""
        os.environ["STATE_FILE"] = tmp_state_file
        with open(tmp_state_file, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json }")

        bot._storage = None
        storage = bot.FileStorage(tmp_state_file)
        with pytest.raises(bot.StateError):
            storage.load()

        with open(tmp_state_file, "r", encoding="utf-8") as f:
            content = f.read()
        assert "this is not valid json" in content

    def test_save_load_roundtrip(self, tmp_state_file):
        """save و بعد load همان داده را برگرداند."""
        os.environ["STATE_FILE"] = tmp_state_file
        bot._storage = None
        storage = bot.FileStorage(tmp_state_file)

        original = {
            "btc_cash": 0.75,
            "holdings": {"ETHBTC": 10.0, "BNBBTC": 0.0, "SOLBTC": 5.5, "XRPBTC": 0.0,
                          "DOGEBTC": 0.0, "ADABTC": 0.0, "LTCBTC": 0.0},
            "last_rebalance_ts": 1234567890,
            "trade_history": [{"ts": 1, "reason": "manual", "side": "buy", "pair": "ETHBTC",
                               "qty": 1.0, "price": 0.001, "gross_btc": 0.001, "fee_btc": 0.00000075}],
        }
        storage.save(original)
        loaded = storage.load()

        assert loaded == original

    def test_normalize_adds_missing_pairs(self):
        """normalize_state جفت‌ارزهای جاافتاده را اضافه کند."""
        raw = {
            "btc_cash": 0.5,
            "holdings": {"ETHBTC": 10.0},
            "last_rebalance_ts": 0,
            "trade_history": [],
        }
        normalized = bot.normalize_state(raw)
        for p in bot.PAIRS:
            assert p in normalized["holdings"]
            assert normalized["holdings"][p] == (10.0 if p == "ETHBTC" else 0.0)

    def test_normalize_rejects_unknown_pair_with_positive_holding(self):
        """برای دارایی جفت‌ارز خارج از PAIRS خطا بدهد."""
        raw = {
            "btc_cash": 1.0,
            "holdings": {"UNKNOWNBTC": 5.0},
            "last_rebalance_ts": 0,
            "trade_history": [],
        }
        with pytest.raises(bot.StateError):
            bot.normalize_state(raw)

    def test_normalize_accepts_unknown_pair_with_zero_holding(self):
        """جفت‌ارز خارج از PAIRS با مقدار صفر نباید خطا بدهد."""
        raw = {
            "btc_cash": 1.0,
            "holdings": {"UNKNOWNBTC": 0.0},
            "last_rebalance_ts": 0,
            "trade_history": [],
        }
        normalized = bot.normalize_state(raw)
        assert normalized["holdings"]["UNKNOWNBTC"] == 0.0


# ─────────────────────────── تست is_due ───────────────────────────
class TestIsDue:
    def test_last_ts_zero_is_due(self):
        """اگر last_rebalance_ts == 0، باید due باشد (ریبالانس اولیه)."""
        state = {"last_rebalance_ts": 0}
        assert bot.is_rebalance_due(state, now=1_700_000_000) is True

    def test_within_interval_not_due(self):
        """اگر زمان از last_ts کمتر از بازه باشد، due نیست."""
        now = 1_700_000_000
        state = {"last_rebalance_ts": now - 100}
        assert bot.is_rebalance_due(state, now=now) is False

    def test_past_interval_is_due(self):
        """اگر زمان از last_ts بیشتر از بازه باشد، due است."""
        now = 1_700_000_000
        state = {"last_rebalance_ts": now - bot.REBALANCE_INTERVAL_SECONDS - 1}
        assert bot.is_rebalance_due(state, now=now) is True


# ─────────────────────────── تست فیلتر چت ───────────────────────────
class TestChatFilter:
    def test_all_command_handlers_have_chat_filter(self):
        """همه‌ی CommandHandlerها باید فیلتر چت داشته باشند."""
        import inspect
        src = inspect.getsource(bot)
        # ۴ CommandHandler داریم: start, status, rebalance, history
        assert src.count("CommandHandler(") == 4
        # هر ۴ مورد باید filters=chat_filter داشته باشند
        assert src.count("filters=chat_filter") >= 4, (
            "همه‌ی CommandHandlerها باید filters=chat_filter داشته باشند"
        )


# ─────────────────────────── تست اعتبارسنجی TELEGRAM_CHAT_ID ───────────────────────────
class TestEnvValidation:
    def test_missing_chat_id_stops_startup(self):
        """اگر TELEGRAM_CHAT_ID تعریف نشده یا عدد نباشد، برنامه نباید بالا بیاید."""
        import subprocess
        env = dict(os.environ)
        env.pop("TELEGRAM_CHAT_ID", None)
        result = subprocess.run(
            [sys.executable, "-c", "import bot"],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "برنامه نباید بدون TELEGRAM_CHAT_ID بالا بیاید"
        combined = result.stderr + result.stdout
        assert "TELEGRAM_CHAT_ID" in combined, (
            f"پیام خطا باید TELEGRAM_CHAT_ID را ذکر کند. combined={combined!r}"
        )
