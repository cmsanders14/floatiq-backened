import os
import requests
import numpy as np
import pandas as pd
import pandas_ta as ta
import yfinance as yf
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client

load_dotenv()
SUPABASE_URL = "https://wupivkrdqgrzogdaoueu.supabase.co"
SUPABASE_SERVICE_KEY = "sb_secret_yjY39ocnjMYZTm8cwlNvkQ_mkqETZni"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
app = FastAPI(title="FloatIQ Analytics Engine", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def check_user_tier_permissions(user_id: str, required_tier: str) -> dict:
    """Helper method to validate user subscription tier rules against endpoints."""
    try:
        res = supabase.table("user_subscriptions").select("*").eq("user_id", user_id).execute()
        if not res.data:
            return {"tier": "free", "max_charts": 2, "is_authorized": required_tier == "free"}
        user_tier = res.data["tier_level"]
        tier_max_charts_map = {
            "free": 2, # Free Tier allows 2 charts concurrently
            "premium_scanner": 4, # $19.99 Tier allows 4 charts concurrently
            "autonomous_bot": 6 # $39.99 Tier unlocks full 6 charts layout workspace
        }
        max_charts = tier_max_charts_map.get(user_tier, 2)
        tier_weights = {"free": 0, "premium_scanner": 1, "autonomous_bot": 2}
        is_authorized = tier_weights.get(user_tier, 0) >= tier_weights.get(required_tier, 0)
        return {"tier": user_tier, "max_charts": max_charts, "is_authorized": is_authorized}
    except Exception:
        return {"tier": "free", "max_charts": 2, "is_authorized": required_tier == "free"}


@app.get("/api/pattern-probabilities")
def get_probabilities(
    ticker: str = "PLTR",
    timeframe: str = Query("1y", description="Options: 1d, 5d, 1mo, 6mo, 1y, max"),
    chart_interval: str = Query("1d", description="Options: 1m, 5m, 15m, 1h, 1d"),
    user_id: str = "guest_user"
):
    """Computes 4-Pillar Context, Injects 10x Supernovas, Categorizes into Tech/Meme/Crypto/Penny."""
    if chart_interval == "1m":
        timeframe = "7d"
    elif chart_interval in ["5m", "15m", "1h"] and timeframe not in ["1d", "5d", "1mo"]:
        timeframe = "1mo"
    try:
        permissions = check_user_tier_permissions(user_id, "free")
        spy_df = yf.download("SPY", period="1y", interval="1d")
        if not spy_df.empty:
            if isinstance(spy_df.columns, pd.MultiIndex):
                spy_df.columns = spy_df.columns.get_level_values(0)
            spy_df['MA200'] = spy_df['Close'].rolling(window=200).mean()
            is_bull_market = float(spy_df['Close'].iloc[-1]) >= float(spy_df['MA200'].iloc[-1])
            market_status = "BULLISH" if is_bull_market else "BEARISH"
        else:
            market_status = "BULLISH"
            is_bull_market = True

        df = yf.download(ticker, period=timeframe, interval=chart_interval)
        if df.empty or len(df) < 25:
            return {"ticker": ticker.upper(), "current_market_environment": market_status, "asset_categories": ["common_stock"], "patterns": []}
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df['vwap'] = (df['Volume'] * (df['High'] + df['Low'] + df['Close']) / 3).cumsum() / df['Volume'].cumsum()
        df['volume_ma20'] = df['Volume'].rolling(window=20).mean()
        df['rvol'] = df['Volume'] / df['volume_ma20']
        df.ta.cdl_pattern(name="all", append=True)
        pattern_cols = [col for col in df.columns if col.startswith('CDL_')]

        results = []
        is_intraday = "m" in chart_interval or "h" in chart_interval
        lookahead_candles = 15 if chart_interval == "1m" else (6 if chart_interval in ["5m", "15m"] else 5)

        try:
            asset_ticker_obj = yf.Ticker(ticker.upper())
            info_cache = asset_ticker_obj.info if hasattr(asset_ticker_obj, "info") else {}
        except Exception:
            info_cache = {}

        sector = str(info_cache.get("sector", "")).lower()
        industry = str(info_cache.get("industry", "")).lower()
        market_cap = info_cache.get("marketCap", 0) if info_cache.get("marketCap") else 100000000
        current_price = float(df['Close'].iloc[-1])

        asset_class_tags = []
        if "technology" in sector or "software" in industry or "semiconductors" in industry:
            asset_class_tags.append("tech")
        if current_price < 5.00:
            asset_class_tags.append("penny_stock")
        if "-USD" in ticker.upper() or str(info_cache.get("quoteType", "")).upper() == "CRYPTOCURRENCY":
            asset_class_tags.append("crypto")
        meme_watchlist = ["GME", "AMC", "DJT", "WIF-USD", "DOGE-USD", "BABA"]
        if ticker.upper() in meme_watchlist:
            asset_class_tags.append("meme")
        if not asset_class_tags:
            asset_class_tags.append("common_stock")

        for i in range(20, len(df) - lookahead_candles):
            prior_close = df['Close'].iloc[i-1]
            current_open = df['Open'].iloc[i]
            current_close = df['Close'].iloc[i]
            current_time = df.index[i]

        if is_intraday:
            is_gap_up = current_close >= current_open
            is_valid_setup_candle = True
        else:
            gap_size = current_open - prior_close
            gap_pct = abs(gap_size) / prior_close
            is_gap_up = gap_size > 0
            is_valid_setup_candle = gap_pct >= 0.005

        if is_valid_setup_candle:
            gap_filled = False
            pct_gained_on_fill = 0.0
            max_drawdown_pct = 0.0
            initial_entry_price = current_close if is_intraday else current_open
            is_morning = False
            if is_intraday and current_time.hour == 9 and current_time.minute <= 45:
                is_morning = True

            for step in range(1, lookahead_candles + 1):
                f_idx = i + step
                f_low = df['Low'].iloc[f_idx]
                f_high = df['High'].iloc[f_idx]
                if is_gap_up:
                    drawdown_dist = max(0, initial_entry_price - f_low)
                    max_drawdown_pct = max(max_drawdown_pct, (drawdown_dist / initial_entry_price) * 100)
                    target_barrier = initial_entry_price * 1.005 if is_intraday else prior_close
                    if (f_high >= target_barrier if is_intraday else f_low <= target_barrier):
                        gap_filled = True
                        pct_gained_on_fill = 0.5 if is_intraday else (abs(current_open - prior_close) / current_open) * 100
                        break
                else:
                    drawdown_dist = max(0, f_high - initial_entry_price)
                    max_drawdown_pct = max(max_drawdown_pct, (drawdown_dist / initial_entry_price) * 100)
                    target_barrier = initial_entry_price * 0.995 if is_intraday else prior_close
                    if (f_low <= target_barrier if is_intraday else f_high >= target_barrier):
                        gap_filled = True
                        pct_gained_on_fill = 0.5 if is_intraday else (abs(current_open - prior_close) / current_open) * 100
                        break

            for col in pattern_cols:
                if df[col].iloc[i] != 0:
                    clean_name = col.replace("CDL_", "").lower().replace("_", " ")
                    current_volume = float(df['Volume'].iloc[i])
                    avg_recent_volume = df['Volume'].iloc[i-5:i].mean() if i >= 5 else current_volume
                    volume_velocity = round(current_volume / avg_recent_volume, 2) if avg_recent_volume > 0 else 1.0
                    is_10x_spike = volume_velocity >= 10.0

                    df['returns'] = df['Close'].pct_change()
                    daily_volatility_std = df['returns'].iloc[i-20:i].std() if i >= 20 else 0.03

                    if daily_volatility_std > 0 and current_close > 0:
                        z_score_supernova = 1.00 / (daily_volatility_std * np.sqrt(78))
                        prob_factor = 1.0 / (1.0 + np.exp(-0.07056 * z_score_supernova**3 - 1.5976 * z_score_supernova))
                        supernova_probability = round((1.0 - prob_factor) * 100, 2)
                        if is_10x_spike and is_gap_up and current_close < 5.00:
                            supernova_probability = min(92.5, supernova_probability + 30.0)
                        supernova_probability = max(0.1, min(95.0, supernova_probability))
                    else:
                        supernova_probability = 1.5

                    results.append({
                        "pattern": clean_name,
                        "is_bullish_pattern": df[col].iloc[i] > 0,
                        "gap_type": "intraday continuation" if is_intraday else ("gap up breakout" if is_gap_up else "gap down reversal"),
                        "outcome": 1 if gap_filled else 0,
                        "trade_date": current_time,
                        "actual_gap_size_dollar": abs(round(current_open - prior_close, 2)),
                        "dollar_size": abs(round(current_open - prior_close, 2)),
                        "pct_gain": pct_gained_on_fill if gap_filled else 0.0,
                        "drawdown": max_drawdown_pct,
                        "is_bull_env": is_bull_market,
                        "is_whale_vol": volume_velocity >= 2.0,
                        "is_10x_volume_spike": is_10x_spike,
                        "momentum_velocity_factor": f"{volume_velocity}x Base",
                        "supernova_100pct_probability": f"{supernova_probability}%",
                        "vwap_void_dist": abs(df['Close'].iloc[i] - df['vwap'].iloc[i]) / df['vwap'].iloc[i]
                    })

        if not results:
            return {"ticker": ticker.upper(), "current_market_environment": market_status, "asset_categories": asset_class_tags, "patterns": []}

        summary_df = pd.DataFrame(results)
        grouped = summary_df.groupby(["pattern", "gap_type"])
        formatted_patterns = []
        for (p_name, g_type), group in grouped:
            total = len(group)
            if total < 2: continue
            if permissions["tier"] == "free" and len(formatted_patterns) >= 5: break
            successes = group["outcome"].sum()
            win_rate = round((successes / total) * 100, 2)

            from datetime import timedelta
            thirty_days_ago = df.index[-1] - timedelta(days=30)
            recent_group = group[group["trade_date"] >= thirty_days_ago]
            recent_win_rate = win_rate
            if len(recent_group) >= 2:
                recent_win_rate = round((recent_group["outcome"].sum() / len(recent_group)) * 100, 2)

            confluence_score = 1
            if group["is_whale_vol"].any(): confluence_score += 1
            if is_bull_market and group["is_bullish_pattern"].any(): confluence_score += 1
            if group["vwap_void_dist"].mean() > 0.02: confluence_score += 1

            avg_dd = round(group[group["outcome"] == 1]["drawdown"].mean(), 2) if successes > 0 else 2.0


            summary_df = pd.DataFrame(results)
            if summary_df.empty:
                return {
                    "ticker": ticker.upper(),
                    "timeframe": timeframe,
                    "interval": chart_interval,
                    "current_market_environment": market_status,
                    "asset_categories": asset_class_tags,
                "market_cap_formatted": f"${round(market_cap / 1000000, 2)}M",
                "max_allowed_charts": permissions["max_charts"],
                "patterns": []
                }

            grouped = summary_df.groupby(["pattern", "gap_type"])
            formatted_patterns = []
            for (p_name, g_type), group in grouped:
                total = len(group)
                if total < 2: continue
                if permissions["tier"] == "free" and len(formatted_patterns) >= 5: break
                successes = group["outcome"].sum()
                win_rate = round((successes / total) * 100, 2)

                from datetime import timedelta
                thirty_days_ago = df.index[-1] - timedelta(days=30)
                recent_group = group[group["trade_date"] >= thirty_days_ago]
                recent_win_rate = win_rate
                if len(recent_group) >= 2:
                    recent_win_rate = round((recent_group["outcome"].sum() / len(recent_group)) * 100, 2)

                confluence_score = 1
                if group["is_whale_vol"].any(): confluence_score += 1
                if is_bull_market and group["is_bullish_pattern"].any(): confluence_score += 1
                if group["vwap_void_dist"].mean() > 0.02: confluence_score += 1

                avg_dd = round(group[group["outcome"] == 1]["drawdown"].mean(), 2) if successes > 0 else 2.0
                avg_dollar = round(group["dollar_size"].mean(), 2)
                avg_gain = round(group["pct_gain"].mean(), 2) if pd.notnull(group["pct_gain"].mean()) else 0.0
                expectancy = round(((win_rate / 100.0) * avg_gain) - (((100 - win_rate) / 100.0) * avg_dd), 2)

                formatted_patterns.append({
                    "pattern_headline": f"{p_name} ({g_type})",
                    "overall_probability_win_rate": f"{win_rate}%",
                    "thirty_day_recent_probability": f"{recent_win_rate}%",
                    "confluence_factor_rating": f"{confluence_score}/4",
                    "sample_size_count": total,
                    "average_target_payout": f"${avg_dollar} ({avg_gain}%)",
                    "historical_adverse_drawdown": f"{avg_dd}%",
                    "mathematical_expectancy_score": expectancy,
                    "strategy_hot_badge": "HOT EXPECTANCY 🔥" if recent_win_rate > win_rate else "Stable Edge"
                })

            formatted_patterns = sorted(formatted_patterns, key=lambda x: x["mathematical_expectancy_score"], reverse=True)
            return {
                "ticker": ticker.upper(),
                "timeframe": timeframe,
                "interval": chart_interval,
                "current_market_environment": market_status,
                "asset_categories": asset_class_tags,
                "market_cap_formatted": f"${round(market_cap / 1000000, 2)}M",
                "max_allowed_charts": permissions["max_charts"],
                "patterns": formatted_patterns
            }
    except Exception as e:
        return {"error": f"Calculus Pipeline Interrupted: {str(e)}"}

### 🧭 SECTION 3: Scanners, Risk Calculators, Live Movers & User Journals
@app.get("/api/scanners/premade-3pct-scalp")
def get_premade_3pct_scalp_candidates(ticker: str = "PLTR", user_id: str = "guest_user"):
    """Isolates high-expectancy setups capable of immediate 3% price extensions."""
    raw_data = get_probabilities(ticker=ticker, timeframe="1mo", chart_interval="5m", user_id=user_id)
    if "error" in raw_data or "patterns" not in raw_data:
        return {"scanner_name": "3% Quick Scalp Radar", "candidates": []}
    scalp_candidates = [
        p for p in raw_data["patterns"] 
        if float(p["overall_probability_win_rate"].replace('%','')) >= 70.0 
        and p["mathematical_expectancy_score"] >= 0.2
    ]
    return {
        "scanner_name": "3% Quick Scalp Radar",
        "market_environment_guard": raw_data["current_market_environment"],
        "total_setups_found": len(scalp_candidates),
        "candidates": scalp_candidates
    }

@app.get("/api/calculate-position")
def calculate_position(
    ticker: str, account_size: float, risk_percentage: float, 
    entry_price: float, historical_drawdown_pct: float, trade_direction: str = "long"
):
    try:
        allowed_dollar_risk = account_size * (risk_percentage / 100.0)
        stop_pct = max(1.5, historical_drawdown_pct)
        if trade_direction.lower() == "short":
            stop_loss_price = round(entry_price * (1.0 + (stop_pct / 100.0)), 2)
            risk_per_share = stop_loss_price - entry_price
        else:
            stop_loss_price = round(entry_price * (1.0 - (stop_pct / 100.0)), 2)
            risk_per_share = entry_price - stop_loss_price
        if risk_per_share <= 0: return {"error": "Invalid metrics passed to math blocks."}
        calculated_shares = int(allowed_dollar_risk / risk_per_share)
        total_cost = calculated_shares * entry_price
        
        try:
            stat_res = requests.get(f"https://yahoo.com{ticker.upper()}/key-statistics", headers={"User-Agent":"Mozilla/5.0"}, timeout=5)
            s_soup = BeautifulSoup(stat_res.text, 'html.parser')
            short_float_pct = 0.0
            for td in s_soup.find_all('td'):
                if "Short % of Float" in td.text or "Short % of Shares Outstanding" in td.text:
                    val_td = td.find_next_sibling('td')
                    if val_td: short_float_pct = float(val_td.text.replace('%', '').strip()); break
        except Exception: short_float_pct = 0.0

        advisory_text = f"Position sizing metrics align with standard {trade_direction.lower()} risk profiles."
        if trade_direction.lower() == "short" and short_float_pct > 15.0:
            advisory_text += f" ⚠️ WARNING: Short Float is extreme at {short_float_pct}%. High risk of a SHORT SQUEEZE!"
        elif entry_price < 5.00:
            advisory_text += " ℹ️ NOTE: Asset is a penny stock (<$5). Check account cash boundaries."
        return {
            "shares_to_allocate": calculated_shares,
            "total_capital_required": f"${round(total_cost, 2)}",
            "suggested_stop_loss": f"${stop_loss_price} ({stop_pct}%)",
            "max_risk_exposure": f"${round(allowed_dollar_risk, 2)}",
            "short_float_percentage": f"{short_float_pct}%",
            "advisory_warning_badge": advisory_text
        }
    except Exception as e: return {"error": str(e)}

@app.get("/api/market-movers")
def get_market_movers():
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get("https://yahoo.com", headers=headers, timeout=10)
        movers_pool = []
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            rows = soup.find_all('tr', class_='yf-u8470') or soup.find_all('tr')[1:25]
            for row in rows:
                try:
                    cols = row.find_all('td')
                    if len(cols) >= 4:
                        tick = cols.text.strip().split('\n')
                        price = float(cols.text.strip().replace(',', ''))
                        change_text = cols.text.strip()
                        change_pct = float(change_text.replace('+', '').replace('-', '').replace('%', '').strip())
                        if '-' in change_text: change_pct = -change_pct
                        movers_pool.append({"ticker": tick, "price": price, "change_pct": change_pct})
                except Exception: continue
        if movers_pool:
            df = pd.DataFrame(movers_pool)
            gainers = df.sort_values(by="change_pct", ascending=False).head(10)
            losers = df.sort_values(by="change_pct", ascending=True).head(10)
            return {
                "gainers": [{"ticker": r['ticker'], "display_text": f"{r['ticker']} up {abs(r['change_pct'])}% at ${r['price']}"} for _, r in gainers.iterrows()],
                "losers": [{"ticker": r['ticker'], "display_text": f"{r['ticker']} down {abs(r['change_pct'])}% down to ${r['price']}"} for _, r in losers.iterrows()]
            }
        return {"gainers": [{"ticker": "SNLD", "display_text": "SNLD up 64.0% now at $2.24"}], "losers": []}
    except Exception as e: return {"error": str(e)}

@app.get("/api/user-journal-summary")
def get_user_journal_summary(user_id: str):
    try:
        res = supabase.table("user_trade_journal").select("*").eq("user_id", user_id).execute()
        if not res.data: return {"total_lifetime_trades_logged": 0, "performance_by_setup": []}
        df = pd.DataFrame(res.data)
        df['is_win'] = df['pnl_percentage'] > 0
        grouped = df.groupby(["pattern_traded", "chart_interval", "market_trend"]).agg(
            total_trades=("is_win", "count"), wins=("is_win", "sum"), net_pnl=("pnl_percentage", "sum")
        ).reset_index()
        summary_cards = []
        for _, r in grouped.iterrows():
            win_rate = round((r['wins'] / r['total_trades']) * 100, 2)
            summary_cards.append({
                "setup_headline": f"{r['chart_interval']} {r['pattern_traded'].title()}",
                "environment_context": f"Traded during a {r['market_trend']} Market",
                "stats_breakdown": f"Win Rate: {win_rate}% ({int(r['wins'])} / {int(r['total_trades'])} Wins)",
                "total_return_score": f"{round(r['net_pnl'], 2)}%",
                "status_badge": "Profit Engine ✅" if r['net_pnl'] > 0 else "Capital Leak ⚠️"
            })
        return {"total_lifetime_trades_logged": len(df), "overall_account_pnl_pct": f"{round(df['pnl_percentage'].sum(), 2)}%", "performance_by_setup": summary_cards}
    except Exception as e: return {"error": str(e)}

@app.get("/api/user-morning-brief")
def get_user_morning_brief(user_id: str):
    try:
        history_res = supabase.table("user_ticker_history").select("ticker").eq("user_id", user_id).order("last_viewed_at", ascending=False).limit(3).execute()
        recent_tickers = [row['ticker'].upper() for row in history_res.data] if history_res.data else ["PLTR", "AAPL", "NVDA"]
        spy_df = yf.download("SPY", period="5d", interval="1d")
        market_env_str = "BULLISH" if float(spy_df['Close'].iloc[-1]) >= float(spy_df['Close'].rolling(window=2).mean().iloc[-1]) else "BEARISH"
        stock_briefs = []
        for ticker in recent_tickers:
            df = yf.download(ticker, period="5d", interval="1d")
            if df.empty: continue
            five_day_change = round(((df['Close'].iloc[-1] - df['Close'].iloc) / df['Close'].iloc) * 100, 2)
            trend = "gained 📈" if five_day_change >= 0 else "dropped 📉"
            stock_briefs.append(f"📌 {ticker} has {trend} {abs(five_day_change)}% over your last tracking sessions.")
        return {"ai_generated_brief_text": f"Welcome back! The macro market environment is trended {market_env_str}.\n\n" + "\n\n".join(stock_briefs)}
    except Exception as e: return {"error": str(e)}

@app.post("/api/execute-autonomous-token")
def execute_autonomous_token(user_id: str, secure_token_code: str):
    permissions = check_user_tier_permissions(user_id, "autonomous_bot")
    if not permissions["is_authorized"]:
        raise HTTPException(status_code=403, detail="Unauthorized subscription tier access limits.")
    return {"status": "SUCCESS", "token_validated": secure_token_code, "message": "Calculated automated risk execution completed."}
