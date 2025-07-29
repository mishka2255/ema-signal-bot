import os
import time
import threading
import requests
import ccxt
import pandas as pd
import numpy as np # <-- დამატებულია ახალი სტრატეგიისთვის
import ta
from flask import Flask, render_template, request, jsonify

# --- 1. კონფიგურაცია: მორგებულია ახალ სტრატეგიაზე ---
CONFIG = {
    # სტრატეგიის პარამეტრები
    "scan_timeframe": "1h",
    "bb_length": 55,
    "bb_std_dev": 1.0,
    
    # რისკ-მენეჯმენტი
    "risk_reward_ratio": 2.0,

    # ტექნიკური პარამეტრები
    "ohlcv_limit": 150, # <-- გაზრდილია ახალი სტრატეგიის მოთხოვნით
    "api_call_delay": 0.25,
    "scan_interval_minutes": 60 # <-- სკანირების ინტერვალი 1 საათი
}

# --- 2. Telegram-ის მონაცემები (უცვლელი) ---
BOT_TOKEN = "8158204187:AAFPEApXyE_ot0pz3J23b1h5ubJ82El5gLc"
CHAT_ID = "7465722084"

app = Flask(__name__)
exchange = ccxt.binanceusdm({'options': {'defaultType': 'future'}})

# --- 3. გლობალური სტატუსი (უცვლელი) ---
status = {
    "running": False,
    "current_phase": "Idle",
    "symbols_total": 0,
    "symbols_scanned": 0,
    "last_scan_time": "N/A"
}

# --- 4. სერვისები (უცვლელი) ---
def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        requests.post(url, data=data, timeout=10).raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Telegram შეცდომა: {e}")

def get_all_future_symbols():
    status["current_phase"] = "Fetching all symbols..."
    try:
        markets = exchange.load_markets()
        return [s for s in markets if markets[s].get('contract') and markets[s]['quote'] == 'USDT' and markets[s]['settle'] == 'USDT']
    except Exception as e:
        print(f"სიმბოლოების ჩატვირთვის შეცდომა: {e}")
        return []

# --- 5. სტრატეგიის ახალი ფუნქცია (TradeChartist ლოგიკა) ---
def check_tradechartist_bb_signal(df):
    """
    ამოწმებს სიგნალს TradeChartist-ის BB Filter-ის ლოგიკის მიხედვით.
    """
    if len(df) < CONFIG["bb_length"]: return None
    try:
        bb = ta.volatility.BollingerBands(close=df['close'], window=CONFIG["bb_length"], window_dev=CONFIG["bb_std_dev"])
        df['bb_upper'], df['bb_lower'], df['bb_middle'] = bb.bollinger_hband(), bb.bollinger_lband(), bb.bollinger_mavg()
        df = df.dropna()
        if df.empty: return None

        df['long_condition'] = df['close'] > df['bb_upper']
        df['short_condition'] = df['close'] < df['bb_lower']
        long_indices, short_indices = df.index[df['long_condition']].to_numpy(), df.index[df['short_condition']].to_numpy()

        if len(long_indices) == 0 and len(short_indices) == 0: return None
            
        def find_last_event(current_index, events):
            pos = np.searchsorted(events, current_index)
            return events[pos-1] if pos > 0 else -1

        df['last_long_event'] = [find_last_event(i, long_indices) for i in df.index]
        df['last_short_event'] = [find_last_event(i, short_indices) for i in df.index]
        df['long_is_latest'] = df['last_long_event'] > df['last_short_event']
        df['state_changed'] = df['long_is_latest'].diff()
        
        last_row = df.iloc[-1]
        signal_type = "BUY" if last_row['state_changed'] == True else "SELL" if last_row['state_changed'] == False else None
        
        if signal_type:
            entry_price, stop_loss = last_row['close'], last_row['bb_middle']
            risk = abs(entry_price - stop_loss)
            if risk == 0: return None
            take_profit = entry_price + risk * CONFIG["risk_reward_ratio"] if signal_type == "BUY" else entry_price - risk * CONFIG["risk_reward_ratio"]
            return {"signal": signal_type, "entry": entry_price, "sl": stop_loss, "tp": take_profit}
        return None
    except Exception:
        return None

# --- 6. მთავარი სკანირების ციკლი (მორგებული) ---
def scan_loop():
    status["running"] = True
    all_symbols = get_all_future_symbols()
    status["symbols_total"] = len(all_symbols)

    while status["running"]:
        status["current_phase"] = f"Scanning {CONFIG['scan_timeframe']} with TradeChartist Logic..."
        found_signals = []

        for i, symbol in enumerate(all_symbols):
            if not status["running"]: break
            status["symbols_scanned"] = i + 1
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, CONFIG["scan_timeframe"], limit=CONFIG["ohlcv_limit"])
                if len(ohlcv) < CONFIG['ohlcv_limit']: continue # ვამოწმებთ, რომ საკმარისი მონაცემებია
                
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                # ვიძახებთ ახალ სტრატეგიის ფუნქციას
                result = check_tradechartist_bb_signal(df)
                
                if result:
                    link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol.replace('/', '').replace(':USDT', '')}.P"
                    prec = result['entry']
                    price_precision = max(2, str(prec)[::-1].find('.')) if '.' in str(prec) else 2
                    
                    signal_emoji = '📈' if result['signal'] == 'BUY' else '📉'
                    signal_text = (
                        f"{signal_emoji} <b>{result['signal']}: <a href='{link}'>{symbol}</a></b>\n\n"
                        f"<b>Entry:</b> <code>{result['entry']:.{price_precision}f}</code>\n"
                        f"<b>Stop Loss:</b> <code>{result['sl']:.{price_precision}f}</code>\n"
                        f"<b>Take Profit:</b> <code>{result['tp']:.{price_precision}f}</code>"
                    )
                    found_signals.append(signal_text)
                    print(f"🔥 სიგნალი: {symbol} ({result['signal']})")
                    
            except Exception:
                continue
            time.sleep(CONFIG["api_call_delay"])

        status["last_scan_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        
        if found_signals:
            header = f"📢 <b>სავაჭრო სიგნალები ({status['last_scan_time']})</b>\n"
            message = header + "\n---\n".join(found_signals)
            send_telegram(message)
        else:
            # თუ სიგნალები არ არის, არაფერს ვაგზავნით, რომ არ დაისპამოს ჩატი
            print(f"({status['last_scan_time']}) - სიგნალები ვერ მოიძებნა.")
            
        if status["running"]:
            sleep_duration = CONFIG["scan_interval_minutes"] * 60
            print(f"ციკლი დასრულდა. შემდეგი სკანირება {CONFIG['scan_interval_minutes']} წუთში...")
            # ველოდებით 10-წამიანი ინტერვალებით, რომ Stop ღილაკმა სწრაფად იმუშაოს
            for _ in range(int(sleep_duration / 10)):
                if not status["running"]: break
                time.sleep(10)
            if status["running"]:
                time.sleep(sleep_duration % 10)

    status["running"] = False
    print("სკანირების პროცესი შეჩერებულია.")


# --- 7. Flask მარშრუტები (უცვლელი) ---
@app.route("/")
def index():
    # index.html ფაილის არსებობაა საჭირო templates საქაღალდეში
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
def get_status():
    return jsonify(status)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
