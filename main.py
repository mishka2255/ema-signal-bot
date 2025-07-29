import os
import time
import threading
import requests
import ccxt
import pandas as pd
import numpy as np
import ta
from flask import Flask, render_template, jsonify
from datetime import datetime, timedelta

# --- 1. კონფიგურაცია: TradeChartist BB სტრატეგია ---
# მომხმარებლის მოთხოვნით, ტოკენი და ID ჩაწერილია პირდაპირ.
BOT_TOKEN = "8158204187:AAFPEApXyE_ot0pz3J23b1h5ubJ82El5gLc"
CHAT_ID = "7465722084"

CONFIG = {
    # სტრატეგიის პარამეტრები (Pine Script-ის მიხედვით)
    "scan_timeframe": "1h",
    "bb_length": 55,
    "bb_std_dev": 1.0,

    # რისკ-მენეჯმენტი
    "risk_reward_ratio": 2.0,
    "min_volume_usdt": 10_000_000,

    # ტექნიკური პარამეტრები
    "ohlcv_limit": 150, # ვზრდით, რომ barssince ლოგიკამ ზუსტად იმუშაოს
    "api_call_delay": 0.2,
    "signal_cooldown_hours": 4
}

# --- 2. Flask და CCXT ინიციალიზაცია (გასწორებული) ---
app = Flask(__name__)
# **გასწორება:** ვქმნით exchange ობიექტს, მაგრამ ჯერ არ ვტვირთავთ მარკეტებს.
try:
    exchange = ccxt.binanceusdm({'options': {'defaultType': 'future'}})
    print("CCXT exchange ობიექტი წარმატებით შეიქმნა.")
except Exception as e:
    print(f"CCXT ობიექტის შექმნის შეცდომა: {e}")
    exchange = None

# --- 3. გლობალური სტატუსი და მონაცემები ---
status = {
    "running": False,
    "current_phase": "Idle",
    "symbols_total": 0,
    "symbols_scanned": 0,
    "last_scan_time": "N/A",
    "next_scan_time": "N/A",
    "last_error": None,
    "markets_loaded": False
}
sent_signals = {}


# --- 4. სერვისის ფუნქციები ---
def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram BOT_TOKEN ან CHAT_ID არ არის მითითებული.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        response = requests.post(url, data=data, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        error_message = f"Telegram შეცდომა: {e}"
        print(error_message)
        status["last_error"] = error_message

def get_filtered_symbols():
    status["current_phase"] = "Fetching and filtering symbols..."
    try:
        tickers = exchange.fetch_tickers()
        filtered_symbols = [
            symbol for symbol, market in exchange.markets.items()
            if market.get('contract') and market.get('quote') == 'USDT' and market.get('settle') == 'USDT'
            and tickers.get(symbol) and tickers[symbol].get('quoteVolume', 0) > CONFIG["min_volume_usdt"]
        ]
        print(f"მოიძებნა {len(filtered_symbols)} წყვილი, რომელიც აკმაყოფილებს მოცულობის ფილტრს.")
        return filtered_symbols
    except Exception as e:
        error_message = f"სიმბოლოების ჩატვირთვის შეცდომა: {e}"
        print(error_message)
        status["last_error"] = error_message
        return []

def get_seconds_until_next_candle():
    now = datetime.utcnow()
    next_hour = (now + timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    wait_seconds = (next_hour - now).total_seconds()
    status["next_scan_time"] = next_hour.strftime("%Y-%m-%d %H:%M:%S UTC")
    return wait_seconds

# --- 5. ახალი სტრეგიის ფუნქცია (TradeChartist ლოგიკით) ---
def check_tradechartist_bb_signal(df):
    """
    ამოწმებს სიგნალს TradeChartist-ის BB Filter-ის ლოგიკის მიხედვით.
    სიგნალი გენერირდება მხოლოდ მაშინ, როდესაც იცვლება ბოლო გარღვევის მიმართულება.
    """
    if len(df) < CONFIG["bb_length"]: return None

    try:
        bb = ta.volatility.BollingerBands(
            close=df['close'], window=CONFIG["bb_length"], window_dev=CONFIG["bb_std_dev"]
        )
        df['bb_upper'] = bb.bollinger_hband()
        df['bb_lower'] = bb.bollinger_lband()
        df['bb_middle'] = bb.bollinger_mavg()
        df = df.dropna()

        df['long_condition'] = df['close'] > df['bb_upper']
        df['short_condition'] = df['close'] < df['bb_lower']

        long_indices = df.index[df['long_condition']].to_numpy()
        short_indices = df.index[df['short_condition']].to_numpy()

        if len(long_indices) == 0 and len(short_indices) == 0:
            return None
            
        def find_last_event(current_index, events):
            pos = np.searchsorted(events, current_index)
            return events[pos-1] if pos > 0 else -1

        df['last_long_event'] = [find_last_event(i, long_indices) for i in df.index]
        df['last_short_event'] = [find_last_event(i, short_indices) for i in df.index]
        
        df['long_is_latest'] = df['last_long_event'] > df['last_short_event']
        df['state_changed'] = df['long_is_latest'].diff()

        last_row = df.iloc[-1]
        
        signal_type = None
        if last_row['state_changed'] == True:
            signal_type = "BUY"
        elif last_row['state_changed'] == False:
            signal_type = "SELL"
        
        if signal_type:
            entry_price = last_row['close']
            stop_loss = last_row['bb_middle']
            risk = abs(entry_price - stop_loss)
            if risk == 0: return None

            take_profit = entry_price + risk * CONFIG["risk_reward_ratio"] if signal_type == "BUY" else entry_price - risk * CONFIG["risk_reward_ratio"]
            
            return {"signal": signal_type, "entry": entry_price, "sl": stop_loss, "tp": take_profit}
            
        return None
    except Exception as e:
        status["last_error"] = f"Indicator calculation error: {e}"
        return None

# --- 6. მთავარი სკანირების ციკლი (გაძლიერებული) ---
def scan_loop():
    if not exchange:
        print("ბირჟა არ არის ინიციალიზებული. სკანირება ჩერდება.")
        status["running"] = False
        return

    status["running"] = True
    print("სკანირების ციკლი დაიწყო.")

    # **გასწორება:** მარკეტების ჩატვირთვა ხდება აქ, "Start" ღილაკზე დაჭერის შემდეგ.
    if not status["markets_loaded"]:
        try:
            status["current_phase"] = "Loading markets..."
            print("იწყება მარკეტების ჩატვირთვა...")
            exchange.load_markets()
            status["markets_loaded"] = True
            print("მარკეტები წარმატებით ჩაიტვირთა.")
        except Exception as e:
            error_msg = f"მარკეტების ჩატვირთვის კრიტიკული შეცდომა: {e}"
            print(error_msg)
            status["last_error"] = error_msg
            status["running"] = False
            return
    
    all_symbols = get_filtered_symbols()
    status["symbols_total"] = len(all_symbols)

    while status["running"]:
        try:
            current_time = datetime.utcnow()
            status["current_phase"] = f"Scanning {CONFIG['scan_timeframe']} with TradeChartist Logic..."
            status["last_scan_time"] = current_time.strftime("%Y-%m-%d %H:%M:%S UTC")
            found_signals = []

            for i, symbol in enumerate(all_symbols):
                if not status["running"]: break
                status["symbols_scanned"] = i + 1
                if symbol in sent_signals and sent_signals[symbol] > current_time - timedelta(hours=CONFIG["signal_cooldown_hours"]):
                    continue

                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, CONFIG["scan_timeframe"], limit=CONFIG["ohlcv_limit"])
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    result = check_tradechartist_bb_signal(df)
                    
                    if result:
                        price_precision = exchange.markets[symbol]['precision']['price']
                        link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol.replace('/', '').replace(':USDT', '')}.P"
                        signal_text = (
                            f"🔥 <b>TradeChartist BB Signal: <a href='{link}'>{symbol}</a> | {result['signal']}</b>\n\n"
                            f"<b>Entry:</b> <code>{result['entry']:.{price_precision}f}</code>\n"
                            f"<b>Stop Loss:</b> <code>{result['sl']:.{price_precision}f}</code>\n"
                            f"<b>Take Profit:</b> <code>{result['tp']:.{price_precision}f}</code>"
                        )
                        found_signals.append(signal_text)
                        sent_signals[symbol] = current_time
                        print(f"🔥 სიგნალი: {symbol} ({result['signal']})")
                except Exception as e:
                    print(f"შეცდომა წყვილზე {symbol}: {e}")
                    status["last_error"] = f"Error on {symbol}: {e}"
                    continue
                
                time.sleep(CONFIG["api_call_delay"])

            if found_signals:
                header = f"📢 <b>სავაჭრო სიგნალები ({status['last_scan_time']})</b>\n"
                message = header + "\n---\n".join(found_signals)
                send_telegram(message)
            else:
                print(f"სკანირება დასრულდა, ახალი სიგნალები არ არის. შემდეგი სკანირება: {status['next_scan_time']}")
            
            if status["running"]:
                wait_time = get_seconds_until_next_candle()
                print(f"ველოდები {wait_time:.0f} წამს შემდეგ სკანირებამდე...")
                time.sleep(max(10, wait_time))

        except Exception as e:
            print(f"მოულოდნელი შეცდომა მთავარ ციკლში: {e}. ველოდები 60 წამს და ვაგრძელებ.")
            status["last_error"] = f"Main loop error: {e}"
            time.sleep(60)

    status["current_phase"] = "Idle"
    status["running"] = False
    print("სკანირების ციკლი დასრულდა მომხმარებლის მიერ.")

# --- 7. Flask ვებ-ინტერფეისი ---
@app.route("/")
def index():
    return render_template("index.html", status=status, config=CONFIG)

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
def get_status_json():
    return jsonify(status)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
