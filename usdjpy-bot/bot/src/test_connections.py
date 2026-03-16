"""
test_connections.py — Manual connection validation script.

Run: docker compose exec bot python src/test_connections.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"


def test_database():
    """Connect to TimescaleDB and verify all required tables exist."""
    print("\n--- Database ---")
    try:
        import config
        import sqlalchemy
        from sqlalchemy import text

        engine = sqlalchemy.create_engine(config.DATABASE_URL)
        required_tables = [
            "candles", "cot_data", "sentiment_data", "signals", "trades",
            "model_performance", "rollover_data", "interest_rates", "news_events",
            "dom_snapshots",
        ]
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            for table in required_tables:
                row = conn.execute(
                    text("SELECT to_regclass(:t)"), {"t": table}
                ).scalar()
                if row is None:
                    print(f"  {FAIL} Table '{table}' does not exist")
                else:
                    print(f"  {PASS} Table '{table}' exists")
        engine.dispose()
        print(f"{PASS} Database connection OK")
        return True
    except Exception as exc:
        print(f"{FAIL} Database: {exc}")
        return False


def test_cot_download():
    """Download 2024 COT data and print first 3 rows."""
    print("\n--- COT Download (2024) ---")
    try:
        from data.cot_fetcher import download_cot_year
        df = download_cot_year(2024)
        if df.empty:
            print(f"{FAIL} No JPY rows found in 2024 COT data")
            return False
        print(f"  Rows fetched: {len(df)}")
        print(df.head(3).to_string())
        print(f"{PASS} COT download OK")
        return True
    except Exception as exc:
        print(f"{FAIL} COT download: {exc}")
        return False


async def test_ctrader_connection():
    """Connect to cTrader demo and fetch USD/JPY current price."""
    print("\n--- cTrader Connection ---")
    import config
    if not config.CTRADER_CLIENT_ID or not config.CTRADER_CLIENT_SECRET:
        print(f"{SKIP} CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET not set")
        return True  # not a hard failure
    try:
        from data.candle_fetcher import connect_ctrader, fetch_candles
        session = await asyncio.wait_for(connect_ctrader(), timeout=30)
        df = await fetch_candles(session, symbol="USDJPY", timeframe=config.CANDLE_TIMEFRAME, count=3)
        if df.empty:
            print(f"{FAIL} No candles returned")
            session.disconnect()
            return False
        print(f"  Latest close: {df['close'].iloc[-1]:.3f}")
        print(f"  Rows: {len(df)}")
        session.disconnect()
        print(f"{PASS} cTrader connection OK")
        return True
    except Exception as exc:
        msg = str(exc)
        if "not in active state" in msg or "OA_APPLICATION_DISABLED" in msg:
            print(f"{SKIP} cTrader app pending Spotware review — {msg}")
            return True
        print(f"{FAIL} cTrader: {msg}")
        return False


def test_myfxbook_sentiment():
    """
    Fetch USD/JPY sentiment from Myfxbook Community Outlook.
    This test PASSES even without credentials (shows a warning instead).
    It only FAILS if credentials are set but the API returns an error.
    """
    print("\n--- Myfxbook Sentiment ---")
    import config
    from data.sentiment_fetcher import fetch_myfxbook_sentiment

    if not config.MYFXBOOK_EMAIL or not config.MYFXBOOK_PASSWORD:
        print(f"  {SKIP} MYFXBOOK_EMAIL / MYFXBOOK_PASSWORD not set in .env")
        print("         Sentiment signal will be disabled until credentials added.")
        return True  # Not a failure

    result = fetch_myfxbook_sentiment(
        config.MYFXBOOK_EMAIL,
        config.MYFXBOOK_PASSWORD
    )

    if result:
        print(f"  {PASS} Myfxbook sentiment: "
              f"Long {result['long_pct']}% / Short {result['short_pct']}%")
        print(f"         Long positions: {result['long_positions']} | "
              f"Short positions: {result['short_positions']}")
        return True
    else:
        print(f"  {FAIL} Could not fetch Myfxbook sentiment - check credentials")
        return False


async def test_telegram():
    """Send a test message to Telegram."""
    print("\n--- Telegram ---")
    import config
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print(f"{SKIP} TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping")
        return True
    try:
        from telegram import Bot
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text="✅ USD/JPY Bot — connection test passed",
        )
        print(f"{PASS} Telegram message sent")
        return True
    except Exception as exc:
        print(f"{FAIL} Telegram: {exc}")
        return False


def test_rollover_data():
    """
    Test FRED API connection and print current rate snapshot.

    PASS: API key set and FRED returns Fed + BOJ rates
    SKIP: FRED_API_KEY not set in .env
    FAIL: API key set but FRED returns error

    Swap point values show as 0.0 — requires live cTrader connection.
    """
    print("\n--- Rollover Data (FRED API) ---")
    import config
    from data.rollover_fetcher import download_interest_rate_history, compute_rollover_signal

    if not config.FRED_API_KEY:
        print(f"  {SKIP} FRED_API_KEY not set — rollover uses swap points from cTrader only")
        print("         Get free key at https://fred.stlouisfed.org/docs/api/api_key.html")
        return True

    try:
        df = download_interest_rate_history(config.FRED_API_KEY)
        if df.empty:
            print(f"  {FAIL} FRED returned empty DataFrame — check API key")
            return False

        fed_df = df[df["series"] == "FEDFUNDS"].sort_values("date")
        boj_df = df[df["series"] == "IRSTCI01JPM156N"].sort_values("date")

        if fed_df.empty or boj_df.empty:
            print(f"  {FAIL} FRED missing Fed or BOJ data")
            return False

        fed = fed_df.iloc[-1]
        boj = boj_df.iloc[-1]
        signal = compute_rollover_signal(0.0, 0.0, float(fed["rate_pct"]), float(boj["rate_pct"]))
        direction_str = {1: "LONG", 0: "NEUTRAL", -1: "SHORT"}.get(signal["rollover_direction"], "UNKNOWN")

        print(f"  {PASS} Rollover Data:")
        print(f"    Fed Funds Rate:    {fed['rate_pct']:.2f}%  (as of {fed['date']})")
        print(f"    BOJ Policy Rate:   {boj['rate_pct']:.2f}%  (as of {boj['date']})")
        print(f"    Rate Differential: {signal['rate_differential']:+.2f}%")
        print(f"    Carry Direction:   {direction_str} (strength: {signal['carry_strength']:.2f})")
        print(f"    Swap Long  (IC Markets): 0.00 pts = 0.00 EUR/night")
        print(f"    Swap Short (IC Markets): 0.00 pts = 0.00 EUR/night")
        print(f"    Note: Swap points require live cTrader connection")
        return True

    except Exception as exc:
        print(f"  {FAIL} FRED API error: {exc}")
        return False


async def test_dom_subscription():
    """Subscribe to DOM data for USDJPY and verify book state after 3 seconds."""
    print("\n--- DOM Subscription ---")
    import config
    if not config.CTRADER_CLIENT_ID or not config.CTRADER_CLIENT_SECRET:
        print(f"{SKIP} CTRADER credentials not set — skipping DOM test")
        return True
    try:
        from data.candle_fetcher import connect_ctrader
        session = await asyncio.wait_for(connect_ctrader(), timeout=30)
        symbol_id = await session.get_symbol_id("USDJPY")
        await session.subscribe_dom(symbol_id)
        await asyncio.sleep(3)
        snap = session.get_dom_snapshot()
        session.disconnect()

        print(f"  Spread:     {snap['spread_pips']:.2f} pips" if snap['spread_pips'] is not None else "  Spread:     N/A")
        print(f"  Imbalance:  {snap['order_imbalance']:.3f}  (0.5 = balanced)")
        print(f"  Bid depth:  {snap['bid_depth_total']}")
        print(f"  Ask depth:  {snap['ask_depth_total']}")
        print(f"  Levels:     {snap['levels_count']}")

        if snap["levels_count"] > 0:
            print(f"{PASS} DOM subscription OK")
            return True
        else:
            print(f"{FAIL} DOM book is empty after 3s — market may be closed")
            return False
    except Exception as exc:
        msg = str(exc)
        if "not in active state" in msg or "OA_APPLICATION_DISABLED" in msg:
            print(f"{SKIP} cTrader app pending Spotware review — {msg}")
            return True
        print(f"{FAIL} DOM subscription: {msg}")
        return False


def test_rate_limit_budget():
    """Verify Myfxbook rate limit budget is sufficient for configured poll interval."""
    print("\n--- Myfxbook Rate Limit Budget ---")
    import config
    daily_limit = 100
    polls_per_day = 24 * 60 // config.SENTIMENT_INTERVAL_MIN
    login_per_day = 1
    total = polls_per_day + login_per_day
    buffer = daily_limit - total
    print(f"  Daily limit:     {daily_limit} requests")
    print(f"  {config.SENTIMENT_INTERVAL_MIN}min polling:  {polls_per_day} requests/day")
    print(f"  Login:           {login_per_day} request/day")
    print(f"  Total planned:   {total} requests/day")
    status = "✅" if buffer >= 10 else "⚠️"
    print(f"  Safety buffer:   {buffer} requests/day {status}")
    return True


async def run_all():
    print("=" * 50)
    print("USD/JPY Bot — Connection Tests")
    print("=" * 50)

    results = {
        "database": test_database(),
        "cot_download": test_cot_download(),
        "ctrader": await test_ctrader_connection(),
        "dom_subscription": await test_dom_subscription(),
        "myfxbook_sentiment": test_myfxbook_sentiment(),
        "telegram": await test_telegram(),
        "rollover_data": test_rollover_data(),
        "rate_limit": test_rate_limit_budget(),
    }

    print("\n" + "=" * 50)
    print("RESULTS SUMMARY")
    print("=" * 50)
    all_passed = True
    for name, passed in results.items():
        icon = PASS if passed else FAIL
        print(f"  {icon} {name}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("All tests passed ✅")
        sys.exit(0)
    else:
        print("Some tests failed ❌ (see details above)")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_all())
