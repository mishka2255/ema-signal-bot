from flask import Flask, render_template, request
import ccxt
import pandas as pd
import ta
import time
import requests
import threading

app = Flask(__name__)

# Telegram პარამეტრები
BOT_TOKEN = "8158204187:AAFPEApXyE_ot0pz3J23b1h5ubJ82El5gLc"
CHAT_ID = "7465722084"

# სტატუსის ობიექტი
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

# Binance Futures
exchange = ccxt.binance({'options': {'defaultType': 'future'}})

def get_symbols():
    try:
        markets = exchange.load_markets()
        symbols = [s for s in markets if markets[s].get('contract') and markets[s]['quote'] == 'USDT']
        print(f"🔍 ქოინების რაოდენობა: {len(symbols)}")
        return symbols
    except Exception as e:
        print(f"❌ get_symbols შეცდომა: {e}")
        return []

def check_indicators(df):
    try:
        df['ema7'] = ta.trend.ema_indicator(df['close'], window=7)
        df['ema25'] = ta.trend.ema_indicator(df['close'], window=25)
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

def is_confirmed_after_cross(df):
    df['ema7'] = ta.trend.ema_indicator(df['close'], window=7)
    df['ema25'] = ta.trend.ema_indicator(df['close'], window=25)

    ema7_prev = df['ema7'].iloc[-3]
    ema25_prev = df['ema25'].iloc[-3]
    ema7_cross = df['ema7'].iloc[-2]
    ema25_cross = df['ema25'].iloc[-2]

    # BUY გადაკვეთა — ოვერ ლოუს შემდეგ
    if ema7_prev < ema25_prev and ema7_cross > ema25_cross:
        red_count = sum(df.iloc[-i]['close'] < df.iloc[-i]['open'] for i in [1, 2])
        if red_count in [1, 2]:
            return "BUY"

    # SELL გადაკვეთა — ოვერ ჰაის შემდეგ
    elif ema7_prev > ema25_prev and ema7_cross < ema25_cross:
        green_count = sum(df.iloc[-i]['close'] > df.iloc[-i]['open'] for i in [1, 2])
        if green_count in [1, 2]:
            return "SELL"

    return None

def scan_loop(tf):
    status["running"] = True
    status["tf"] = tf

    while status["running"]:
        symbols = get_symbols()
        status["total"] = len(symbols)
        status["results"] = []
        status["duration"] = 0
        status["finished"] = False

        start = time.time()
        results = []

        for symbol in symbols:
            if not status["running"]:
                break
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe="1h", limit=52)
                if len(ohlcv) < 52:
                    continue

                df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
                dir_signal = is_confirmed_after_cross(df)

                if dir_signal:
                    indicators = check_indicators(df)
                    results.append((len(indicators), f"{dir_signal}: {symbol} ({' + '.join(indicators)})"))
            except Exception as e:
                print(f"{symbol} შეცდომა: {e}")
            time.sleep(0.3)

        status["duration"] = int(time.time() - start)
        status["finished"] = True

        if results:
            sorted_results = sorted(results, key=lambda x: -x[0])
            status["results"] = [r[1] for r in sorted_results]
            msg = f"📊 EMA 7/25 გადაკვეთა 1სთ (დადასტურებული)\n\n" + "\n".join(status["results"])
        else:
            msg = "❌ არ მოიძებნა გადაკვეთა\nტაიმფრეიმი: 1h-confirmed"

        send_telegram(msg)
        time.sleep(300)  # ყოველ 5 წუთში ერთხელ

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", status=status)

@app.route("/start", methods=["POST"])
def start():
    if not status["running"]:
        tf = request.form.get("timeframe")
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
