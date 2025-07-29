import os
import time
import threading
import requests
import ccxt
import pandas as pd
import ta
from flask import Flask, render_template, request, jsonify

# --- 1. კონფიგურაცია: BB Breakout სტრატეგია ---
CONFIG = {
    # სტრატეგიის პარამეტრები
    "scan_timeframe": "1h",
    "bb_length": 55,
    "bb_std_dev": 1.0,
    "signal_freshness_candles": 2, # მაქსიმუმ რამდენი სანთლის წინანდელი სიგნალი მივიღოთ

    # რისკ-მენეჯმენტი
    "risk_reward_ratio": 2.0,

    # ტექნიკური პარამეტრები
    "ohlcv_limit": 60,
    "api_call_delay": 0.25,
    "scan_interval_minutes": 5 # --- *** ახალი პარამეტრი: სკანირების ინტერვალი წუთებში *** ---
}

# --- 2. Telegram-ის მონაცემები ---
BOT_TOKEN = "8158204187:AAFPEApXyE_ot0pz3J23b1h5ubJ82El5gLc"
CHAT_ID = "7465722084"

app = Flask(__name__)
exchange = ccxt.binanceusdm({'options': {'defaultType': 'future'}})

# --- 3. გლობალური სტატუსი ---
status = {
    "running": False,
    "current_phase": "Idle",
    "symbols_total": 0,
    "symbols_scanned": 0,
    "last_scan_time": "N/A"
}

# --- 4. სერვისები ---
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

# --- 5. სტრატეგიის მთავარი ფუნქცია ---
def check_bb_breakout_signal(df):
    try:
        bb_indicator = ta.volatility.BollingerBands(
            close=df['close'],
            window=CONFIG["bb_length"],
            window_dev=CONFIG["bb_std_dev"]
        )
        df['bb_upper'] = bb_indicator.bollinger_hband()
        df['bb_lower'] = bb_indicator.bollinger_lband()
        df['bb_middle'] = bb_indicator.bollinger_mavg()

        for i in range(1, CONFIG["signal_freshness_candles"] + 1):
            if len(df) <= i: continue

            current = df.iloc[-i]
            previous = df.iloc[-(i+1)]

            signal_type = None
            if previous['close'] <= previous['bb_upper'] and current['close'] > current['bb_upper']:
                signal_type = "BUY"
            elif previous['close'] >= previous['bb_lower'] and current['close'] < current['bb_lower']:
                signal_type = "SELL"
            
            if signal_type:
                entry_price = current['close']
                stop_loss = current['bb_middle']
                
                if signal_type == "BUY":
                    risk = entry_price - stop_loss
                    if risk <= 0: continue
                    take_profit = entry_price + risk * CONFIG["risk_reward_ratio"]
                else: # SELL
                    risk = stop_loss - entry_price
                    if risk <= 0: continue
                    take_profit = entry_price - risk * CONFIG["risk_reward_ratio"]
                
                return {
                    "signal": signal_type,
                    "entry": entry_price,
                    "sl": stop_loss,
                    "tp": take_profit
                }
        return None
    except Exception:
        return None

# --- 6. მთავარი სკანირების ციკლი ---
def scan_loop():
    # დაველოდოთ რამდენიმე წამი, რომ სერვერი სრულად ჩაიტვირთოს
    time.sleep(10) 
    status["running"] = True
    all_symbols = get_all_future_symbols()
    status["symbols_total"] = len(all_symbols)

    while status["running"]:
        status["current_phase"] = f"Scanning {CONFIG['scan_timeframe']} BB Breakouts..."
        found_signals = []

        for i, symbol in enumerate(all_symbols):
            if not status["running"]: break
            status["symbols_scanned"] = i + 1
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, CONFIG["scan_timeframe"], limit=CONFIG["ohlcv_limit"])
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                result = check_bb_breakout_signal(df)
                
                if result:
                    link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol.replace('/', '').replace(':USDT', '')}.P"
                    prec = result['entry']
                    price_precision = max(2, str(prec)[::-1].find('.')) if '.' in str(prec) else 2
                    
                    signal_text = (
                        f"🔥 <b>BB Breakout: <a href='{link}'>{symbol}</a> | {result['signal']}</b>\n\n"
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
            status_message = (
                f"✅ <b>სტატუს-რეპორტი ({status['last_scan_time']})</b>\n\n"
                f"BB Breakout სიგნალები ვერ მოიძებნა. ვაგრძელებ დაკვირვებას..."
            )
            send_telegram(status_message)
        
        if status["running"]:
            sleep_duration = CONFIG["scan_interval_minutes"] * 60
            print(f"ციკლი დასრულდა. შემდეგი სკანირება {CONFIG['scan_interval_minutes']} წუთში...")
            time.sleep(sleep_duration)

    status["running"] = False
    print("სკანირების პროცესი შეჩერებულია.")


# --- 7. Flask მარშრუტები ---
@app.route("/")
def index():
    return render_template("index.html", status=status, config=CONFIG)

@app.route("/start", methods=["POST"])
def start():
    if not status["running"]:
        print("Starting scanner manually...")
        thread = threading.Thread(target=scan_loop, daemon=True)
        thread.start()
    return "OK"

@app.route("/stop", methods=["POST"])
def stop():
    if status["running"]:
        print("Stopping scanner manually...")
        status["running"] = False
    return "OK"

@app.route("/status")
def get_status():
    return jsonify(status)

# --- 8. ავტომატური გაშვება სერვერის ჩართვისას ---
def auto_start_scanner():
    """Starts the scanner in a background thread upon application startup."""
    print("Starting scanner automatically on boot...")
    thread = threading.Thread(target=scan_loop, daemon=True)
    thread.start()

# ეს კოდი გაეშვება ერთხელ, როდესაც Gunicorn-ი ჩატვირთავს აპლიკაციას
auto_start_scanner()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
