import os
import time
import threading
import requests
import ccxt
import pandas as pd
import numpy as np
from flask import Flask, render_template, request, jsonify

# --- 1. პროფესიონალური კონფიგურაცია ---
CONFIG = {
    # "Gatekeeper" - ჯანსაღი ბაზრის ფილტრები
    "min_daily_volume_usdt": 20_000_000,
    "min_history_days": 120,

    # სტრატეგია #1: "ელიტური სნაიპერი"
    "elite_structure_tf": "1d",
    "elite_zone_tf": "4h",
    "elite_entry_tf": "1h",
    "elite_swing_lookback": 5,
    "elite_sl_buffer_percent": 0.05,
    "elite_rr_ratio": 3.0,

    # სტრატეგია #2: "ტრენდის პატრული"
    "patrol_trend_tf": "4h",
    "patrol_entry_tf": "15m",
    "patrol_supertrend_atr": 10,
    "patrol_supertrend_multiplier": 3.0,
    "patrol_ema_period": 21,
    "patrol_sl_buffer_percent": 0.05,
    "patrol_rr_ratio": 1.8,

    # ტექნიკური პარამეტრები
    "ohlcv_limit": 200,
    "api_call_delay": 0.3
}

# --- 2. Telegram-ის მონაცემები ---
BOT_TOKEN = "8158204187:AAFPEApXyE_ot0pz3J23b1h5ubJ82El5gLc"
CHAT_ID = "7465722084"

app = Flask(__name__)
exchange = ccxt.binanceusdm({'options': {'defaultType': 'future'}})

# --- 3. გლობალური სტატუსი ---
status = {
    "running": False, "current_phase": "Idle", "whitelist_count": 0,
    "symbols_total": 0, "symbols_scanned": 0, "last_scan_time": "N/A"
}

# --- 4. სერვისები ---
def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        requests.post(url, data=data, timeout=10).raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Telegram შეცდომა: {e}")

# --- 5. "The Gatekeeper" ---
def get_healthy_symbols_whitelist():
    # ... (ეს ფუნქცია უცვლელია) ...
    status["current_phase"] = "Filtering markets..."
    whitelist = []
    try:
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
        for symbol in tickers:
            if symbol.endswith(':USDT'):
                ticker_data = tickers[symbol]
                if ticker_data.get('quoteVolume', 0) > CONFIG["min_daily_volume_usdt"]:
                    try:
                        ohlcv = exchange.fetch_ohlcv(symbol, '1d', limit=CONFIG["min_history_days"])
                        if len(ohlcv) >= CONFIG["min_history_days"]:
                            whitelist.append(symbol)
                    except Exception: pass
    except Exception as e:
        print(f"Gatekeeper შეცდომა: {e}")
    status["whitelist_count"] = len(whitelist)
    print(f"Gatekeeper: თეთრი სია დასრულებულია. შეირჩა {len(whitelist)} სიმბოლო.")
    return whitelist

# --- 6. ანალიტიკური ფუნქციები ---
# (ელიტური სნაიპერის ფუნქციები უცვლელია)
def find_swing_points(df, lookback):
    highs = df['high'].rolling(window=2*lookback+1, center=True).apply(lambda x: x.argmax() == lookback, raw=True)
    lows = df['low'].rolling(window=2*lookback+1, center=True).apply(lambda x: x.argmin() == lookback, raw=True)
    df['swing_high'] = np.where(highs, df['high'], np.nan)
    df['swing_low'] = np.where(lows, df['low'], np.nan)
    return df

def get_market_structure(df):
    swings = find_swing_points(df, CONFIG['elite_swing_lookback'])
    swing_highs, swing_lows = swings['swing_high'].dropna().iloc[-2:], swings['swing_low'].dropna().iloc[-2:]
    if len(swing_highs) < 2 or len(swing_lows) < 2: return "INDECISIVE"
    if swing_highs.iloc[-1] > swing_highs.iloc[-2] and swing_lows.iloc[-1] > swing_lows.iloc[-2]: return "BULLISH"
    if swing_highs.iloc[-1] < swing_highs.iloc[-2] and swing_lows.iloc[-1] < swing_lows.iloc[-2]: return "BEARISH"
    return "INDECISIVE"

def find_order_block(df, trend):
    for i in range(len(df) - 2, 0, -1):
        is_bullish_ob = df['close'][i-1] < df['open'][i-1] and df['close'][i+1] > df['high'][i-1]
        is_bearish_ob = df['close'][i-1] > df['open'][i-1] and df['close'][i+1] < df['low'][i-1]
        if trend == "BULLISH" and is_bullish_ob: return {'low': df['low'][i-1], 'high': df['high'][i-1]}
        if trend == "BEARISH" and is_bearish_ob: return {'low': df['low'][i-1], 'high': df['high'][i-1]}
    return None

def check_elite_confirmation(df, trend, zone):
    price = df['close'].iloc[-1]
    if not (zone['low'] <= price <= zone['high']): return False
    swings = find_swing_points(df, 3)
    if trend == "BULLISH":
        last_local_highs = swings['swing_high'].dropna()
        if not last_local_highs.empty and price > last_local_highs.iloc[-1]: return True
    elif trend == "BEARISH":
        last_local_lows = swings['swing_low'].dropna()
        if not last_local_lows.empty and price < last_local_lows.iloc[-1]: return True
    return False

def calculate_supertrend(df, atr_period, multiplier):
    # ... (ეს ფუნქცია უცვლელია) ...
    hl2 = (df['high'] + df['low']) / 2
    atr = df['high'].rolling(atr_period).mean() - df['low'].rolling(atr_period).mean() # Using 'ta' library's ATR can be slow, simple ATR calculation
    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)
    df['supertrend'] = True
    for i in range(1, len(df.index)):
        if df['close'][i] > upperband[i-1]: df.loc[df.index[i], 'supertrend'] = True
        elif df['close'][i] < lowerband[i-1]: df.loc[df.index[i], 'supertrend'] = False
        else: df.loc[df.index[i], 'supertrend'] = df['supertrend'][i-1]
    return df

def check_patrol_status(df):
    df['ema'] = ta.trend.ema_indicator(df['close'], window=CONFIG['patrol_ema_period'])
    last, prev = df.iloc[-1], df.iloc[-2]
    
    # პოტენციური სეთაფის შემოწმება (შეხება EMA-ზე)
    is_at_ema_buy = last['low'] <= last['ema'] and last['close'] > last['ema']
    is_at_ema_sell = last['high'] >= last['ema'] and last['close'] < last['ema']
    
    # დადასტურების შემოწმება (შერბილებული შთანთქმა)
    is_bullish_engulfing = is_at_ema_buy and last['close'] > last['open'] and last['close'] > prev['open'] and last['open'] < prev['close']
    is_bearish_engulfing = is_at_ema_sell and last['close'] < last['open'] and last['close'] < prev['open'] and last['open'] > prev['close']
    
    if is_bullish_engulfing:
        return "CONFIRMED_BUY", {'low': min(last['low'], prev['low'])}
    if is_bearish_engulfing:
        return "CONFIRMED_SELL", {'high': max(last['high'], prev['high'])}
        
    if is_at_ema_buy: return "WATCHLIST_BUY", None
    if is_at_ema_sell: return "WATCHLIST_SELL", None

    return None, None

# --- 7. მთავარი სკანირების ციკლი ---
def scan_loop():
    status["running"] = True
    whitelist = get_healthy_symbols_whitelist()
    status["symbols_total"] = len(whitelist)

    while status["running"]:
        start_time = time.time()
        elite_signals, patrol_signals, watchlist = [], [], []
        status["current_phase"] = "Scanning for opportunities..."

        for i, symbol in enumerate(whitelist):
            if not status["running"]: break
            status["symbols_scanned"] = i + 1
            try:
                # --- სტრატეგია #1: "ელიტური სნაიპერი" (ლოგიკა უცვლელია) ---
                # ...
                
                # --- სტრატეგია #2: "ტრენდის პატრული" (განახლებული ლოგიკა) ---
                df_4h_patrol = pd.DataFrame(exchange.fetch_ohlcv(symbol, CONFIG["patrol_trend_tf"], limit=50))
                df_4h_patrol.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
                df_4h_patrol = calculate_supertrend(df_4h_patrol, CONFIG['patrol_supertrend_atr'], CONFIG['patrol_supertrend_multiplier'])
                patrol_trend_is_up = df_4h_patrol['supertrend'].iloc[-1]

                df_15m = pd.DataFrame(exchange.fetch_ohlcv(symbol, CONFIG["patrol_entry_tf"], limit=50))
                df_15m.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
                patrol_status, pattern_range = check_patrol_status(df_15m)

                link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol.replace('/', '').replace(':USDT', '')}.P"
                
                # თუ სიგნალი დადასტურდა
                if patrol_status in ["CONFIRMED_BUY", "CONFIRMED_SELL"]:
                    signal_type = "BUY" if patrol_status == "CONFIRMED_BUY" else "SELL"
                    if (patrol_trend_is_up and signal_type == "BUY") or (not patrol_trend_is_up and signal_type == "SELL"):
                        entry_price = df_15m['close'].iloc[-1]
                        if signal_type == "BUY":
                            stop_loss = pattern_range['low'] * (1 - CONFIG["patrol_sl_buffer_percent"] / 100)
                            take_profit = entry_price + (entry_price - stop_loss) * CONFIG["patrol_rr_ratio"]
                        else:
                            stop_loss = pattern_range['high'] * (1 + CONFIG["patrol_sl_buffer_percent"] / 100)
                            take_profit = entry_price - (stop_loss - entry_price) * CONFIG["patrol_rr_ratio"]
                        
                        prec = entry_price; price_precision = max(2, str(prec)[::-1].find('.')) if '.' in str(prec) else 2
                        signal_text = (
                            f"🎯 <b><a href='{link}'>{symbol}</a> | {signal_type}</b>\n\n"
                            f"<b>Strategy:</b> Trend Patrol\n"
                            f"<b>Confirmation:</b> {CONFIG['patrol_entry_tf']} Engulfing\n\n"
                            f"<b>Entry:</b> <code>{entry_price:.{price_precision}f}</code>\n"
                            f"<b>Stop Loss:</b> <code>{stop_loss:.{price_precision}f}</code>\n"
                            f"<b>Take Profit:</b> <code>{take_profit:.{price_precision}f}</code>"
                        )
                        patrol_signals.append(signal_text)
                
                # თუ ქოინი "სათვალთვალო სიაში" უნდა მოხვდეს
                elif patrol_status in ["WATCHLIST_BUY", "WATCHLIST_SELL"]:
                    signal_type = "BUY" if patrol_status == "WATCHLIST_BUY" else "SELL"
                    if (patrol_trend_is_up and signal_type == "BUY") or (not patrol_trend_is_up and signal_type == "SELL"):
                        watchlist_text = f"<b><a href='{link}'>{symbol}</a></b> ({signal_type} setup at EMA21)"
                        if watchlist_text not in watchlist:
                             watchlist.append(watchlist_text)

            except Exception as e:
                print(f"შეცდომა {symbol}-ზე: {e}")
            time.sleep(CONFIG["api_call_delay"])

        status["last_scan_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        
        # --- შეტყობინების ფორმირების ახალი ლოგიკა ---
        final_message = ""
        header = f"📢 <b>სავაჭრო რეპორტი ({status['last_scan_time']})</b>\n"
        final_message += header

        all_signals = elite_signals + patrol_signals
        if all_signals:
            final_message += "\n--- 🎯 <b>დადასტურებული სიგნალები</b> ---\n" + "\n".join(all_signals)
        
        if watchlist:
            final_message += "\n--- 👀 <b>სათვალთვალო სია (Watchlist)</b> ---\n" + ", ".join(watchlist)
            
        if not all_signals and not watchlist:
             final_message += "\n" + (
                f"სკანირების ციკლი დასრულდა. აქტიური სიგნალები ან სეთაფები ვერ მოიძებნა.\n"
                f"თეთრ სიაშია <b>{status['whitelist_count']}</b> სანდო სიმბოლო. ვაგრძელებ დაკვირვებას..."
            )
        
        send_telegram(final_message)

    status["running"] = False

# --- 8. Flask მარშრუტები ---
@app.route("/")
def index(): return render_template("index.html", status=status, config=CONFIG)
@app.route("/start", methods=["POST"])
def start():
    if not status["running"]:
        thread = threading.Thread(target=scan_loop, daemon=True)
        thread.start()
    return "OK"
@app.route("/stop", methods=["POST"])
def stop():
    status["running"] = False
    return "OK"
@app.route("/status")
def get_status(): return jsonify(status)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
