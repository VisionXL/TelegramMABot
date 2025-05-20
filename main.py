import json, threading, time, requests, numpy as np
from flask import Flask, request
import websocket

# === Load and save config ===

CONFIG_FILE = "config.json"

def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"bot_token": "", "chat_id": "", "symbols": []}

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

config = load_config()

bot_token = config.get("bot_token")
chat_id = config.get("chat_id")
symbols = config.get("symbols", [])  # List of dicts: {name, timeframes: [{fast_ma, slow_ma, granularity}]}

# === Telegram send message ===
def send_telegram(text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("Telegram send error:", e)

# === Logging crossovers ===
def log_event(text):
    with open("events.log", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {text}\n")

# === Crossover check ===
def check_crossover(data):
    closes = data["closes"]
    fast = data["fast_ma"]
    slow = data["slow_ma"]

    if len(closes) < slow + 2:
        return

    fast_now = np.mean(closes[-fast:])
    slow_now = np.mean(closes[-slow:])
    fast_prev = np.mean(closes[-fast - 1:-1])
    slow_prev = np.mean(closes[-slow - 1:-1])

    sym = data["name"]
    tf = int(data["granularity"]) // 60

    if fast_prev < slow_prev and fast_now > slow_now:
        msg = f"[{sym} {tf}m] Bullish MA crossover!"
        send_telegram(msg)
        log_event(msg)
    elif fast_prev > slow_prev and fast_now < slow_now:
        msg = f"[{sym} {tf}m] Bearish MA crossover!"
        send_telegram(msg)
        log_event(msg)

# === Manage WebSocket per symbol/timeframe ===
class Monitor:
    def __init__(self, name, fast_ma, slow_ma, granularity):
        self.name = name
        self.fast_ma = fast_ma
        self.slow_ma = slow_ma
        self.granularity = granularity
        self.closes = []
        self.ws = None
        self.thread = threading.Thread(target=self.run)
        self.thread.daemon = True
        self.active = True

    def on_message(self, ws, message):
        try:
            msg = json.loads(message)
            candles = msg.get("candles")
            if candles:
                closes = [float(c["close"]) for c in candles]
                self.closes = closes
                check_crossover({
                    "name": self.name,
                    "fast_ma": self.fast_ma,
                    "slow_ma": self.slow_ma,
                    "granularity": self.granularity,
                    "closes": self.closes
                })
        except Exception as e:
            print(f"[{self.name} {int(self.granularity)//60}m] Message error: {e}")

    def on_open(self, ws):
        req = {
            "candles": self.name,
            "subscribe": 1,
            "style": "candles",
            "granularity": self.granularity
        }
        ws.send(json.dumps(req))

    def on_close(self, ws, close_status_code, close_msg):
        print(f"[{self.name} {int(self.granularity)//60}m] WebSocket closed")

    def run(self):
        while self.active:
            try:
                websocket.enableTrace(True)
                self.ws = websocket.WebSocketApp(
                    "wss://ws.deriv.com/websockets/v3",
                    on_message=self.on_message,
                    on_open=self.on_open,
                    on_close=self.on_close
                )
                self.ws.run_forever()
            except Exception as e:
                print(f"[{self.name} {int(self.granularity)//60}m] WS error: {e}")
            time.sleep(5)

    def start(self):
        self.thread.start()

    def stop(self):
        self.active = False
        if self.ws:
            self.ws.close()

# === Global monitors dictionary to manage all active monitors ===
monitors = {}

# === Start monitors from config ===
def start_all_monitors():
    for sym in symbols or []:
        name = sym["name"]
        for tf in sym.get("timeframes", []):
            key = f"{name}_{tf['granularity']}"
            if key not in monitors:
                m = Monitor(name, tf["fast_ma"], tf["slow_ma"], tf["granularity"])
                monitors[key] = m
                m.start()

# === Stop all monitors ===
def stop_all_monitors():
    for m in monitors.values():
        m.stop()
    monitors.clear()

# === Telegram webhook handler ===
app = Flask('')

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    data = request.json
    if not data or "message" not in data:
        return {"ok": True}

    message = data["message"]
    chat = message.get("chat", {})
    text = message.get("text", "").strip()
    chat_id_msg = chat.get("id")

    # Only accept messages from configured chat_id
    if str(chat_id_msg) != str(chat_id):
        return {"ok": True}

    # Handle commands
    if text.startswith("/"):
        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "/addsymbol":
            # /addsymbol SYMBOL FAST_MA SLOW_MA GRANULARITY_SECONDS
            if len(parts) != 5:
                send_telegram("Usage: /addsymbol SYMBOL FAST_MA SLOW_MA GRANULARITY_SECONDS")
                return {"ok": True}
            sym_name = parts[1].upper()
            try:
                fast_ma = int(parts[2])
                slow_ma = int(parts[3])
                gran = int(parts[4])
            except ValueError:
                send_telegram("MA periods and granularity must be integers.")
                return {"ok": True}
            # Find symbol in config or add new
            sym = next((s for s in symbols or [] if s["name"] == sym_name), None)
            if sym is None:
                sym = {"name": sym_name, "timeframes": []}
                symbols.append(sym)
            # Check if timeframe exists
            if any(tf["granularity"] == gran for tf in sym["timeframes"]):
                send_telegram(f"{sym_name} already has timeframe {gran}s")
                return {"ok": True}
            sym["timeframes"].append({
                "fast_ma": fast_ma,
                "slow_ma": slow_ma,
                "granularity": gran
            })
            save_config(config)
            # Start monitor immediately
            key = f"{sym_name}_{gran}"
            if key not in monitors:
                m = Monitor(sym_name, fast_ma, slow_ma, gran)
                monitors[key] = m
                m.start()
            send_telegram(f"Added {sym_name} {gran}s with fast MA={fast_ma} slow MA={slow_ma}")
            return {"ok": True}

        elif cmd == "/remsymbol":
            # /remsymbol SYMBOL GRANULARITY_SECONDS
            if len(parts) != 3:
                send_telegram("Usage: /remsymbol SYMBOL GRANULARITY_SECONDS")
                return {"ok": True}
            sym_name = parts[1].upper()
            try:
                gran = int(parts[2])
            except ValueError:
                send_telegram("Granularity must be integer seconds.")
                return {"ok": True}
            sym = next((s for s in symbols or [] if s["name"] == sym_name), None)
            if not sym:
                send_telegram(f"No symbol {sym_name} found.")
                return {"ok": True}
            tf_to_remove = None
            for tf in sym["timeframes"]:
                if tf["granularity"] == gran:
                    tf_to_remove = tf
                    break
            if not tf_to_remove:
                send_telegram(f"No timeframe {gran}s found for {sym_name}")
                return {"ok": True}
            sym["timeframes"].remove(tf_to_remove)
            # Stop monitor if running
            key = f"{sym_name}_{gran}"
            if key in monitors:
                monitors[key].stop()
                del monitors[key]

            # If symbol has no timeframes left, remove symbol entirely
            if len(sym["timeframes"]) == 0:
                symbols.remove(sym)

            save_config(config)
            send_telegram(f"Removed {sym_name} {gran}s monitor.")
            return {"ok": True}

        elif cmd == "/listsymbols":
            if not symbols:
                send_telegram("No symbols are being monitored.")
            else:
                lines = []
                for sym in symbols:
                    tfs = ', '.join([f"{tf['granularity']//60}m (fast={tf['fast_ma']} slow={tf['slow_ma']})" for tf in sym["timeframes"]])
                    lines.append(f"{sym['name']}: {tfs}")
                send_telegram("Currently monitored symbols:\n" + "\n".join(lines))
            return {"ok": True}

        elif cmd == "/status":
            active = []
            for key, mon in monitors.items():
                active.append(f"{mon.name} {int(mon.granularity)//60}m")
            if active:
                send_telegram("Active monitors:\n" + "\n".join(active))
            else:
                send_telegram("No active monitors.")
            return {"ok": True}

        else:
            send_telegram("Unknown command. Available:\n/addsymbol\n/remsymbol\n/listsymbols\n/status")
            return {"ok": True}

    return {"ok": True}

# === Flask keep-alive server ===
@app.route('/')
def home():
    info = []
    for sym in symbols or []:
        for tf in sym.get("timeframes", []):
            info.append(f"{sym['name']}({int(tf['granularity'])//60}m)")
    return "Monitoring: " + ", ".join(info)

# === Start the bot ===
if __name__ == "__main__":
    start_all_monitors()
    app.run(host='0.0.0.0', port=8080)
