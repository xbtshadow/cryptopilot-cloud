import json
import logging
import time
import urllib.request
import urllib.parse
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from security import check_token_security

# --- LECTURE DES SECRETS DEPUIS LE CLOUD ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return 
    try:
        encoded_message = urllib.parse.quote_plus(message)
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={TELEGRAM_CHAT_ID}&text={encoded_message}"
        urllib.request.urlopen(url, timeout=5)
    except Exception as e:
        print(f"⚠️ Erreur Telegram : {e}")

# --- MINI SERVEUR WEB POUR UPTIMEROBOT / RENDER ---
class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is alive")

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), WebHandler)
    server.serve_forever()

# --- LE RESTE DE TON CODE ---
def load_config(path="config.json"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "initial_capital_usd": 100.0, "safe_capital_percentage": 50.0,
            "profit_secure_percentage": 80.0, "position_size_percent": 60.0,
            "stop_loss_percent": 5.0, "anti_crash_drop_percent": 30.0,
            "take_profit_percent": 10.0, "trailing_stop_activation_pct": 5.0,
            "trailing_stop_percent": 2.0, "ema_fast": 5, "ema_slow": 15,
            "rsi_period": 10, "estimated_gas_cost_usd": 0.15,
            "estimated_trading_fee_percent": 0.25, "estimated_slippage_percent": 0.5
        }

def load_state(path="state.json"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        config = load_config()
        total_capital = config.get("initial_capital_usd", 10.0)
        safe_pct = config.get("safe_capital_percentage", 50.0)
        safe_amount = total_capital * (safe_pct / 100.0)
        trade_amount = total_capital - safe_amount
        return {
            "capital_usd": trade_amount, "safe_capital_usd": safe_amount,
            "position_open": False, "position_amount_usd": 0.0,
            "entry_price": 0.0, "highest_price": 0.0,
            "total_fees_usd": 0.0, "realized_pnl_usd": 0.0
        }

def save_state(state, path="state.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def load_history(path="history.json"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def save_history(history, path="history.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f)

def get_real_price():
    url = "https://api.dexscreener.com/latest/dex/pairs/bsc/0x16b9a82891338f9bA80E2D6970FddA79D1eb0daE"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            if data.get("pair"):
                return float(data["pair"]["priceUsd"])
    except Exception:
        pass
    return None

def calculate_ema(prices, period):
    if len(prices) < period: return prices[-1]
    ema = prices[0]
    multiplier = 2 / (period + 1)
    for price in prices[1:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_rsi(prices, period=14):
    if len(prices) <= period: return 50.0
    gains, losses = 0, 0
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        if diff > 0: gains += diff
        elif diff < 0: losses -= diff
    if losses == 0 and gains == 0: return 50.0
    if losses == 0: return 100.0
    if gains == 0: return 0.0
    rs = (gains / period) / (losses / period)
    return 100 - (100 / (1 + rs))

def simulate_buy(state, price, config):
    token_address = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c" 
    try:
        security_check = check_token_security(token_address)
        if not security_check["safe"]:
            return state
    except Exception:
        return state

    amount_usd = state["capital_usd"] * (config["position_size_percent"] / 100.0)
    gas = config["estimated_gas_cost_usd"]
    fee = amount_usd * (config["estimated_trading_fee_percent"] / 100.0)
    slippage = amount_usd * (config["estimated_slippage_percent"] / 100.0)
    total_cost = amount_usd + gas + fee + slippage

    if total_cost > state["capital_usd"]: return state

    state["capital_usd"] -= total_cost
    state["position_open"] = True
    state["position_amount_usd"] = amount_usd
    state["entry_price"] = price
    state["highest_price"] = price
    state["total_fees_usd"] += gas + fee
    send_telegram_message(f"🚀 ACHAT !\nPrix : {price:.2f} $\nMontant : {amount_usd:.2f} $")
    return state

def simulate_sell(state, price, config, reason="Signal"):
    if not state["position_open"]: return state

    amount_usd = state["position_amount_usd"]
    current_value = amount_usd * (price / state["entry_price"])
    gas = config["estimated_gas_cost_usd"]
    fee = current_value * (config["estimated_trading_fee_percent"] / 100.0)
    slippage = current_value * (config["estimated_slippage_percent"] / 100.0)
    net_proceeds = current_value - gas - fee - slippage

    pnl = net_proceeds - amount_usd - gas
    state["capital_usd"] += net_proceeds
    state["total_fees_usd"] += gas + fee
    state["realized_pnl_usd"] += pnl
    state["position_open"] = False
    state["position_amount_usd"] = 0.0
    state["entry_price"] = 0.0
    state["highest_price"] = 0.0

    if pnl > 0:
        profit_secure_pct = config.get("profit_secure_percentage", 80.0)
        secure_amount = pnl * (profit_secure_pct / 100.0)
        state["capital_usd"] -= secure_amount
        state["safe_capital_usd"] += secure_amount

    emoji = "💰" if pnl > 0 else "🛑"
    send_telegram_message(f"{emoji} VENTE ({reason}) !\nPrix : {price:.2f} $\nPnL Net : {pnl:+.4f} $")
    return state

def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    
    config = load_config()
    state = load_state()
    price_history = load_history()
    
    send_telegram_message("🤖 Le bot CryptoPilot est EN LIGNE sur le Cloud 24/7 !")

    while True:
        try:
            current_price = get_real_price()
            if current_price:
                price_history.append(current_price)
                if len(price_history) > 100: price_history.pop(0)
                save_history(price_history)

                if len(price_history) >= config["ema_slow"]:
                    ema_fast = calculate_ema(price_history, config["ema_fast"])
                    ema_slow = calculate_ema(price_history, config["ema_slow"])
                    rsi = calculate_rsi(price_history, config["rsi_period"])

                    if not state["position_open"]:
                        is_price_falling = len(price_history) >= 3 and current_price < price_history[-2]
                        if ema_fast > ema_slow and rsi < 70 and not is_price_falling:
                            state = simulate_buy(state, current_price, config)
                    else:
                        if current_price > state["highest_price"]:
                            state["highest_price"] = current_price
                        
                        pnl_percent = ((current_price - state["entry_price"]) / state["entry_price"]) * 100
                        drop_from_peak = ((state["highest_price"] - current_price) / state["highest_price"]) * 100
                        
                        if pnl_percent >= config["take_profit_percent"]:
                            state = simulate_sell(state, current_price, config, reason="TAKE-PROFIT")
                        elif pnl_percent >= config["trailing_stop_activation_pct"] and drop_from_peak >= config["trailing_stop_percent"]:
                            state = simulate_sell(state, current_price, config, reason="TRAILING STOP")
                        elif pnl_percent <= -config["anti_crash_drop_percent"]:
                            state = simulate_sell(state, current_price, config, reason="ANTI-CRASH")
                        elif pnl_percent <= -config["stop_loss_percent"]:
                            state = simulate_sell(state, current_price, config, reason="STOP-LOSS")

                save_state(state)
        except Exception as e:
            print(f"Erreur : {e}")
            
        time.sleep(10)

if __name__ == "__main__":
    main()