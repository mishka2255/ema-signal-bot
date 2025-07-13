import os
import time
import threading
import requests
import ccxt
import pandas as pd
import ta
from flask import Flask, render_template, request, jsonify

# --- 1. პროფესიონალური კონფიგურაცია ---
CONFIG = {
    # მაღალი ტაიმფრეიმი ტრენდის დასადგენად
    "high_tf": "4h",
    "high_tf_ema": 50, # EMA პერიოდი ტრენდისთვის

    # დაბალი ტაიმფრეიმი შესვლის სიგნალისთვის
    "low_tf": "1h",
    "low_tf_ema_short": 7,
    "low_tf_ema_long": 25,

    # დამატებითი ფილტრები
    "rsi_period": 14,
    "adx_period": 14,
    "adx_threshold": 25, # გავზარდოთ ზღვარი ძლიერი ტრენდისთვის
    
    # ტექნიკური პარამეტრები
    "ohlcv_limit": 100,
    "scan_interval_seconds": 600 # 10 წუთი
}

# --- 2. უსაფრთხოება: API გასაღებები გარემოს ცვლადებიდან ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_FALLBACK_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "YOUR_FALLBACK_CHAT_ID")

app = Flask(__name__)

# გლობალური სტატუსის ობიექტი
status = {
    "running": False,
    "current_strategy": "N/A",
    "symbols_total": 0,
    "symbols_scanned": 0,
    "scan_duration": 0,
    "last_scan_results": [],
    "last_scan_time": "N/A"
}

# --- 3. სერვისები ---
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
        markets = exchange.load_markets()
        return [s for s in markets if markets[s].get('contract') and markets[s]['quote'] == 'USDT']
    except ccxt.BaseError as e:
        print(f"❌ სიმბოლოების ჩატვირთვის შეცდომა: {e}")
        return []

# --- 4. ანალიტიკური ფუნქციები ---

def get_higher_tf_trend(df):
    """განსაზღვრავს ტრენდს მაღალ ტაიმფრეიმზე."""
    try:
        df['ema_trend'] = ta.trend.ema_indicator(df['close'], window=CONFIG['high_tf_ema'])
        last_close = df['close'].iloc[-1]
        last_ema = df['ema_trend'].iloc[-1]
        
        if last_close > last_ema:
            return "BULLISH" # აღმავალი
        elif last_close < last_ema:
            return "BEARISH" # დაღმავალი
        return "NEUTRAL" # ნეიტრალური
    except Exception:
        return "UNKNOWN"

def check_low_tf_signal(df):
    """ეძებს შესვლის სიგნალს დაბალ ტაიმფრეიმზე."""
    try:
        # ინდიკატორების გამოთვლა
        df['ema_short'] = ta.trend.ema_indicator(df['close'], window=CONFIG['low_tf_ema_short'])
        df['ema_long'] = ta.trend.ema_indicator(df['close'], window=CONFIG['low_tf_ema_long'])
        df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=CONFIG['rsi_period']).rsi()
        df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=CONFIG['adx_period']).adx()

        # ბოლო სანთლის მონაცემები
        last = df.iloc[-1]
        prev = df.iloc[-2]

        # გადაკვეთის ლოგიკა
        is_buy_cross = prev['ema_short'] < prev['ema_long'] and last['ema_short'] > last['ema_long']
        is_sell_cross = prev['ema_short'] > prev['ema_long'] and last['ema_short'] < last['ema_long']
        
        signal_type = None
        if is_buy_cross:
            signal_type = "BUY"
        elif is_sell_cross:
            signal_type = "SELL"
        
        if not signal_type:
            return None, []

        # ფილტრების შემოწმება
        passed_filters = []
        is_bullish_candle = last['close'] > last['open']
        
        if signal_type == "BUY" and is_bullish_candle:
            passed_filters.append("CANDLE")
        elif signal_type == "SELL" and not is_bullish_candle:
            passed_filters.append("CANDLE")
            
        if last['adx'] > CONFIG['adx_threshold']:
            passed_filters.append("ADX")
            
        # RSI ფილტრი: BUY-სთვის არ უნდა იყოს გადაყიდული, SELL-სთვის - გადაყიდული
        if signal_type == "BUY" and last['rsi'] < 70:
            passed_filters.append("RSI")
        elif signal_type == "SELL" and last['rsi'] > 30:
            passed_filters.append("RSI")
            
        return signal_type, passed_filters

    except Exception as e:
        # print(f"Low TF Signal Error: {e}")
        return None, []


# --- 5. მთავარი სკანირების ციკლი ---
def scan_loop():
    """მთავარი ციკლი, რომელიც იყენებს მრავალ-ტაიმფრეიმიან ანალიზს."""
    status["running"] = True
    status["current_strategy"] = f"MTA: {CONFIG['high_tf']} Trend / {CONFIG['low_tf']} Entry"
    
    symbols = get_all_symbols()
    status["symbols_total"] = len(symbols)

    while status["running"]:
        start_time = time.time()
        status["last_scan_results"] = []
        status["symbols_scanned"] = 0
        
        found_signals = []

        for i, symbol in enumerate(symbols):
            if not status["running"]: break
            status["symbols_scanned"] = i + 1
            
            try:
                # 1. ვიღებთ მაღალი ტაიმფრეიმის მონაცემებს და ვადგენთ ტრენდს
                ohlcv_high = exchange.fetch_ohlcv(symbol, timeframe=CONFIG['high_tf'], limit=CONFIG['ohlcv_limit'])
                if len(ohlcv_high) < CONFIG['ohlcv_limit']: continue
                df_high = pd.DataFrame(ohlcv_high, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                main_trend = get_higher_tf_trend(df_high)

                if main_trend in ["BULLISH", "BEARISH"]:
                    # 2. თუ ტრენდი გვაქვს, ვიღებთ დაბალი ტაიმფრეიმის მონაცემებს
                    ohlcv_low = exchange.fetch_ohlcv(symbol, timeframe=CONFIG['low_tf'], limit=CONFIG['ohlcv_limit'])
                    if len(ohlcv_low) < CONFIG['ohlcv_limit']: continue
                    df_low = pd.DataFrame(ohlcv_low, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    signal, filters = check_low_tf_signal(df_low)

                    # 3. ვადარებთ ტრენდს და სიგნალს
                    if (main_trend == "BULLISH" and signal == "BUY") or \
                       (main_trend == "BEARISH" and signal == "SELL"):
                        
                        # სიგნალს ვთვლით მხოლოდ თუ ყველა ძირითადი ფილტრი გავლილია
                        if "CANDLE" in filters and "ADX" in filters and "RSI" in filters:
                            link = f"https://www.binance.com/en/futures/{symbol.replace('USDT', '_USDT')}"
                            result_text = (
                                f"📈 <b>{signal}: <a href='{link}'>{symbol}</a></b>\n"
                                f"    - <b>Trend ({CONFIG['high_tf']}):</b> {main_trend}\n"
                                f"    - <b>Entry ({CONFIG['low_tf']}):</b> EMA Cross\n"
                                f"    - <b>Filters:</b> {', '.join(filters)}"
                            )
                            found_signals.append(result_text)

            except ccxt.BaseError:
                continue # ბირჟის შეცდომისას უბრალოდ გადავდივართ შემდეგზე
            except Exception as e:
                print(f"გაუთვალისწინებელი შეცდომა {symbol}-ზე: {e}")
            
            time.sleep(0.3) # API ლიმიტების დაცვა

        status["scan_duration"] = int(time.time() - start_time)
        status["last_scan_time"] = time.strftime("%Y-%m-%d %H:%M:%S")

        if found_signals:
            status["last_scan_results"] = found_signals
            header = f"🎯 <b>სავაჭრო სიგნალები ({status['current_strategy']})</b>\n"
            message = header + "\n\n" + "\n\n".join(found_signals)
            send_telegram(message)
        else:
            print(f"{status['last_scan_time']} - შესაბამისი სიგნალები ვერ მოიძებნა.")

        time.sleep(CONFIG['scan_interval_seconds'])
    
    status["running"] = False

# --- 6. Flask მარშრუტები (არ შეცვლილა, მაგრამ დავტოვოთ) ---
@app.route("/", methods=["GET"])
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

@app.route("/status", methods=["GET"])
def get_status():
    return jsonify(status)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)

