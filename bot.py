import os
import time
import json
import logging
import requests
import numpy as np
import pandas as pd
from datetime import datetime
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = "portfolio_state.json"

PAIRS = ["ETHBTC", "BNBBTC", "SOLBTC", "XRPBTC", "DOGEBTC", "ADABTC", "LTCBTC"]
LOOKBACK_HOURS = 240  # 10 روز
CHECK_INTERVAL_SECONDS = 3600
FEE = 0.00075

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading state: {e}")
    return {
        "btc_cash": 1.0,
        "holdings": {p: 0.0 for p in PAIRS},
        "last_rebalance_ts": 0,
        "trade_history": []
    }

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving state: {e}")

# دریافت پایدار کندل‌ها از اندپوینت‌های رسمی با قابلیت fallback
def fetch_klines(symbol, limit=250):
    endpoints = [
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1h&limit={limit}",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit={limit}"
    ]
    
    for url in endpoints:
        try:
            res = requests.get(url, headers=HEADERS, timeout=12)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list) and len(data) >= limit - 20:
                    closes = [float(k[4]) for k in data]
                    return np.array(closes, dtype=np.float32)
                else:
                    logger.warning(f"Unexpected response for {symbol}: {str(data)[:100]}")
        except Exception as e:
            logger.warning(f"Fail fetching {symbol} from {url}: {e}")
            continue

    raise RuntimeError(f"امکان دریافت کندل‌های معتبر برای {symbol} میسر نشد.")

def compute_hybrid_scores():
    rets = {}
    reg_scores = {}
    current_prices = {}

    x = np.arange(LOOKBACK_HOURS, dtype=np.float32)
    x_diff = x - np.mean(x)
    x_var = np.sum(x_diff ** 2)

    for p in PAIRS:
        closes = fetch_klines(p, limit=LOOKBACK_HOURS + 10)
        window = closes[-LOOKBACK_HOURS:]
        c_now = float(window[-1])
        c_old = float(window[0])
        current_prices[p] = c_now

        # بازدهی ۱۰ روزه
        ret = (c_now / c_old) - 1.0
        rets[p] = ret

        # شیب رگرسیون پیوسته
        y = window / c_old
        y_diff = y - np.mean(y)
        slope = np.sum(x_diff * y_diff) / x_var
        ss_tot = np.sum(y_diff ** 2)
        r2 = ((np.sum(x_diff * y_diff) ** 2) / (x_var * ss_tot)) if ss_tot > 0 else 0.0
        reg_scores[p] = float(slope * r2)

    r_arr = np.array(list(rets.values()))
    s_arr = np.array(list(reg_scores.values()))

    z_ret = (r_arr - np.mean(r_arr)) / (np.std(r_arr) + 1e-6)
    z_reg = (s_arr - np.mean(s_arr)) / (np.std(s_arr) + 1e-6)

    blend = 0.5 * z_ret + 0.5 * z_reg
    final_scores = {}
    for idx, p in enumerate(PAIRS):
        is_eligible = (rets[p] > 0 or reg_scores[p] > 0)
        final_scores[p] = {
            "score": float(blend[idx]) if is_eligible else -999.0,
            "return_10d": float(rets[p] * 100),
            "r2": float(reg_scores[p]),
            "price": float(current_prices[p])
        }

    return final_scores, current_prices

def execute_rebalance(state, scores, prices):
    sorted_pairs = sorted(scores.keys(), key=lambda p: scores[p]["score"], reverse=True)
    targets = [p for p in sorted_pairs[:2] if scores[p]["score"] > -900.0]

    btc_cash = float(state.get("btc_cash", 1.0))
    holdings = state.get("holdings", {p: 0.0 for p in PAIRS})
    logs = []

    total_val = btc_cash + sum(holdings.get(p, 0.0) * prices[p] for p in PAIRS)

    # فروش موارد خروجی
    for p in PAIRS:
        qty = holdings.get(p, 0.0)
        if p not in targets and qty > 0:
            sold_btc = (qty * prices[p]) * (1 - FEE)
            btc_cash += sold_btc
            logs.append(f"🔴 فروش کامل {p} معادل {sold_btc:.4f} BTC")
            holdings[p] = 0.0

    # تخصیص به تارگت‌های منتخب
    for p in targets:
        target_val = total_val * 0.20
        curr_val = holdings.get(p, 0.0) * prices[p]
        diff = target_val - curr_val

        if diff > 0 and btc_cash >= diff:
            bought = (diff / prices[p]) * (1 - FEE)
            holdings[p] = holdings.get(p, 0.0) + bought
            btc_cash -= diff
            logs.append(f"🟢 خرید {p} معادل {diff:.4f} BTC")
        elif diff < 0:
            excess = min(abs(diff) / prices[p], holdings.get(p, 0.0))
            sold_btc = (excess * prices[p]) * (1 - FEE)
            btc_cash += sold_btc
            holdings[p] -= excess
            logs.append(f"🟡 سیو سود {p} معادل {sold_btc:.4f} BTC")

    state["btc_cash"] = btc_cash
    state["holdings"] = holdings
    state["last_rebalance_ts"] = int(time.time())
    new_total = btc_cash + sum(holdings[p] * prices[p] for p in PAIRS)
    save_state(state)

    return targets, logs, new_total

def format_status_message(state, scores, prices, targets=None, logs=None):
    total_val = state["btc_cash"] + sum(state["holdings"].get(p, 0.0) * prices[p] for p in PAIRS)
    
    msg = "📊 **گزارش وضعیت استراتژی هیبریدی ۱۰ روزه**\n\n"
    msg += f"💰 **ارزش کل پورتفو:** `{total_val:.4f} BTC`\n"
    msg += f"💵 **موجودی نقد بیت‌کوین:** `{state['btc_cash']:.4f} BTC` ({state['btc_cash']/total_val*100:.1f}%)\n\n"
    
    msg += "📈 **سبد آلت‌کوین‌ها:**\n"
    has_alts = False
    for p in PAIRS:
        amt = state["holdings"].get(p, 0.0)
        if amt > 0:
            has_alts = True
            val = amt * prices[p]
            msg += f"▫️ `{p}`: {amt:.2f} واحد (`{val:.4f} BTC` | {val/total_val*100:.1f}%)\n"
    if not has_alts:
        msg += "▫️ *تمام سبد در حال حاضر بیت‌کوین نقد است.*\n"

    msg += "\n🔍 **رتبه‌بندی ۱۰ روز اخیر:**\n"
    sorted_p = sorted(scores.keys(), key=lambda x: scores[x]["score"], reverse=True)
    for p in sorted_p:
        sc = scores[p]
        status = "✅" if p in (targets or []) else "▫️"
        msg += f"{status} `{p:<7}` | بازدهی: `{sc['return_10d']:>+5.1f}%` | امتیاز: `{sc['score']:>+4.2f}`\n"

    if logs:
        msg += "\n📝 **تراکنش‌های ریبالانس:**\n" + "\n".join(logs)

    return msg

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ربات معامله‌گر هیبریدی ۱۰ روزه فعال است.\n\n"
        "دستورات:\n"
        "/status - نمایش وضعیت فعلی پورتفو و رتبه‌بندی ارزها\n"
        "/rebalance - اجرای دستی چرخه ریبالانس همین لحظه"
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("در حال دریافت داده‌های بازار و محاسبه Z-Score...")
    try:
        state = load_state()
        scores, prices = compute_hybrid_scores()
        msg = format_status_message(state, scores, prices)
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"خطا در پردازش: {e}")

async def manual_rebalance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("در حال اجرای ریبالانس...")
    try:
        state = load_state()
        scores, prices = compute_hybrid_scores()
        targets, logs, _ = execute_rebalance(state, scores, prices)
        msg = format_status_message(state, scores, prices, targets=targets, logs=logs)
        await update.message.reply_text(f"⚡ **ریبالانس دستی اجرا شد:**\n\n{msg}", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"خطا در ریبالانس: {e}")

# تابع بازبینی خودکار متصل به JobQueue استاندارد تلگرام
async def check_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        state = load_state()
        now = int(time.time())
        elapsed = now - state.get("last_rebalance_ts", 0)

        # اجرای خودکار پس از ۲۴۰ ساعت
        if elapsed >= (LOOKBACK_HOURS * 3600):
            logger.info("سررسید ۲۴۰ ساعت فرا رسید. شروع ریبالانس خودکار...")
            scores, prices = compute_hybrid_scores()
            targets, logs, _ = execute_rebalance(state, scores, prices)
            msg = format_status_message(state, scores, prices, targets=targets, logs=logs)
            
            chat_id = TELEGRAM_CHAT_ID or (context.job.chat_id if context.job else None)
            if chat_id:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🚨 **اجرای سررسید دوره ۱۰ روزه (ریبالانس خودکار)**\n\n{msg}",
                    parse_mode='Markdown'
                )
    except Exception as e:
        logger.error(f"خطا در جاب دوره‌ای: {e}")

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("متغیر TELEGRAM_BOT_TOKEN تعریف نشده است.")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("rebalance", manual_rebalance_command))

    # زمان‌بندی بررسی دوره‌ای هر ۱ ساعت با جاب‌کیوی استاندارد ربات
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(check_scheduler_job, interval=CHECK_INTERVAL_SECONDS, first=10)
        logger.info("JobQueue با موفقیت زمان‌بندی شد.")

    logger.info("ربات با موفقیت استارت خورد.")
    app.run_polling()

if __name__ == '__main__':
    main()
