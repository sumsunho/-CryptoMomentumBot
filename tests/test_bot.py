"""Pytest tests for CryptoMomentumBot.

هیچ تستی به اینترنت، Binance یا تلگرام وصل نمی‌شود. تمام وابستگی‌های خارجی mock می‌شوند.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
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

from telegram import Chat, Message, MessageEntity, Update, User  # noqa: E402
from telegram.ext import CommandHandler  # noqa: E402


# ─────────────────────────── Fixtures ───────────────────────────
@pytest.fixture
def tmp_state_file(tmp_path):
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


def _make_closed_klines(closes, last_open_close=None):
    """ساخت لیست کندل‌های Binance با فرمت واقعی.

    هر کندل: [openTime, open, high, low, close, volume, closeTime, ...]
    closeTime = k[6]؛ اگر < now_ms باشد یعنی کندل بسته‌شده است.
    کندل آخر را با closeTime > now_ms می‌سازیم تا «باز» تلقی شود.
    """
    klines = []
    now_ms = int(time.time() * 1000)
    n = len(closes)
    for i, c in enumerate(closes):
        # همه‌ی کندل‌های واقعی قبل از now بسته شوند
        open_time = now_ms - (n - i) * 3600_000 - 3600_000
        close_time = open_time + 3600_000 - 1
        if close_time >= now_ms:
            close_time = now_ms - (n - i) * 3600_000 - 1
            open_time = close_time - 3600_000 + 1
        klines.append(
            [open_time, str(c), str(c), str(c), str(c), "1.0", close_time, "1.0", 0, "0", "0", "0"]
        )
    # کندل آخر (باز): closeTime در آینده
    last_close = last_open_close if last_open_close is not None else closes[-1]
    open_time = now_ms + 1000
    close_time = now_ms + 3_600_000  # در آینده
    klines.append(
        [open_time, str(last_close), str(last_close), str(last_close), str(last_close), "1.0", close_time, "1.0", 0, "0", "0", "0"]
    )
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


def _make_command_update(chat_id, command="start"):
    """ساخت یک Update واقعی تلگرام برای تست CommandHandler.check_update.

    شامل یک Message با متن `/command` و یک MessageEntity از نوع BOT_COMMAND.
    یک bot mock شده به message متصل می‌شود تا PTB v22 بتواند get_bot() را صدا بزند.
    """
    chat = Chat(id=chat_id, type=Chat.PRIVATE)
    user = User(id=chat_id, first_name="Test", is_bot=False)
    text = f"/{command}"
    msg = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
        entities=[
            MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text))
        ],
    )
    # PTB v22 نیاز دارد که message به یک bot متصل باشد (در check_update → get_bot)
    msg.set_bot(MagicMock())
    return Update(update_id=1, message=msg)


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


# ─────────────────────────── تست‌های انتخاب هدف ───────────────────────────
class TestSelectTargets:
    def _make_scores(self, eligible_pairs, scores_by_pair=None):
        """ساخت scores برای تست.

        eligible_pairs: لیست جفت‌ارزهایی که eligible هستند.
        scores_by_pair: dict اختیاری برای مقداردهی score دستی.
        """
        scores = {}
        for i, p in enumerate(bot.PAIRS):
            eligible = p in eligible_pairs
            default_score = float(len(bot.PAIRS) - i) if eligible else -float(len(bot.PAIRS) - i)
            scores[p] = {
                "score": float(scores_by_pair[p]) if scores_by_pair and p in scores_by_pair else default_score,
                "return_10d": 5.0 if eligible else -5.0,
                "trend_score": 0.1 if eligible else -0.1,
                "eligible": eligible,
                "price": 0.001,
            }
        return scores

    def test_selects_top_n_eligible(self):
        """از بین eligibleها، TOP_N با بالاترین امتیاز انتخاب شوند."""
        scores = self._make_scores(
            eligible_pairs=["ETHBTC", "BNBBTC", "SOLBTC"],
            scores_by_pair={"ETHBTC": 3.0, "BNBBTC": 2.0, "SOLBTC": 1.0},
        )
        targets = bot.select_targets(scores)
        assert len(targets) == bot.TOP_N
        assert "ETHBTC" in targets
        assert "BNBBTC" in targets

    def test_skips_ineligible_even_with_high_score(self):
        """اگر جفت‌ارز ineligible در TOP_N اول باشد، انتخاب نمی‌شود (خروجی کم می‌شود).

        منطق اصلی: فقط بین TOP_N رتبه‌ی اول eligible را فیلتر می‌کند. پس اگر
        TOP_N اول ineligible باشند، خروجی خالی می‌شود.
        """
        scores = self._make_scores(
            eligible_pairs=["SOLBTC", "XRPBTC"],
            scores_by_pair={"ETHBTC": 10.0, "BNBBTC": 9.0, "SOLBTC": 1.0, "XRPBTC": 0.5},
        )
        targets = bot.select_targets(scores)
        # چون TOP_N اول (ETHBTC, BNBBTC با امتیاز 10 و 9) eligible نیستند، خروجی خالی می‌ماند
        assert "ETHBTC" not in targets
        assert "BNBBTC" not in targets
        assert targets == [], (
            "وقتی TOP_N اول ineligible باشند، خروجی باید خالی باشد (منطق نسخه‌ی اصلی)."
        )

    def test_selects_eligible_when_in_top_n(self):
        """اگر eligibleها در TOP_N اول باشند، انتخاب می‌شوند."""
        scores = self._make_scores(
            eligible_pairs=["SOLBTC", "XRPBTC"],
            scores_by_pair={"SOLBTC": 10.0, "XRPBTC": 9.0, "ETHBTC": 1.0, "BNBBTC": 0.5},
        )
        targets = bot.select_targets(scores)
        # SOLBTC و XRPBTC در TOP_N اول هستند و eligible هستند
        assert "SOLBTC" in targets
        assert "XRPBTC" in targets
        assert "ETHBTC" not in targets
        assert "BNBBTC" not in targets
        assert len(targets) == bot.TOP_N

    def test_empty_scores_returns_empty(self):
        """با scores خالی، خروجی لیست خالی باشد."""
        targets = bot.select_targets({})
        assert targets == []


# ─────────────────────────── تست‌های ریبالانس ───────────────────────────
class TestExecuteRebalance:
    def _flat_prices(self):
        return {p: 0.001 for p in bot.PAIRS}

    def _make_scores(self, eligible_pair="ETHBTC"):
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

        _targets, logs, _new_total, new_state = bot.execute_rebalance(
            state, scores, prices, "manual"
        )

        assert new_state["holdings"]["XRPBTC"] == 0.0
        assert any("فروش کامل XRPBTC" in log for log in logs)

    def test_target_weights_approximately_20_percent(self):
        """وزن هر هدف بعد از ریبالانس باید ≈ ۲۰٪ باشد (با تلورانس کارمزد)."""
        state = bot.default_state()
        scores = self._make_scores(eligible_pair="ETHBTC")
        prices = self._flat_prices()

        _targets, logs, new_total, new_state = bot.execute_rebalance(
            state, scores, prices, "scheduled"
        )

        eth_val = new_state["holdings"]["ETHBTC"] * prices["ETHBTC"]
        weight = eth_val / new_total
        # با تلورانس ۵٪ (به‌خاطر کارمزد)
        assert 0.15 <= weight <= 0.25, f"وزن ETHBTC باید ≈ 0.20 باشد ولی شد {weight}"

    def test_small_trades_skipped_for_buy_and_trim(self):
        """معاملات کوچک‌تر از آستانه برای trim و buy انجام نشوند؛ اما sell همیشه انجام شود."""
        state = bot.default_state()
        state["btc_cash"] = 0.0001
        scores = self._make_scores(eligible_pair="ETHBTC")
        prices = self._flat_prices()

        _targets, logs, _new_total, new_state = bot.execute_rebalance(
            state, scores, prices, "scheduled"
        )

        # btc_cash نباید منفی شود
        assert new_state["btc_cash"] >= 0.0

    def test_small_non_target_asset_still_sold(self):
        """دارایی خیلی کوچکِ خارج از هدف هم فروخته شود (رفتار نسخه‌ی اصلی)."""
        state = bot.default_state()
        state["btc_cash"] = 1.0
        # مقدار خیلی کوچک از یک جفت‌ارز غیر هدف
        state["holdings"]["XRPBTC"] = 0.00001  # ارزش = 0.00001 * 0.001 = 1e-8 BTC
        scores = self._make_scores(eligible_pair="ETHBTC")
        prices = self._flat_prices()

        _targets, logs, _new_total, new_state = bot.execute_rebalance(
            state, scores, prices, "manual"
        )

        # XRPBTC باید کامل فروخته شده باشد (حتی با ارزش خیلی کم)
        assert new_state["holdings"]["XRPBTC"] == 0.0, (
            "دارایی غیر هدف باید همیشه کامل فروخته شود، حتی اگر ارزشش زیر آستانه باشد."
        )
        assert any("فروش کامل XRPBTC" in log for log in logs)

    def test_btc_cash_never_negative(self):
        """btc_cash نباید منفی شود."""
        state = bot.default_state()
        state["btc_cash"] = 0.5
        scores = self._make_scores(eligible_pair="ETHBTC")
        prices = self._flat_prices()

        _targets, logs, new_total, new_state = bot.execute_rebalance(
            state, scores, prices, "scheduled"
        )

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

        old_total = state["btc_cash"] + sum(
            state["holdings"].get(p, 0.0) * prices[p] for p in bot.PAIRS
        )
        _targets, logs, new_total, new_state = bot.execute_rebalance(
            state, scores, prices, "scheduled"
        )

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

        _targets, logs, new_total, new_state = bot.execute_rebalance(
            state, scores, prices, "manual"
        )

        assert len(new_state["trade_history"]) > 0
        for t in new_state["trade_history"]:
            assert "ts" in t and "reason" in t and "side" in t and "pair" in t
            assert "qty" in t and "price" in t and "gross_btc" in t and "fee_btc" in t
            assert t["reason"] == "manual"

    def test_state_not_mutated(self):
        """state اصلی نباید تغییر کند (execute_rebalance روی کپی کار می‌کند)."""
        state = bot.default_state()
        original_history = list(state.get("trade_history", []))
        original_cash = state["btc_cash"]

        scores = {}
        prices = self._flat_prices()

        _targets, logs, new_total, new_state = bot.execute_rebalance(
            state, scores, prices, "manual"
        )

        # state اصلی نباید تغییر کرده باشد
        assert state["btc_cash"] == original_cash
        assert state["trade_history"] == original_history

    def test_execute_rebalance_returns_four_values(self):
        """execute_rebalance باید دقیقاً ۴ مقدار برگرداند (targets, logs, new_total, new_state)."""
        state = bot.default_state()
        scores = self._make_scores(eligible_pair="ETHBTC")
        prices = self._flat_prices()

        result = bot.execute_rebalance(state, scores, prices, "manual")
        assert isinstance(result, tuple) and len(result) == 4, (
            "execute_rebalance باید ۴ مقدار برگرداند: (targets, logs, new_total, new_state)"
        )


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
            "holdings": {
                "ETHBTC": 10.0, "BNBBTC": 0.0, "SOLBTC": 5.5, "XRPBTC": 0.0,
                "DOGEBTC": 0.0, "ADABTC": 0.0, "LTCBTC": 0.0,
            },
            "last_rebalance_ts": 1234567890,
            "trade_history": [
                {
                    "ts": 1, "reason": "manual", "side": "buy", "pair": "ETHBTC",
                    "qty": 1.0, "price": 0.001, "gross_btc": 0.001, "fee_btc": 0.00000075,
                }
            ],
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
    def test_command_handlers_only_accept_allowed_chat(self):
        """برای هر CommandHandler، پیام از چت مجاز پردازش شود و از چت دیگر رد شود.

        از handler.check_update برای تست واقعی فیلتر استفاده می‌کنیم (نه inspect.getsource).
        """
        app = bot.build_application()

        # گرفتن همه‌ی handlerها از همه‌ی گروه‌ها
        all_handlers = []
        for handlers in app.handlers.values():
            all_handlers.extend(handlers)

        command_handlers = [h for h in all_handlers if isinstance(h, CommandHandler)]
        assert len(command_handlers) == 4, (
            f"باید ۴ CommandHandler باشد (start, status, rebalance, history) ولی {len(command_handlers)} پیدا شد."
        )

        for handler in command_handlers:
            # handler.commands یک frozenset است؛ اولین عضو را می‌گیریم
            cmd = list(handler.commands)[0]

            # پیام از چت مجاز
            allowed_update = _make_command_update(bot.ALLOWED_CHAT_ID, cmd)
            # پیام از چت غیرمجاز
            disallowed_update = _make_command_update(bot.ALLOWED_CHAT_ID + 999, cmd)

            # check_update برای چت مجاز باید truthy (non-None, non-False) برگرداند
            # و برای چت غیرمجاز falsy (None یا False)
            allowed_result = handler.check_update(allowed_update)
            disallowed_result = handler.check_update(disallowed_update)

            assert allowed_result, (
                f"Handler /{cmd} باید پیام از چت مجاز {bot.ALLOWED_CHAT_ID} را بپذیرد. "
                f"نتیجه: {allowed_result!r}"
            )
            assert not disallowed_result, (
                f"Handler /{cmd} نباید پیام از چت غیرمجاز {bot.ALLOWED_CHAT_ID + 999} را بپذیرد. "
                f"نتیجه: {disallowed_result!r}"
            )


# ─────────────────────────── تست‌های format_status_message ───────────────────────────
class TestFormatStatusMessage:
    def _make_scores(self, eligible_pair="ETHBTC"):
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

    def _make_prices(self):
        return {p: 0.001 for p in bot.PAIRS}

    def _make_state(self, last_rebalance_ts=0):
        return {
            "btc_cash": 1.0,
            "holdings": {p: 0.0 for p in bot.PAIRS},
            "last_rebalance_ts": last_rebalance_ts,
            "trade_history": [],
        }

    def test_without_targets_and_logs_last_ts_zero(self):
        """بدون targets و logs و با last_rebalance_ts=0، پیام باید متناسب بسازد."""
        state = self._make_state(last_rebalance_ts=0)
        scores = self._make_scores()
        prices = self._make_prices()

        msg = bot.format_status_message(state, scores, prices)

        assert "ارزش کل پورتفو" in msg
        assert "هنوز انجام نشده" in msg, "برای last_ts=0 باید پیام «هنوز انجام نشده» بیاید."
        # نباید بخش تراکنش‌ها بیاید
        assert "تراکنش‌های ریبالانس" not in msg

    def test_with_targets_and_logs_last_ts_nonzero(self):
        """با targets و logs و با last_rebalance_ts غیرصفر، پیام باید متناسب بسازد."""
        ts = int(time.time()) - 100
        state = self._make_state(last_rebalance_ts=ts)
        scores = self._make_scores()
        prices = self._make_prices()
        targets = ["ETHBTC"]
        logs = ["🟢 خرید ETHBTC → 0.200000 BTC"]

        msg = bot.format_status_message(state, scores, prices, targets=targets, logs=logs)

        assert "آخرین ریبالانس" in msg
        assert "ریبالانس بعدی" in msg
        assert "تراکنش‌های ریبالانس" in msg
        assert "🟢 خرید ETHBTC" in msg

    def test_does_not_raise_on_html_escape_format_spec(self):
        """format spec نباید ValueError بدهد (تست باگ &lt;7)."""
        state = self._make_state(last_rebalance_ts=0)
        scores = self._make_scores()
        prices = self._make_prices()

        # این فراخوانی نباید exception بدهد
        msg = bot.format_status_message(state, scores, prices)

        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_marks_only_target_pairs_with_check(self):
        """فقط targets باید ✅ بگیرند، بقیه باید ▫️ داشته باشند."""
        state = self._make_state(last_rebalance_ts=0)
        scores = self._make_scores()
        prices = self._make_prices()
        targets = ["ETHBTC"]

        msg = bot.format_status_message(state, scores, prices, targets=targets)

        # ETHBTC در targets است → باید ✅ داشته باشد
        # بقیه باید ▫️ داشته باشند
        ethbtc_line = next(line for line in msg.split("\n") if "ETHBTC" in line and "بازدهی" in line)
        assert "✅" in ethbtc_line

        bnbbtc_line = next(line for line in msg.split("\n") if "BNBBTC" in line and "بازدهی" in line)
        assert "✅" not in bnbbtc_line
        assert "▫️" in bnbbtc_line


# ─────────────────────────── تست‌های format_history_message ───────────────────────────
class TestFormatHistoryMessage:
    def test_empty_history(self):
        """با trade_history خالی، پیام «هنوز هیچ معامله‌ای» بدهد."""
        state = {"trade_history": []}
        msg = bot.format_history_message(state)
        assert "هنوز هیچ معامله‌ای" in msg

    def test_with_trades(self):
        """با معاملات، پیام باید شامل جزئیات معامله باشد."""
        state = {
            "trade_history": [
                {
                    "ts": int(time.time()),
                    "reason": "manual",
                    "side": "buy",
                    "pair": "ETHBTC",
                    "qty": 100.0,
                    "price": 0.001,
                    "gross_btc": 0.1,
                    "fee_btc": 0.000075,
                },
                {
                    "ts": int(time.time()) - 60,
                    "reason": "scheduled",
                    "side": "sell",
                    "pair": "XRPBTC",
                    "qty": 50.0,
                    "price": 0.002,
                    "gross_btc": 0.1,
                    "fee_btc": 0.000075,
                },
            ]
        }
        msg = bot.format_history_message(state)
        assert "ETHBTC" in msg
        assert "buy" in msg
        assert "manual" in msg
        assert "XRPBTC" in msg
        assert "sell" in msg
        assert "scheduled" in msg

    def test_does_not_raise_on_html_escape(self):
        """با کاراکترهای خاص در pair، نباید exception بدهد."""
        state = {
            "trade_history": [
                {
                    "ts": int(time.time()),
                    "reason": "manual",
                    "side": "buy",
                    "pair": "ETH<BTC",  # کاراکتر HTML خاص
                    "qty": 1.0,
                    "price": 0.001,
                    "gross_btc": 0.001,
                    "fee_btc": 0.00000075,
                }
            ]
        }
        # این فراخوانی نباید exception بدهد (html.escape باید &lt; کند)
        msg = bot.format_history_message(state)
        assert isinstance(msg, str)
        # کاراکتر < نباید به‌صورت خام در خروجی باشد (escape شده)
        assert "ETH<BTC" not in msg
        assert "ETH&lt;BTC" in msg


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
