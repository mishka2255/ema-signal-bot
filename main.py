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
    "min_daily_volume_usdt": 20_000_000, # მინიმალური 24სთ მოცულობა (20 მილიონი)
    "min_history_days": 120,             # მინიმალური სავაჭრო ისტორია დღეებში

    # "Three Pillars" - სტრატეგიის პარამეტრები
    "structure_tf": "1d",     # ბაზრის სტრუქტურის ტაიმფრეიმი
    "zone_tf": "4h",          # მოთხოვნა-მიწოდების ზონის ტაიმფრეიმი
    "entry_tf": "1h",         # შესვლის დადასტურების ტაიმფრეიმი
    "swing_points_lookback": 5, # რამდენი სანთელი განვსაზღვროთ სვინგის წერტილისთვის
    
    # პროფესიონალური რისკ-მენეჯმენტი
    "risk_reward_ratio": 2.5,   # რისკი/მოგების თანაფარდობა (მაგ: 2.5:1)
    "sl_buffer_percent": 0.05,  # 0.05% ბუფერი სტოპ ლოსისთვის (სპრედის და ფიტილისგან დასაცავად)

    # ტექნიკური პარამეტრები
    "ohlcv_limit": 200,       # რამდენი სანთელი ჩამოვტვირთოთ ანალიზისთვის
    "api_call_delay": 0.3
}

# --- 2. Telegram-ის მონაცემები (ჩაწერილია პირდაპირ კოდში) ---
BOT_TOKEN = "8158204187:AAFPEApXyE_ot0pz3J23b1h5ubJ82El5gLc"
CHAT_ID = "7465722084"

app = Flask(__name__)
# ვიყენებთ binanceusdm-ს, რომელიც ფიუჩერსებისთვის უფრო სტაბილურია
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

# --- 5. "The Gatekeeper" - ბაზრის ფილტრაცია ---
def get_healthy_symbols_whitelist():
    print("MTA Gatekeeper: ვიწყებ ჯანსაღი სიმბოლოების სიის ფორმირებას...")
    status["current_phase"] = "Filtering markets..."
    whitelist = []
    try:
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
        for symbol in tickers:
            if symbol.endswith(':USDT'): # ვფილტრავთ ფიუჩერსულ წყვილებს
                ticker_data = tickers[symbol]
                # ფილტრი #1: 24სთ მოცულობა
                volume_ok = ticker_data.get('quoteVolume', 0) > CONFIG["min_daily_volume_usdt"]
                if volume_ok:
                    # ფილტრი #2: ისტორია
                    history_ok = False
                    try:
                        ohlcv = exchange.fetch_ohlcv(symbol, '1d', limit=CONFIG["min_history_days"])
                        if len(ohlcv) >= CONFIG["min_history_days"]:
                            history_ok = True
                    except Exception:
                        pass 

                    if history_ok:
                        whitelist.append(symbol)

    except Exception as e:
        print(f"Gatekeeper შეცდომა: {e}")
    print(f"Gatekeeper: თეთრი სია დასრულებულია. შეირჩა {len(whitelist)} სიმბოლო.")
    status["whitelist_count"] = len(whitelist)
    return whitelist

# --- 6. "The Three Pillars" - სტრატეგიის ანალიტიკა ---

def find_swing_points(df, lookback):
    # იყენებს კვანტების მეთოდს სვინგების საპოვნელად
    highs = df['high'].rolling(window=2*lookback+1, center=True).apply(lambda x: x.argmax() == lookback, raw=True)
    lows = df['low'].rolling(window=2*lookback+1, center=True).apply(lambda x: x.argmin() == lookback, raw=True)
    df['swing_high'] = np.where(highs, df['high'], np.nan)
    df['swing_low'] = np.where(lows, df['low'], np.nan)
    return df

def get_market_structure(df):
    swings = find_swing_points(df, CONFIG['swing_points_lookback'])
    swing_highs = swings['swing_high'].dropna().iloc[-2:]
    swing_lows = swings['swing_low'].dropna().iloc[-2:]

    if len(swing_highs) < 2 or len(swing_lows) < 2: return "INDECISIVE"
    
    # ამოწმებს HH/HL ან LL/LH ფორმაციას
    if swing_highs.iloc[-1] > swing_highs.iloc[-2] and swing_lows.iloc[-1] > swing_lows.iloc[-2]: return "BULLISH"
    if swing_highs.iloc[-1] < swing_highs.iloc[-2] and swing_lows.iloc[-1] < swing_lows.iloc[-2]: return "BEARISH"
    return "INDECISIVE"

def find_order_block(df, trend):
    # ეძებს ბოლო სანთელს, რომელიც ქმნის იმბალანსს
    for i in range(len(df) - 2, 0, -1):
        is_bullish_ob_candidate = df['close'][i-1] < df['open'][i-1] and df['close'][i+1] > df['high'][i-1]
        is_bearish_ob_candidate = df['close'][i-1] > df['open'][i-1] and df['close'][i+1] < df['low'][i-1]

        if trend == "BULLISH" and is_bullish_ob_candidate:
            return {'low': df['low'][i-1], 'high': df['high'][i-1], 'type': 'Bullish'}
        if trend == "BEARISH" and is_bearish_ob_candidate:
            return {'low': df['low'][i-1], 'high': df['high'][i-1], 'type': 'Bearish'}
    return None

def check_entry_confirmation(df, trend, zone):
    price = df['close'].iloc[-1]
    # ამოწმებს, არის თუ არა ფასი ზონაში შესული რეაქციისთვის
    if not (zone['low'] <= price <= zone['high']): return False

    swings = find_swing_points(df, 3) # უფრო მცირე ლუქბექი 1სთ-ზე
    if trend == "BULLISH":
        last_local_high = swings['swing_high'].dropna().iloc[-1]
        if price > last_local_high: return True # CHoCH
    elif trend == "BEARISH":
        last_local_low = swings['swing_low'].dropna().iloc[-1]
        if price < last_local_low: return True # CHoCH
    return False

# --- 7. მთავარი სკანირების ციკლი ---
def scan_loop():
    status["running"] = True
    whitelist = get_healthy_symbols_whitelist()
    status["symbols_total"] = len(whitelist)

    while status["running"]:
        start_time = time.time()
        found_signals = []
        status["current_phase"] = "Scanning for opportunities..."

        for i, symbol in enumerate(whitelist):
            if not status["running"]: break
            status["symbols_scanned"] = i + 1
            try:
                ohlcv_d = exchange.fetch_ohlcv(symbol, CONFIG["structure_tf"], limit=CONFIG["ohlcv_limit"])
                df_d = pd.DataFrame(ohlcv_d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                market_structure = get_market_structure(df_d)

                if market_structure in ["BULLISH", "BEARISH"]:
                    ohlcv_4h = exchange.fetch_ohlcv(symbol, CONFIG["zone_tf"], limit=CONFIG["ohlcv_limit"])
                    df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    order_block = find_order_block(df_4h, market_structure)

                    if order_block:
                        ohlcv_1h = exchange.fetch_ohlcv(symbol, CONFIG["entry_tf"], limit=50)
                        df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        confirmed = check_entry_confirmation(df_1h, market_structure, order_block)

                        if confirmed:
                            # --- პროფესიონალური რისკ-მენეჯმენტის ლოგიკა ---
                            entry_price = df_1h['close'].iloc[-1]
                            
                            if market_structure == "BULLISH":
                                # Stop Loss: Order Block-ის მინიმუმის ქვემოთ, ბუფერით
                                stop_loss = order_block['low'] * (1 - CONFIG["sl_buffer_percent"] / 100)
                                # Take Profit: გამოთვლილი RR-ით
                                take_profit = entry_price + (entry_price - stop_loss) * CONFIG["risk_reward_ratio"]
                            else: # BEARISH
                                # Stop Loss: Order Block-ის მაქსიმუმის ზემოთ, ბუფერით
                                stop_loss = order_block['high'] * (1 + CONFIG["sl_buffer_percent"] / 100)
                                # Take Profit: გამოთვლილი RR-ით
                                take_profit = entry_price - (stop_loss - entry_price) * CONFIG["risk_reward_ratio"]

                            # უსაფრთხოების შემოწმება, რომ სტოპი ლოგიკურია
                            if (market_structure == "BULLISH" and entry_price > stop_loss) or \
                               (market_structure == "BEARISH" and entry_price < stop_loss):
                                
                                link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol.replace('/', '').replace(':USDT', '')}.P"
                                prec = entry_price
                                price_precision = max(2, str(prec)[::-1].find('.')) if '.' in str(prec) else 2

                                signal_text = (
                                    f"💎 <b><a href='{link}'>{symbol}</a> | {market_structure}</b>\n\n"
                                    f"<b>Strategy:</b> Market Structure Shift\n"
                                    f"<b>Zone:</b> {CONFIG['zone_tf']} Order Block\n"
                                    f"<b>Confirmation:</b> {CONFIG['entry_tf']} CHoCH\n\n"
                                    f"<b>Entry:</b> <code>{entry_price:.{price_precision}f}</code>\n"
                                    f"<b>Stop Loss:</b> <code>{stop_loss:.{price_precision}f}</code>\n"
                                    f"<b>Take Profit:</b> <code>{take_profit:.{price_precision}f}</code>"
                                )
                                found_signals.append(signal_text)
                                print(f"✅ სიგნალი მოიძებნა: {symbol}")

            except Exception as e:
                print(f"შეცდომა {symbol}-ზე: {e}")
            time.sleep(CONFIG["api_call_delay"])

        status["last_scan_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if found_signals:
            header = f"🎯 <b>ელიტური სავაჭრო სიგნალები ({status['last_scan_time']})</b>\n"
            message = header + "\n" + "\n\n".join(found_signals)
            send_telegram(message)

        print(f"სკანირების ციკლი დასრულდა {int(time.time() - start_time)} წამში. ვიწყებ ახალს...")

    status["running"] = False
    print("სკანირების პროცესი შეჩერებულია.")

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
