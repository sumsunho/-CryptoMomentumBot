# CryptoMomentumBot

ربات تلگرامی **معامله‌ی شبیه‌سازی‌شده (Paper Trading)** که یک استراتژی مومنتوم را روی جفت‌ارزهای BTC در Binance اجرا می‌کند. هر ۱۰ روز دو جفت‌ارز برتر (بر اساس ترکیب z-score بازدهی ۱۰ روزه و شیب رگرسیون × R²) انتخاب می‌شوند و به هر کدام ۲۰٪ از سبد اختصاص می‌یابد.

> ⚠️ **هشدار:** این ربات هیچ سفارش واقعی ثبت نمی‌کند و هیچ API صرافی با کلید خصوصی استفاده نمی‌کند. تمام معاملات فقط روی وضعیت ذخیره‌شده محلی (یا PostgreSQL) اعمال می‌شوند. این پروژه صرفاً برای آزمایش و شبیه‌سازی استراتژی است.

## استراتژی

- **پنجره‌ی محاسبه‌ی مومنتوم:** ۲۴۰ ساعت (۱۰ روز) کندل بسته‌شده
- **فاصله‌ی ریبالانس خودکار:** ۲۴۰ ساعت (۱۰ روز)
- **تعداد ارزهای منتخب (TOP_N):** ۲
- **وزن هر ارز (WEIGHT_PER_ASSET):** ۲۰٪ — با ۲×۲۰٪، ۶۰٪ سبد به‌صورت BTC نقد می‌ماند
- **شرط ورود (eligibility):** بازدهی ۱۰ روزه > ۰ **یا** trend_score (slope × R²) > ۰
- **انتخاب هدف‌ها:** از بین TOP_N رتبه‌ی اول فقط آن‌هایی که `eligible = True` هستند
- **فرمول امتیاز:** `blend = 0.5 × z_score(return) + 0.5 × z_score(trend_score)`
- **کارمزد فرضی (FEE):** ۰٫۰۷۵٪

### جفت‌ارزهای پشتیبانی‌شده

```
ETHBTC, BNBTC, SOLBTC, XRPBTC, DOGEBTC, ADABTC, LTCBTC
```

## متغیرهای محیطی

| متغیر | ضرورت | پیش‌فرض | توضیح |
|-------|-------|---------|-------|
| `TELEGRAM_BOT_TOKEN` | الزامی | — | توکن ربات تلگرام از [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | الزامی | — | شناسه‌ی عددی چتی که اجازه‌ی کار با ربات را دارد. اگر تعریف نشده یا عدد نباشد، برنامه با خطا متوقف می‌شود. |
| `DATABASE_URL` | اختیاری | — | اگر تعریف شود، وضعیت در PostgreSQL ذخیره می‌شود. در غیر این صورت در فایل ذخیره می‌شود. |
| `STATE_FILE` | اختیاری | `portfolio_state.json` | مسیر فایل وضعیت وقتی دیتابیس ندارید. |
| `BOT_TIMEZONE` | اختیاری | `Asia/Tehran` | منطقه‌ی زمانی نمایش (برای زمان آخرین/بعدی ریبالانس). |
| `LOOKBACK_HOURS` | اختیاری | `240` | پنجره‌ی محاسبه‌ی مومنتوم به ساعت. |
| `REBALANCE_INTERVAL_HOURS` | اختیاری | `240` | فاصله‌ی ریبالانس خودکار به ساعت. |

> 📌 برای پیدا کردن `TELEGRAM_CHAT_ID` می‌توانید از [@userinfobot](https://t.me/userinfobot) کمک بگیرید.

## دستورات ربات

| دستور | توضیح |
|-------|-------|
| `/start` | پیام خوش‌آمد و لیست دستورات |
| `/status` | نمایش وضعیت فعلی پورتفو، رتبه‌بندی ارزها، زمان آخرین/بعدی ریبالانس |
| `/rebalance` | اجرای دستی چرخه‌ی ریبالانس همین لحظه |
| `/history` | نمایش ۱۰ معامله‌ی آخر |

همه‌ی دستورها فقط از چتی که در `TELEGRAM_CHAT_ID` تعریف شده قابل اجرا هستند.

## اجرای محلی

### ۱. نصب وابستگی‌ها

```bash
python3 -m venv .venv
source .venv/bin/activate  # در ویندوز: .venv\Scripts\activate
pip install -r requirements.txt
```

برای اجرای تست‌ها:

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

### ۲. تنظیم متغیرهای محیطی

```bash
export TELEGRAM_BOT_TOKEN="توکن-ربات-شما"
export TELEGRAM_CHAT_ID="شناسه-چت-شما"
# اختیاری:
# export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
# export STATE_FILE="portfolio_state.json"
# export BOT_TIMEZONE="Asia/Tehran"
```

یا از فایل `.env` استفاده کنید (با [python-dotenv](https://github.com/theskumar/python-dotenv) یا ابزار مشابه):

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### ۳. اجرای ربات

```bash
python bot.py
```

اگر `TELEGRAM_CHAT_ID` تعریف نشده باشد یا عدد نباشد، برنامه با پیام خطای واضح متوقف می‌شود.

## اجرای تست‌ها

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
```

تست‌ها به اینترنت، Binance یا تلگرام وصل نمی‌شوند — تمام وابستگی‌های خارجی mock می‌شوند.

### پوشش تست‌ها

- امتیاز سری صعودی بالاتر از نزولی
- خطای `MarketDataError` در صورت کمبود کندل بسته‌شده
- کندل بازِ آخر در محاسبه‌ی امتیاز استفاده نمی‌شود
- `execute_rebalance`: فروش کامل غیر اهداف، وزن هدف ≈ ۲۰٪، رد معاملات کوچک، `btc_cash` منفی نمی‌شود، افت ارزش فقط به اندازه‌ی کارمزدها، پر شدن `trade_history`
- فایل وضعیت خراب → `StateError`، فایل دست‌نخورده می‌ماند
- round-trip ذخیره‌سازی فایل
- `normalize_state`: اضافه‌کردن جفت‌ارزهای جاافتاده، خطا برای دارایی خارج از PAIRS
- منطق `is_rebalance_due` (last_ts=0، کمتر و بیشتر از بازه)
- همه‌ی `CommandHandler`ها فیلتر چت دارند
- توقف برنامه بدون `TELEGRAM_CHAT_ID`

## CI / GitHub Actions

فایل `.github/workflows/tests.yml` روی هر push یا pull_request با پایتون 3.11 وابستگی‌ها را نصب و `pytest -v` را اجرا می‌کند.

## استقرار در Heroku

```bash
heroku create your-app-name
heroku config:set TELEGRAM_BOT_TOKEN="..."
heroku config:set TELEGRAM_CHAT_ID="..."
# اختیاری (پیشنهادی برای Heroku):
heroku addons:create heroku-postgresql:essential-0
heroku config:set DATABASE_URL="$(heroku config:get DATABASE_URL)"
git push heroku main
heroku ps:scale worker=1
```

> ⚠️ روی Heroku، فایل‌سیستم موقت (ephemeral) است. اگر `DATABASE_URL` تنظیم نشود، وضعیت با هر ری‌استارت dyno پاک می‌شود. حتماً از Heroku Postgres استفاده کنید.

## استقرار در Railway

1. ریپوی GitHub را به Railway متصل کنید.
2. متغیرهای محیطی `TELEGRAM_BOT_TOKEN` و `TELEGRAM_CHAT_ID` را تنظیم کنید.
3. (پیشنهادی) یک PostgreSQL اضافه کنید و `DATABASE_URL` را به آن متصل کنید.
4. Railway به‌طور خودکار از `Procfile` استفاده می‌کند (`worker: python bot.py`).

## ذخیره‌سازی وضعیت

- **فایل (پیش‌فرض):** `portfolio_state.json` در دایرکتوری جاری. نوشتن به‌صورت اتمیک انجام می‌شود (نوشتن در `.tmp`، `fsync`، سپس `os.replace`).
- **PostgreSQL:** اگر `DATABASE_URL` تنظیم شود، جدول `bot_state` با ستون `JSONB` به‌صورت خودکار ساخته می‌شود و رکورد با `id=1` به‌صورت upsert ذخیره می‌شود.
- **رفتار در صورت خرابی:** اگر فایل یا رکورد وضعیت خراب باشد، ربات با `StateError` متوقف می‌شود؛ سبد به‌صورت بی‌صدا ریست نمی‌شود. سبد پیش‌فرض فقط وقتی وضعیتی وجود ندارد ساخته می‌شود.

## امنیت

- همه‌ی `CommandHandler`ها با `filters.Chat(chat_id=ALLOWED_CHAT_ID)` محدود شده‌اند — فقط چت مجاز می‌تواند با ربات کار کند.
- اگر `TELEGRAM_CHAT_ID` تعریف نشده یا عدد نباشد، برنامه با خطا متوقف می‌شود.
- هیچ توکن یا کلیدی در کد نیست — همه‌چیز از متغیرهای محیطی خوانده می‌شود.
- لاگ‌های `httpx` به سطح `WARNING` تنظیم شده‌اند تا آدرس کامل درخواست‌ها (که ممکن است شامل توکن باشد) در لاگ نشت نکنند.
- متن‌های متغیر در پیام‌های تلگرام با `html.escape` امن شده‌اند و از `ParseMode.HTML` به‌جای Markdown استفاده می‌شود.
- هیچ API صرافی با کلید خصوصی استفاده نمی‌شود. تمام داده‌های بازار از اندپوینت‌های عمومی Binance خوانده می‌شوند.

## ساختار پروژه

```
.
├── bot.py                  # کد اصلی ربات
├── requirements.txt        # وابستگی‌های اجرایی
├── requirements-dev.txt    # وابستگی‌های توسعه (pytest)
├── Procfile                # تنظیمات Heroku/Railway
├── python-version          # نسخه‌ی Python (Heroku)
├── .gitignore
├── .github/
│   └── workflows/
│       └── tests.yml       # CI با GitHub Actions
├── tests/
│   ├── __init__.py
│   └── test_bot.py         # تست‌های pytest
└── README.md
```

## مجوز

این پروژه به‌صورت خصوصی برای مصارف آموزشی و آزمایش استراتژی توسعه داده شده است.
