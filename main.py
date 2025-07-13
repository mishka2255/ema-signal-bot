import os
import time
import threading
import requests
import ccxt
import pandas as pd
import ta
from flask import Flask, render_template, request, jsonify

# --- 1. კონფიგურაცია ---
CONFIG = {
    "high_tf": "4h", "high_tf_ema": 50,
    "low_tf": "1h", "low_tf_ema_short": 7, "low_tf_ema_long": 25,
    "rsi_period": 14, "adx_period": 14, "adx_threshold": 25,
    "atr_period_for_sl": 14, "atr_multiplier_for_sl": 2.0,
    "risk_reward_ratio": 1.5,
    "ohlcv_limit": 100, "api_call_delay": 0.25
}

# --- 2. უსაფრთხოება ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_FALLBACK_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "YOUR_FALLBACK_CHAT_ID")

app = Flask(__name__)

# --- 3. გლობალური სტატუსი ---
status = {
    "running": False, "current_strategy": "N/A",
    "symbols_total": 0, "symbols_scanned": 0,
    "scan_duration": 0, "last_scan_time": "N/A",
    "last_scan_results": [], "estimated_remaining_sec": 0
}

# --- 4. სერვისები ---
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

# --- 5. ანალიტიკური ფუნქციები ---
def get_higher_tf_trend(df):
    try:
        df['ema_trend'] = ta.trend.ema_indicator(df['close'], window=CONFIG['high_tf_ema'])
        if df['close'].iloc[-1] > df['ema_trend'].iloc[-1]: return "BULLISH"
        if df['close'].iloc[-1] < df['ema_trend'].iloc[-1]: return "BEARISH"
        return "NEUTRAL"
    except Exception: return "UNKNOWN"

def analyze_low_tf(df):
    try:
        df['ema_short'] = ta.trend.ema_indicator(df['close'], window=CONFIG['low_tf_ema_short'])
        df['ema_long'] = ta.trend.ema_indicator(df['close'], window=CONFIG['low_tf_ema_long'])
        df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=CONFIG['rsi_period']).rsi()
        df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=CONFIG['adx_period']).adx()
        df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=CONFIG['atr_period_for_sl']).average_true_range()

        last, prev = df.iloc[-1], df.iloc[-2]
        
        signal_type = None
        if prev['ema_short'] < prev['ema_long'] and last['ema_short'] > last['ema_long']: signal_type = "BUY"
        if prev['ema_short'] > prev['ema_long'] and last['ema_short'] < last['ema_long']: signal_type = "SELL"
        if not signal_type: return None, [], None, None, None

        passed_filters = []
        if signal_type == "BUY" and last['close'] > last['open']: passed_filters.append("✅ Candle")
        if signal_type == "SELL" and last['close'] < last['open']: passed_filters.append("✅ Candle")
        if last['adx'] > CONFIG['adx_threshold']: passed_filters.append("✅ ADX")
        if signal_type == "BUY" and last['rsi'] < 70: passed_filters.append("✅ RSI")
        if signal_type == "SELL" and last['rsi'] > 30: passed_filters.append("✅ RSI")

        entry_price, atr_value = last['close'], last['atr']
        
        if signal_type == "BUY":
            stop_loss = entry_price - atr_value * CONFIG['atr_multiplier_for_sl']
            take_profit = entry_price + (entry_price - stop_loss) * CONFIG['risk_reward_ratio']
        else:
            stop_loss = entry_price + atr_value * CONFIG['atr_multiplier_for_sl']
            take_profit = entry_price - (stop_loss - entry_price) * CONFIG['risk_reward_ratio']

        return signal_type, passed_filters, entry_price, stop_loss, take_profit
    except Exception: return None, [], None, None, None

# --- 6. მთავარი სკანირების ციკლი ---
def scan_loop():
    status["running"] = True
    status["current_strategy"] = f"MTA: {CONFIG['high_tf']} Trend / {CONFIG['low_tf']} Entry"
    symbols = get_all_symbols()
    status["symbols_total"] = len(symbols)

    while status["running"]:
        start_time = time.time()
        found_signals = []
        
        for i, symbol in enumerate(symbols):
            if not status["running"]: break
            
            # განახლებული სტატუსი ყოველი სიმბოლოს შემდეგ
            status["symbols_scanned"] = i + 1
            elapsed_time = time.time() - start_time
            if elapsed_time > 1: # ვითვლით მხოლოდ თუ საკმარისი დრო გავიდა
                time_per_symbol = elapsed_time / status["symbols_scanned"]
                remaining_symbols = status["symbols_total"] - status["symbols_scanned"]
                status["estimated_remaining_sec"] = int(time_per_symbol * remaining_symbols)

            try:
                ohlcv_high = exchange.fetch_ohlcv(symbol, timeframe=CONFIG['high_tf'], limit=CONFIG['ohlcv_limit'])
                if len(ohlcv_high) < CONFIG['ohlcv_limit']: continue
                df_high = pd.DataFrame(ohlcv_high, columns=['timestamp','open','high','low','close','volume'])
                main_trend = get_higher_tf_trend(df_high)

                if main_trend in ["BULLISH", "BEARISH"]:
                    ohlcv_low = exchange.fetch_ohlcv(symbol, timeframe=CONFIG['low_tf'], limit=CONFIG['ohlcv_limit'])
                    if len(ohlcv_low) < CONFIG['ohlcv_limit']: continue
                    df_low = pd.DataFrame(ohlcv_low, columns=['timestamp','open','high','low','close','volume'])
                    
                    signal, filters, entry, sl, tp = analyze_low_tf(df_low)

                    if (main_trend == "BULLISH" and signal == "BUY") or \
                       (main_trend == "BEARISH" and signal == "SELL"):
                        
                        link = f"https://www.binance.com/en/futures/{symbol.replace('USDT', '_USDT')}"
                        prec = df_low['close'].iloc[-1]
                        price_precision = max(2, str(prec)[::-1].find('.')) if '.' in str(prec) else 2
                        
                        result_text = (
                            f"{'📈' if signal == 'BUY' else '📉'} <b>{signal}: <a href='{link}'>{symbol}</a></b>\n\n"
                            f"<b>Entry:</b> <code>{entry:.{price_precision}f}</code>\n"
                            f"<b>Stop:</b>  <code>{sl:.{price_precision}f}</code>\n"
                            f"<b>Profit:</b> <code>{tp:.{price_precision}f}</code>\n\n"
                            f"<b>Trend ({CONFIG['high_tf']}):</b> {main_trend}\n"
                            f"<b>Filters:</b> {' '.join(filters) if filters else '❌ None'}"
                        )
                        # ვინახავთ სიგნალს და ფილტრების რაოდენობას დასახარისხებლად
                        found_signals.append({'text': result_text, 'quality': len(filters)})

            except ccxt.BaseError: continue
            except Exception as e: print(f"Error on {symbol}: {e}")
            
            time.sleep(CONFIG['api_call_delay'])

        status["scan_duration"] = int(time.time() - start_time)
        status["last_scan_time"] = time.strftime("%Y-%m-%d %H:%M:%S")

        if found_signals:
            # ვახარისხებთ სიგნალებს ხარისხის მიხედვით (კლებადობით)
            sorted_signals = sorted(found_signals, key=lambda x: x['quality'], reverse=True)
            
            # ვამატებთ მედლებს საუკეთესო სიგნალებს
            medals = ['🥇', '🥈', '🥉']
            final_messages = []
            for i, sig in enumerate(sorted_signals):
                prefix = medals[i] if i < len(medals) else '🔹'
                final_messages.append(f"{prefix} {sig['text']}")

            status["last_scan_results"] = final_messages
            header = f"🎯 <b>სავაჭრო სიგნალები ({time.strftime('%H:%M:%S')})</b>\n"
            message = header + "\n" + "\n---\n".join(final_messages)
            send_telegram(message)
        else:
            status["last_scan_results"] = []
            print(f"{status['last_scan_time']} - შესაბამისი სიგნალები ვერ მოიძებნა.")
        
        status["estimated_remaining_sec"] = 0

    status["running"] = False
    print("სკანირების პროცესი შეჩერებულია.")

# --- 7. Flask მარშრუტები ---
@app.route("/")
def index(): return render_template("index.html", status=status, config=CONFIG)

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
def get_status(): return jsonify(status)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
