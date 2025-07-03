from flask import Flask, render_template, request
import ccxt
import pandas as pd
import ta
import time
import requests
import threading

app = Flask(__name__)

BOT_TOKEN = "შენი_ბოტის_ტოკენი"
CHAT_ID = "შენი_ჩათ_აიდი"

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

def get_symbols():
    try:
        markets = exchange.load_markets()
        symbols = [s for s in markets if markets[s].get('contract') and markets[s]['quote'] == 'USDT']
        return symbols
    except Exception as e:
        print(f"get_symbols შეცდომა: {e}")
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
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=52)
                if len(ohlcv) < 52:
                    continue

                df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
                df['ema7'] = ta.trend.ema_indicator(df['close'], window=7)
                df['ema25'] = ta.trend.ema_indicator(df['close'], window=25)

                ema7_prev = df['ema7'].iloc[-3]
                ema25_prev = df['ema25'].iloc[-3]
                ema7_curr = df['ema7'].iloc[-2]
                ema25_curr = df['ema25'].iloc[-2]

                candle = df.iloc[-2]
                prev_candle = df.iloc[-3]

                direction = None
                if ema7_prev < ema25_prev and ema7_curr > ema25_curr:
                    if candle['high'] > prev_candle['high'] and candle['close'] > candle['open']:
                        direction = "BUY"

                elif ema7_prev > ema25_prev and ema7_curr < ema25_curr:
                    if candle['low'] < prev_candle['low'] and candle['close'] < candle['open']:
                        direction = "SELL"

                if direction:
                    indicators = check_indicators(df)
                    results.append({
                        "symbol": symbol,
                        "direction": direction,
                        "indicators": indicators,
                        "match_count": len(indicators)
                    })

            except Exception as e:
                print(f"{symbol} შეცდომა: {e}")

            time.sleep(0.4)

        status["duration"] = int(time.time() - start)
        status["finished"] = True

        if results:
            sorted_results = sorted(results, key=lambda x: -x['match_count'])
            best = sorted_results[0]
            lines = [f"📊 საუკეთესო ქოინი: {best['symbol']} ({best['match_count']} ინდიკატორი)\n" +
                     " + ".join(best['indicators']) + "\n"]

            for r in sorted_results:
                lines.append(f"✅ {r['direction']}: {r['symbol']} ({' + '.join(r['indicators'])})")

            msg = f"📈 EMA 7/25 გადაკვეთა ({tf})\n\n" + "\n".join(lines)
        else:
            msg = f"❌ არ მოიძებნა გადაკვეთა\nტაიმფრეიმი: {tf}\nშემოწმდა: {len(symbols)} ქოინი"

        send_telegram(msg)
        time.sleep(300)

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
