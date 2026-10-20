import json
import logging
import time
import urllib.request
import urllib.parse
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import redis
from security import check_token_security

try:
    from eth_account import Account
except ImportError:
    Account = None

REDIS_URL = os.getenv("REDIS_URL")
r = None
if REDIS_URL:
    try:
        r = redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
    except Exception as e:
        print(f"⚠️ Redis : {e}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def send_telegram_message(message, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return 
    try:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
        encoded_payload = urllib.parse.urlencode(payload).encode('utf-8')
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        req = urllib.request.Request(url, data=encoded_payload)
        urllib.request.urlopen(req, timeout=5)
    except Exception as e: print(f"⚠️ Erreur Telegram : {e}")

def get_main_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "📊 Statistiques", "callback_data": "stats"}],
            [{"text": "💰 Déposer", "callback_data": "deposit"}, {"text": "💸 Retirer", "callback_data": "withdraw"}],
            [{"text": "🔑 Gérer mes Wallets", "callback_data": "manage_wallets"}],
            [{"text": "💹 Prix Actuel", "callback_data": "price"}]
        ]
    }

# --- GESTION DES WALLETS ---
def get_wallets(chat_id):
    wallets = {}
    if not r: return wallets
    try:
        for slot in [1, 2]:
            data = r.get(f"wallet:{chat_id}:{slot}")
            if data:
                wallets[slot] = json.loads(data)
    except Exception:
        pass
    return wallets

def generate_wallet(chat_id, slot):
    if not r: return "❌ Erreur : Redis non connecté."
    if not Account: return "❌ Erreur : librairie `eth-account` manquante."
    
    try:
        acct = Account.create()
        wallet_data = {"address": acct.address, "private_key": acct.key.hex()}
        r.set(f"wallet:{chat_id}:{slot}", json.dumps(wallet_data))
        return f"✅ <b>Wallet {slot} généré !</b>\n📍 <code>{acct.address}</code>"
    except Exception as e:
        return f"❌ Erreur : {str(e)}"

def delete_wallet(chat_id, slot):
    if not r: return "❌ Erreur : Redis non connecté."
    try:
        r.delete(f"wallet:{chat_id}:{slot}")
        return f"🗑️ Le Wallet {slot} a été supprimé avec succès."
    except Exception as e:
        return f"❌ Erreur : {str(e)}"

def show_wallet_manager(chat_id):
    wallets = get_wallets(chat_id)
    msg = "🔑 <b>Gestionnaire de Wallets</b>\n\n"
    buttons = []

    for slot in [1, 2]:
        if slot in wallets:
            msg += f"<b>Emplacement {slot} :</b>\n<code>{wallets[slot]['address']}</code>\n"
            buttons.append([{"text": f"🗑️ Supprimer Wallet {slot}", "callback_data": f"delete_ask_{slot}"}])
        else:
            msg += f"<b>Emplacement {slot} :</b> (Vide)\n"
            buttons.append([{"text": f"➕ Créer Wallet {slot}", "callback_data": f"create_{slot}"}])
        msg += "\n"

    buttons.append([{"text": "⬅️ Retour au menu", "callback_data": "back_menu"}])
    kb = {"inline_keyboard": buttons}
    return msg, kb

# --- GESTION DES COMMANDES TELEGRAM ---
def listen_telegram_commands(state, config, price_history, pending_actions):
    last_update_id = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id}&timeout=10"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=15) as response:
                data = json.loads(response.read().decode())
                if data.get("result"):
                    for update in data["result"]:
                        last_update_id = update["update_id"] + 1
                        
                        if "message" in update and "text" in update["message"]:
                            text = update["message"]["text"]
                            chat_id = update["message"]["chat"]["id"]
                            if str(chat_id) != str(TELEGRAM_CHAT_ID): continue
                            
                            if text in ["/start", "/menu"]:
                                send_telegram_message("🤖 <b>Menu Principal</b>", get_main_keyboard())
                            elif text == "/stats":
                                send_telegram_message(build_stats_message(state, config), get_main_keyboard())
                            elif chat_id in pending_actions:
                                action = pending_actions[chat_id]["action"]
                                try:
                                    amount = float(text.replace(",", "."))
                                    if amount <= 0: raise ValueError
                                    
                                    if action == "deposit":
                                        pending_actions[chat_id]["amount"] = amount # On garde le montant !
                                        confirm_kb = {"inline_keyboard": [[{"text": "✅ Confirmer", "callback_data": f"confirm_{action}"}], [{"text": "❌ Annuler", "callback_data": "cancel_action"}]]}
                                        send_telegram_message(f"💰 Tu souhaites déposer <b>{amount:.2f} $</b>.\nConfirmes-tu ?", confirm_kb)
                                    elif action == "withdraw":
                                        if amount > state["capital_usd"]:
                                            send_telegram_message(f"❌ Fonds insuffisants. Ton capital de trading est de <b>{state['capital_usd']:.2f} $</b>.", get_main_keyboard())
                                            del pending_actions[chat_id] # On annule l'action
                                        else:
                                            pending_actions[chat_id]["amount"] = amount # On garde le montant !
                                            confirm_kb = {"inline_keyboard": [[{"text": "✅ Confirmer", "callback_data": f"confirm_{action}"}], [{"text": "❌ Annuler", "callback_data": "cancel_action"}]]}
                                            send_telegram_message(f"💸 Tu souhaites retirer <b>{amount:.2f} $</b>.\nConfirmes-tu ?", confirm_kb)
                                except ValueError:
                                    send_telegram_message("❌ Montant invalide. Envoie un nombre positif (ex: 50).", get_main_keyboard())
                                    del pending_actions[chat_id] # On annule l'action

                        elif "callback_query" in update:
                            callback = update["callback_query"]
                            chat_id = callback["message"]["chat"]["id"]
                            data_cb = callback["data"]
                            
                            try:
                                if data_cb == "stats":
                                    send_telegram_message(build_stats_message(state, config), get_main_keyboard())
                                elif data_cb == "back_menu":
                                    pending_actions.pop(chat_id, None)
                                    send_telegram_message("🤖 <b>Menu Principal</b>", get_main_keyboard())
                                elif data_cb == "manage_wallets":
                                    pending_actions.pop(chat_id, None)
                                    msg, kb = show_wallet_manager(chat_id)
                                    send_telegram_message(msg, kb)
                                elif data_cb.startswith("create_"):
                                    slot = int(data_cb.split("_")[1])
                                    result = generate_wallet(chat_id, slot)
                                    send_telegram_message(result)
                                    msg, kb = show_wallet_manager(chat_id)
                                    send_telegram_message(msg, kb)
                                elif data_cb.startswith("delete_ask_"):
                                    slot = int(data_cb.split("_")[2])
                                    confirm_kb = {"inline_keyboard": [
                                        [{"text": "✅ Oui, supprimer", "callback_data": f"delete_confirm_{slot}"}],
                                        [{"text": "❌ Annuler", "callback_data": "manage_wallets"}]
                                    ]}
                                    send_telegram_message(f"⚠️ Es-tu sûr de vouloir supprimer le Wallet {slot} ? Cette action est irréversible.", confirm_kb)
                                elif data_cb.startswith("delete_confirm_"):
                                    slot = int(data_cb.split("_")[2])
                                    result = delete_wallet(chat_id, slot)
                                    send_telegram_message(result)
                                    msg, kb = show_wallet_manager(chat_id)
                                    send_telegram_message(msg, kb)
                                elif data_cb == "deposit":
                                    pending_actions[chat_id] = {"action": "deposit", "amount": 0}
                                    send_telegram_message("💰 <b>Dépôt</b>\nQuel montant en <b>$</b> souhaites-tu déposer ?\n(Envoie un nombre, ex: 50)", None)
                                elif data_cb == "withdraw":
                                    pending_actions[chat_id] = {"action": "withdraw", "amount": 0}
                                    send_telegram_message("💸 <b>Retrait</b>\nQuel montant en <b>$</b> souhaites-tu retirer ?\n(Envoie un nombre, ex: 20)", None)
                                
                                # --- C'EST ICI QUE LE BUG ETAIT CORRIGE ---
                                elif data_cb.startswith("confirm_"):
                                    action = data_cb.replace("confirm_", "")
                                    if chat_id in pending_actions and pending_actions[chat_id].get("amount", 0) > 0:
                                        amount = pending_actions[chat_id]["amount"]
                                        if action == "deposit":
                                            state["capital_usd"] += amount
                                            send_telegram_message(f"✅ Dépôt de <b>{amount:.2f} $</b> effectué !", get_main_keyboard())
                                        elif action == "withdraw":
                                            state["capital_usd"] -= amount
                                            send_telegram_message(f"✅ Retrait de <b>{amount:.2f} $</b> effectué !", get_main_keyboard())
                                        save_state(state)
                                    else:
                                        send_telegram_message("❌ Aucune opération en attente.", get_main_keyboard())
                                    pending_actions.pop(chat_id, None) # On nettoie une fois terminé
                                
                                elif data_cb == "cancel_action":
                                    pending_actions.pop(chat_id, None)
                                    send_telegram_message("❌ Opération annulée.", get_main_keyboard())
                                
                                elif data_cb == "price":
                                    if price_history:
                                        send_telegram_message(f"💹 Prix : <b>{price_history[-1]:.3f} $</b>", get_main_keyboard())
                            except Exception as e:
                                send_telegram_message(f"❌ Erreur de traitement : {e}", get_main_keyboard())
                            finally:
                                try:
                                    urllib.request.urlopen(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery?callback_query_id={callback['id']}")
                                except: pass
        except Exception as e: print(f"Erreur Telegram : {e}")
        time.sleep(1)

def build_stats_message(state, config):
    total = state["capital_usd"] + state["safe_capital_usd"]
    pos = "OUI" if state["position_open"] else "NON"
    return (f"📊 <b>TABLEAU DE BORD</b> 📊\n"
            f"💰 Capital Trading : <b>{state['capital_usd']:.2f} $</b>\n"
            f"🔒 Coffre-fort : <b>{state['safe_capital_usd']:.2f} $</b>\n"
            f"💎 Total : <b>{total:.2f} $</b>\n"
            f"📈 Position Ouverte : {pos}\n"
            f"💵 PnL Réalisé : {state['realized_pnl_usd']:+.4f} $\n"
            f"💸 Frais payés : {state['total_fees_usd']:.4f} $")

def load_config(path="config.json"):
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except Exception:
        return {"initial_capital_usd": 100.0, "safe_capital_percentage": 50.0, "profit_secure_percentage": 80.0, "position_size_percent": 60.0, "stop_loss_percent": 5.0, "anti_crash_drop_percent": 30.0, "take_profit_percent": 15.0, "trailing_stop_activation_pct": 8.0, "trailing_stop_percent": 2.0, "ema_fast": 5, "ema_slow": 15, "rsi_period": 10, "estimated_gas_cost_usd": 0.15, "estimated_trading_fee_percent": 0.25, "estimated_slippage_percent": 0.5, "pair_address": "0x8040E01d9e5c9cCF7E4E6C34b1E90F9dEC20D994"}

def load_state(config):
    if r:
        try:
            data = r.get("cryptopilot_state")
            if data: return json.loads(data)
        except Exception: pass
    total_capital = config.get("initial_capital_usd", 100.0)
    safe_amount = total_capital * (config.get("safe_capital_percentage", 50.0) / 100.0)
    return {"capital_usd": total_capital - safe_amount, "safe_capital_usd": safe_amount, "position_open": False, "position_amount_usd": 0.0, "entry_price": 0.0, "highest_price": 0.0, "total_fees_usd": 0.0, "realized_pnl_usd": 0.0}

def save_state(state):
    if r:
        try: r.set("cryptopilot_state", json.dumps(state))
        except Exception: pass

def load_history():
    if r:
        try:
            data = r.get("cryptopilot_history")
            if data: return json.loads(data)
        except Exception: pass
    return []

def save_history(history):
    if r:
        try: r.set("cryptopilot_history", json.dumps(history[-100:]))
        except Exception: pass

def get_real_price(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/bsc/{pair_address}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            if data.get("pair"): return float(data["pair"]["priceUsd"])
            elif data.get("pairs") and len(data["pairs"]) > 0: return float(data["pairs"][0]["priceUsd"])
    except Exception: pass
    return None

def calculate_ema(prices, period):
    if len(prices) < period: return prices[-1]
    ema = prices[0]
    multiplier = 2 / (period + 1)
    for price in prices[1:]: ema = (ema * (1 - multiplier)) + (price * multiplier)
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
    try:
        security_check = check_token_security("0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82")
        if not security_check["safe"]: return state
    except Exception: return state

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
    send_telegram_message(f"🚀 ACHAT !\nPrix : {price:.3f} $\nMontant : {amount_usd:.2f} $")
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
        secure_amount = pnl * (config.get("profit_secure_percentage", 80.0) / 100.0)
        state["capital_usd"] -= secure_amount
        state["safe_capital_usd"] += secure_amount

    emoji = "💰" if pnl > 0 else "🛑"
    send_telegram_message(f"{emoji} VENTE ({reason}) !\nPrix : {price:.3f} $\nPnL Net : {pnl:+.4f} $")
    return state

class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is alive")
    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), WebHandler)
    server.serve_forever()

def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    config = load_config()
    state = load_state(config)
    price_history = load_history()
    pending_actions = {}
    threading.Thread(target=listen_telegram_commands, args=(state, config, price_history, pending_actions), daemon=True).start()
    send_telegram_message("🤖 <b>Bot en ligne</b>\nTape /menu pour gérer tes wallets et voir tes stats.")

    while True:
        try:
            current_price = get_real_price(config.get("pair_address", "0x8040E01d9e5c9cCF7E4E6C34b1E90F9dEC20D994"))
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
                        if current_price > state["highest_price"]: state["highest_price"] = current_price
                        pnl_percent = ((current_price - state["entry_price"]) / state["entry_price"]) * 100
                        drop_from_peak = ((state["highest_price"] - current_price) / state["highest_price"]) * 100
                        
                        if pnl_percent <= -config["anti_crash_drop_percent"]: state = simulate_sell(state, current_price, config, reason="ANTI-CRASH")
                        elif pnl_percent >= config["take_profit_percent"]: state = simulate_sell(state, current_price, config, reason="TAKE-PROFIT")
                        elif pnl_percent >= config["trailing_stop_activation_pct"] and drop_from_peak >= config["trailing_stop_percent"]: state = simulate_sell(state, current_price, config, reason="TRAILING STOP")
                        elif pnl_percent <= -config["stop_loss_percent"]: state = simulate_sell(state, current_price, config, reason="STOP-LOSS")

                save_state(state)
        except Exception as e: print(f"Erreur : {e}")
        time.sleep(10)

if __name__ == "__main__":
    main()
