from flask import Flask, render_template, request
import ccxt
import pandas as pd
import ta
import time
import requests
import threading

app = Flask(__name__)

BOT_TOKEN = "8158204187:AAFPEApXyE_ot0pz3J23b1h5ubJ82El5gLc"
CHAT_ID = "7465722084"

status = {
    "running": False,
    "tf": "",
    "total": 0,
    "duration": 0,
    "results": [],
    "finished": False
}

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"Telegram შეცდომა: {e}")

exchange = ccxt.binance({'options': {'defaultType': 'future'}})
markets = exchange.load_markets()
symbols = [s for s in markets if markets[s]['contract'] and markets[s]['quote'] == 'USDT' and markets[s]['active']]

def get_direction(symbol, tf):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=50)
    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
    df['ema7'] = ta.trend.ema_indicator(df['close'], window=7)
    df['ema25'] = ta.trend.ema_indicator(df['close'], window=25)
    ema7 = df['ema7']
    ema25 = df['ema25']
    if ema7.iloc[-2] < ema25.iloc[-2] and ema7.iloc[-1] > ema25.iloc[-1]:
        return "BUY"
    elif ema7.iloc[-2] > ema25.iloc[-2] and ema7.iloc[-1] < ema25.iloc[-1]:
        return "SELL"
    else:
        return None

def check_indicators(df):
    try:
        df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        df['volume_avg'] = df['volume'].rolling(window=20).mean()
        df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close']).average_true_range()
        bb = ta.volatility.BollingerBands(df['close'])
        df['bb_width'] = bb.bollinger_hband() - bb.bollinger_lband()
        df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close']).adx()

        passed = []
        if df['volume'].iloc[-1] > df['volume_avg'].iloc[-1]: passed.append("VOL")
        if df['rsi'].iloc[-1] < 70: passed.append("RSI")
        if df['close'].iloc[-1] > df['open'].iloc[-1]: passed.append("CANDLE")
        if df['ema25'].iloc[-1] > df['ema7'].iloc[-1]: passed.append("TREND")
        if df['atr'].iloc[-1] > df['atr'].rolling(window=20).mean().iloc[-1]: passed.append("ATR")
        if df['bb_width'].iloc[-1] > df['bb_width'].rolling(window=20).mean().iloc[-1]: passed.append("BB")
        if df['adx'].iloc[-1] > 20: passed.append("ADX")

        return passed
    except:
        return []

def scan_confirmed():
    status["running"] = True
    status["tf"] = "1h-confirmed"
    status["results"] = []
    status["finished"] = False
    status["duration"] = 0

    start = time.time()
    results = []

    for symbol in symbols:
        if not status["running"]:
            break

        try:
            direction_1d = get_direction(symbol, "1d")
            direction_1h = get_direction(symbol, "1h")

            if direction_1h and direction_1h == direction_1d:
                df = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="1h", limit=50), columns=['timestamp','open','high','low','close','volume'])
                indicators = check_indicators(df)
                results.append((len(indicators), f"{direction_1h}: {symbol} ({' + '.join(indicators)})"))

        except Exception as e:
            print(f"{symbol} შეცდომა: {e}")

        time.sleep(0.4)
        status["duration"] = int(time.time() - start)

    status["finished"] = True
    if results:
        sorted_results = sorted(results, key=lambda x: -x[0])
        status["results"] = [r[1] for r in sorted_results]
        msg = f"📊 დადასტურებული EMA 7/25 გადაკვეთა (1h + 1d)\n\n" + "\n".join(status["results"])
    else:
        msg = f"ℹ️ არ მოიძებნა დადასტურებული გადაკვეთა\nდრო: {status['duration']} წმ"

    send_telegram(msg)

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", status=status)

@app.route("/start", methods=["POST"])
def start():
    if not status["running"]:
        tf = request.form.get("timeframe")
        if tf == "1h-confirmed":
            thread = threading.Thread(target=scan_confirmed)
        else:
            thread = threading.Thread(target=scan_loop, args=(tf,))
        thread.start()
    return render_template("index.html", status=status)

@app.route("/stop", methods=["POST"])
def stop():
    status["running"] = False
    return render_template("index.html", status=status)

@app.route("/status", methods=["GET"])
def get_status():
    return {
        "running": status["running"],
        "duration": status["duration"],
        "finished": status["finished"],
        "total": status["total"]
    }

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
