from flask import Flask, render_template, request
import ccxt
import pandas as pd
import ta
import time
import requests
import threading

app = Flask(__name__)

# Telegram მონაცემები
BOT_TOKEN = "8158204187:AAFPEApXyE_ot0pz3J23b1h5ubJ82El5gLc"
CHAT_ID = "7465722084"

# სტატუსის ტრეკერი
status = {
    "running": False,
    "tf": "",
    "total": 0,
    "duration": 0,
    "results": [],
    "finished": False
}

# შეტყობინების გაგზავნა Telegram-ზე
def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"Telegram შეცდომა: {e}")

# Binance Future ბაზარი
exchange = ccxt.binance({'options': {'defaultType': 'future'}})
markets = exchange.load_markets()
symbols = [s for s in markets if markets[s]['contract'] and markets[s]['quote'] == 'USDT' and markets[s]['active']]

# EMA 7/25 გადაკვეთის შემოწმება
def check_cross(symbol, tf):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=50)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

        df['ema7'] = ta.trend.ema_indicator(df['close'], window=7)
        df['ema25'] = ta.trend.ema_indicator(df['close'], window=25)
        df['ema50'] = ta.trend.ema_indicator(df['close'], window=50)
        df['ema200'] = ta.trend.ema_indicator(df['close'], window=200)

        df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        df['volume_avg'] = df['volume'].rolling(window=20).mean()
        df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close']).average_true_range()
        bb = ta.volatility.BollingerBands(df['close'])
        df['bb_width'] = bb.bollinger_hband() - bb.bollinger_lband()
        df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close']).adx()

        ema_cross_up = df['ema7'].iloc[-2] < df['ema25'].iloc[-2] and df['ema7'].iloc[-1] > df['ema25'].iloc[-1]
        ema_cross_down = df['ema7'].iloc[-2] > df['ema25'].iloc[-2] and df['ema7'].iloc[-1] < df['ema25'].iloc[-1]

        volume_ok = df['volume'].iloc[-1] > df['volume_avg'].iloc[-1]
        rsi_ok_buy = df['rsi'].iloc[-1] < 70
        rsi_ok_sell = df['rsi'].iloc[-1] > 30
        is_bullish = df['close'].iloc[-1] > df['open'].iloc[-1]
        is_bearish = df['close'].iloc[-1] < df['open'].iloc[-1]
        trend_up = df['ema50'].iloc[-1] > df['ema200'].iloc[-1]
        trend_down = df['ema50'].iloc[-1] < df['ema200'].iloc[-1]
        atr_ok = df['atr'].iloc[-1] > df['atr'].rolling(window=20).mean().iloc[-1]
        bb_ok = df['bb_width'].iloc[-1] > df['bb_width'].rolling(window=20).mean().iloc[-1]
        adx_ok = df['adx'].iloc[-1] > 20

        passed = []
        if volume_ok:
            passed.append("VOL")
        if rsi_ok_buy if ema_cross_up else rsi_ok_sell:
            passed.append("RSI")
        if is_bullish if ema_cross_up else is_bearish:
            passed.append("CANDLE")
        if trend_up if ema_cross_up else trend_down:
            passed.append("TREND")
        if atr_ok:
            passed.append("ATR")
        if bb_ok:
            passed.append("BB")
        if adx_ok:
            passed.append("ADX")

        if ema_cross_up or ema_cross_down:
            signal_type = "BUY" if ema_cross_up else "SELL"
            indicators = " + ".join(passed)
            return (len(passed), f"{signal_type}: {symbol} ({indicators})")

    except Exception as e:
        print(f"{symbol} ❌ შეცდომა: {e}")
    return None

# მუდმივი სკანირების ციკლი
def scan_loop(tf):
    status["running"] = True
    status["tf"] = tf

    while status["running"]:
        status["total"] = len(symbols)
        status["results"] = []
        status["finished"] = False
        status["duration"] = 0

        start_time = time.time()
        results_ranked = []

        for symbol in symbols:
            result = check_cross(symbol, tf)
            if result:
                results_ranked.append(result)
            time.sleep(0.4)
            status["duration"] = int(time.time() - start_time)

        status["duration"] = int(time.time() - start_time)
        status["finished"] = True

        if results_ranked:
            sorted_signals = sorted(results_ranked, key=lambda x: -x[0])
            status["results"] = [s[1] for s in sorted_signals]
            msg = f"📊 EMA 7/25 გადაკვეთა ({tf})\n\n" + "\n".join(status["results"])
        else:
            msg = f"ℹ️ არ მოიძებნა EMA 7/25 გადაკვეთა ({tf})\nდრო: {status['duration']} წმ"

        send_telegram(msg)

# მთავარი გვერდი
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", status=status)

# სკანერის გაშვება
@app.route("/start", methods=["POST"])
def start():
    if not status["running"]:
        tf = request.form.get("timeframe")
        thread = threading.Thread(target=scan_loop, args=(tf,))
        thread.start()
    return render_template("index.html", status=status)

# გაჩერება
@app.route("/stop", methods=["POST"])
def stop():
    status["running"] = False
    return render_template("index.html", status=status)

# სტატუსი
@app.route("/status", methods=["GET"])
def get_status():
    return {
        "running": status["running"],
        "duration": status["duration"],
        "finished": status["finished"],
        "total": status["total"]
    }

# Flask-ის გაშვება
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
