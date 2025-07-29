import os
import time
import threading
import requests
import ccxt
import pandas as pd
import numpy as np
import ta
from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta

# --- 1. კონფიგურაცია: TradeChartist BB სტრატეგია ---
CONFIG = {
    "strategy_name": "TradeChartist Bollinger Bands Filter",
    "scan_timeframe": "1h",
    "bb_length": 55,
    "bb_std_dev": 1.0,
    "risk_reward_ratio": 2.0,
    "ohlcv_limit": 150,
    "api_call_delay": 0.2
}

# --- 2. Telegram-ის მონაცემები ---
BOT_TOKEN = "8158204187:AAFPEApXyE_ot0pz3J23b1h5ubJ82El5gLc"
CHAT_ID = "7465722084"

app = Flask(__name__)

# --- 3. გლობალური სტატუსი (შენს სტრუქტურაზე მორგებული) ---
status = {
    "running": False,
    "current_strategy": CONFIG["strategy_name"],
    "symbols_total": 0,
    "symbols_scanned": 0,
    "scan_duration": "N/A",
    "last_scan_time": "N/A",
    "next_scan_time": "N/A", # დამატებულია ახალი ველები
    "last_scan_results": [],
    "estimated_remaining_sec": 0
}

# --- 4. სერვისები ---
# ვიყენებთ შენს მუშა exchange ინსტანციას
exchange = ccxt.binance({'options': {'defaultType': 'future'}})

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        requests.post(url, data=data, timeout=10).raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Telegram შეცდომა: {e}")

def get_all_symbols():
    try:
        # ვიყენებთ შენს მუშა მეთოდს
        markets = exchange.load_markets()
        return [s for s in markets if markets[s].get('contract') and markets[s]['quote'] == 'USDT' and markets[s]['settle'] == 'USDT']
    except ccxt.BaseError as e:
        print(f"❌ სიმბოლოების ჩატვირთვის შეცდომა: {e}")
        return []

def get_seconds_until_next_candle():
    now = datetime.utcnow()
    next_hour = (now + timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    status["next_scan_time"] = next_hour.strftime("%H:%M:%S UTC")
    return (next_hour - now).total_seconds()

# --- 5. ახალი სტრეგიის ფუნქცია (TradeChartist ლოგიკა) ---
def check_tradechartist_bb_signal(df):
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
    except Exception as e:
        print(f"Indicator error on dataframe: {e}")
        return None

# --- 6. ახალი მთავარი სკანირების ციკლი ---
def scan_loop():
    status["running"] = True
    print("სკანირების ციკლი იწყება... ვტვირთავ სიმბოლოებს.")
    symbols = get_all_symbols()
    
    if not symbols:
        status["running"] = False
        print("სიმბოლოების ჩატვირთვა ვერ მოხერხდა. ციკლი ჩერდება.")
        return

    status["symbols_total"] = len(symbols)
    print(f"ჩაიტვირთა {len(symbols)} სიმბოლო. ვიწყებ სკანირებას.")

    while status["running"]:
        start_time = time.time()
        found_signals = []
        
        for i, symbol in enumerate(symbols):
            if not status["running"]: break
            status["symbols_scanned"] = i + 1
            
            # უკუთვლის ლოგიკა
            elapsed_time = time.time() - start_time
            if elapsed_time > 1:
                time_per_symbol = elapsed_time / (i + 1)
                remaining_symbols = status["symbols_total"] - (i + 1)
                status["estimated_remaining_sec"] = int(time_per_symbol * remaining_symbols)

            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=CONFIG['scan_timeframe'], limit=CONFIG['ohlcv_limit'])
                if len(ohlcv) < CONFIG['ohlcv_limit']: continue
                df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
                result = check_tradechartist_bb_signal(df)

                if result:
                    link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol.replace('/', '').replace(':USDT', '')}.P"
                    prec = df['close'].iloc[-1]
                    price_precision = max(2, str(prec)[::-1].find('.')) if '.' in str(prec) else 2
                    
                    signal_emoji = '📈' if result['signal'] == 'BUY' else '📉'
                    result_text = (
                        f"{signal_emoji} <b>{result['signal']}: <a href='{link}'>{symbol}</a></b> ({CONFIG['scan_timeframe']})\n\n"
                        f"<b>Entry:</b> <code>{result['entry']:.{price_precision}f}</code>\n"
                        f"<b>Stop Loss:</b> <code>{result['sl']:.{price_precision}f}</code>\n"
                        f"<b>Take Profit:</b> <code>{result['tp']:.{price_precision}f}</code>"
                    )
                    found_signals.append(result_text)

            except Exception as e: 
                # იგნორირებას უკეთებს წყვილს და აგრძელებს, რომ ციკლი არ გაჩერდეს
                # print(f"Error on {symbol}: {e}")
                continue
            
            time.sleep(CONFIG['api_call_delay'])

        # სკანირების შედეგების განახლება
        scan_end_time = time.time()
        status["scan_duration"] = f"{int(scan_end_time - start_time)} წმ"
        status["last_scan_time"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        status["last_scan_results"] = found_signals
        status["estimated_remaining_sec"] = 0
        
        if found_signals:
            header = f"🎯 <b>სავაჭრო სიგნალები ({status['last_scan_time']})</b>\n"
            message = header + "\n" + "\n---\n".join(found_signals)
            send_telegram(message)
        else:
            print(f"{status['last_scan_time']} - შესაბამისი სიგნალები ვერ მოიძებნა.")
        
        # ველოდებით შემდეგ სანთელს
        if status["running"]:
            wait_seconds = get_seconds_until_next_candle()
            print(f"სკანირება დასრულდა. ველოდები {int(wait_seconds/60)} წუთს შემდეგ სანთლამდე.")
            
            # ველოდებით 10-წამიანი ინტერვალებით, რომ Stop ღილაკმა სწრაფად იმუშაოს
            for _ in range(int(wait_seconds // 10)):
                if not status["running"]: break
                time.sleep(10)
            if status["running"]:
                time.sleep(wait_seconds % 10)

    # ციკლიდან გამოსვლისას სტატუსის გასუფთავება
    status["running"] = False
    print("სკანირების პროცესი შეჩერებულია მომხმარებლის მიერ.")

# --- 7. Flask მარშრუტები (შენი მუშა ვერსია) ---
@app.route("/")
def index(): 
    return render_template("index.html", status=status, config=CONFIG)

@app.route("/start", methods=["POST"])
def start():
    if not status["running"]:
        thread = threading.Thread(target=scan_loop)
        thread.daemon = True
        thread.start()
    return render_template("index.html", status=status, config=CONFIG)

@app.route("/stop", methods=["POST"])
def stop():
    status["running"] = False
    return render_template("index.html", status=status, config=CONFIG)

@app.route("/status")
def get_status(): 
    return jsonify(status)

if __name__ == "__main__":
    # ვიყენებთ PORT ცვლადს Railway-სთვის
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
