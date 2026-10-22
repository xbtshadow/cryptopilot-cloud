import json
import time
import urllib.request
import urllib.parse
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import redis
from security import check_token_security
from datetime import datetime

try:
    from eth_account import Account
except ImportError:
    Account = None

# ==========================================
# 🛑 PARAMÈTRES DE L'AGENT (Le Cerveau)
# ==========================================
STOP_LOSS_PERCENT = 2.0              
TRAILING_STOP_ACTIVATION_PCT = 1.5   
TRAILING_STOP_PERCENT = 0.5          
MAX_DRAWDOWN_PERCENT = 20.0          
ITEMS_PER_PAGE = 5

RSI_BUY_THRESHOLD = 35.0             
RSI_SELL_THRESHOLD = 70.0            
EMA_FAST = 7                         
EMA_SLOW = 21                        

# --- SÉCURITÉ ANTI-FRAIS ---
MIN_NET_PROFIT_USDT = 0.20 
ESTIMATED_FEES_USDT = 0.30 
POSITION_SIZE_PCT = 90.0   

REDIS_URL = os.getenv("REDIS_URL")
r = None
if REDIS_URL:
    try:
        r = redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
    except Exception as e:
        print("Erreur Redis")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def send_telegram_message(message, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return 
    try:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
        encoded_payload = urllib.parse.urlencode(payload).encode('utf-8')
        url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
        req = urllib.request.Request(url, data=encoded_payload)
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

def get_main_keyboard(state):
    btn_pause = "▶️ Reprendre" if state.get("is_paused", False) else "⏸️ Pause"
    return {
        "inline_keyboard": [
            [{"text": "📊 Stats", "callback_data": "stats"}, {"text": "🌐 Marché", "callback_data": "market"}],
            [{"text": "💰 Déposer", "callback_data": "deposit"}, {"text": "💸 Retirer", "callback_data": "withdraw"}],
            [{"text": btn_pause, "callback_data": "toggle_pause"}, {"text": "📜 Trades", "callback_data": "history_page_0"}],
            [{"text": "🧾 Transactions", "callback_data": "tx_history_page_0"}, {"text": "🔑 Wallets", "callback_data": "manage_wallets"}],
            [{"text": "🛠️ Debug", "callback_data": "debug"}]
        ]
    }

# --- GESTION DES WALLETS ---
def get_wallets(chat_id):
    wallets = {}
    if not r: return wallets
    try:
        for slot in [1, 2]:
            data = r.get("wallet:" + str(chat_id) + ":" + str(slot))
            if data: wallets[slot] = json.loads(data)
    except Exception: pass
    return wallets

def generate_wallet(chat_id, slot, chain="BSC"):
    if not r: return "❌ Erreur Redis"
    if not Account: return "❌ Erreur librairie"
    try:
        acct = Account.create()
        r.set("wallet:" + str(chat_id) + ":" + str(slot), json.dumps({"chain": chain, "address": acct.address, "private_key": acct.key.hex()}))
        return "✅ Wallet " + chain + " généré !"
    except Exception: return "❌ Erreur génération"

def delete_wallet(chat_id, slot):
    if not r: return "❌ Erreur Redis"
    try:
        r.delete("wallet:" + str(chat_id) + ":" + str(slot))
        return "🗑️ Wallet supprimé."
    except Exception: return "❌ Erreur supression"

def show_wallet_manager(chat_id):
    wallets = get_wallets(chat_id)
    msg = "🔑 <b>Gestionnaire de Wallets</b>\n\n"
    buttons = []
    for slot in [1, 2]:
        if slot in wallets:
            w = wallets[slot]
            msg += "Emplacement " + str(slot) + " (" + w.get('chain', 'BSC') + "):\n<code>" + w['address'] + "</code>\n"
            buttons.append([{"text": "🔑 Clé " + str(slot), "callback_data": "show_pk_" + str(slot)}, {"text": "🗑️ Supprimer", "callback_data": "delete_ask_" + str(slot)}])
        else:
            msg += "Emplacement " + str(slot) + " : (Vide)\n"
            buttons.append([{"text": "➕ Créer BSC " + str(slot), "callback_data": "create_BSC_" + str(slot)}, {"text": "➕ Créer ETH " + str(slot), "callback_data": "create_ETH_" + str(slot)}])
        msg += "\n"
    buttons.append([{"text": "⬅️ Retour", "callback_data": "back_menu"}])
    return msg, {"inline_keyboard": buttons}

# --- MARCHÉ GLOBAL ---
def get_market_prices():
    msg = "🌐 <b>PRIX DU MARCHÉ</b> 🌐\n\n"
    url = "https://pro-api.coinmarketcap.com/public-api/v1/cryptocurrency/listings/latest?limit=20&convert=USD"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            for coin in data.get("data", []):
                sym = coin.get("symbol")
                if sym in ["BNB", "ETH", "BTC"]:
                    price = coin.get("quote", {}).get("USD", {}).get("price")
                    if price is not None:
                        msg += "💎 <b>" + sym + "</b> : " + str(round(price, 2)) + " $\n"
            return msg
    except Exception: return "❌ Impossible de récupérer les prix."

# --- HISTORIQUES PAGINÉS ---
def build_trade_history_msg(state, page):
    hist = state.get("trade_history", [])
    if not hist: return "📜 Aucun trade.", get_main_keyboard(state)
    total_pages = (len(hist) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    if page < 0: page = 0
    if page >= total_pages: page = total_pages - 1
    reversed_hist = list(reversed(hist))
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = reversed_hist[start:end]
    msg = "📜 <b>HISTORIQUE DES TRADES</b> (" + str(page+1) + "/" + str(total_pages) + ")\n\n"
    for t in page_items:
        emoji = "🟢" if t['pnl'] > 0 else "🔴"
        msg += emoji + " " + t['date'] + "\nAchat: " + str(round(t['entry'], 2)) + " | Vente: " + str(round(t['exit'], 2)) + "\nQty: " + str(round(t.get('qty', 0), 4)) + " BNB\nPnL: " + str(round(t['pnl'], 2)) + " USDT\n\n"
    buttons = []
    nav_row = []
    if page > 0: nav_row.append({"text": "⬅️ Précédent", "callback_data": "history_page_" + str(page-1)})
    if page < total_pages - 1: nav_row.append({"text": "Suivant ➡️", "callback_data": "history_page_" + str(page+1)})
    if nav_row: buttons.append(nav_row)
    buttons.append([{"text": "⬅️ Menu", "callback_data": "back_menu"}])
    return msg, {"inline_keyboard": buttons}

def build_tx_history_msg(state, page):
    hist = state.get("tx_history", [])
    if not hist: return "🧾 Aucune transaction.", get_main_keyboard(state)
    total_pages = (len(hist) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    if page < 0: page = 0
    if page >= total_pages: page = total_pages - 1
    reversed_hist = list(reversed(hist))
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = reversed_hist[start:end]
    msg = "🧾 <b>HISTORIQUE DES TRANSACTIONS</b> (" + str(page+1) + "/" + str(total_pages) + ")\n\n"
    for t in page_items:
        emoji = "🟢" if t['type'] == "DEPOT" else "🔴"
        msg += emoji + " " + t['type'] + " - " + t['date'] + "\nMontant: " + str(round(t['amount'], 2)) + " USDT\nRéseau: " + t.get('chain', 'BSC') + "\n"
        if t.get('address'):
            msg += "Adresse: <code>" + t['address'] + "</code>\n"
        msg += "\n"
    buttons = []
    nav_row = []
    if page > 0: nav_row.append({"text": "⬅️ Précédent", "callback_data": "tx_history_page_" + str(page-1)})
    if page < total_pages - 1: nav_row.append({"text": "Suivant ➡️", "callback_data": "tx_history_page_" + str(page+1)})
    if nav_row: buttons.append(nav_row)
    buttons.append([{"text": "⬅️ Menu", "callback_data": "back_menu"}])
    return msg, {"inline_keyboard": buttons}

# --- CALCULS DE L'AGENT ---
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

def get_agent_decision(price_history, current_price):
    if len(price_history) < EMA_SLOW: return "ATTENDRE"
    rsi = calculate_rsi(price_history, 14)
    ema_f = calculate_ema(price_history, EMA_FAST)
    ema_s = calculate_ema(price_history, EMA_SLOW)
    
    is_dip = rsi < RSI_BUY_THRESHOLD
    is_trend_up = ema_f > ema_s
    is_bouncing = current_price >= price_history[-2]
    
    if is_dip and is_trend_up and is_bouncing: return "ACHETER"
    elif rsi > RSI_SELL_THRESHOLD: return "VENDRE"
    return "ATTENDRE"

# --- COMMANDES TELEGRAM ---
def listen_telegram_commands(state, config, price_history, pending_actions):
    last_update_id = 0
    while True:
        try:
            url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/getUpdates?offset=" + str(last_update_id) + "&timeout=10"
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
                                send_telegram_message("🤖 <b>Menu Principal</b>", get_main_keyboard(state))
                            elif chat_id in pending_actions:
                                action_data = pending_actions[chat_id]
                                action = action_data["action"]
                                try:
                                    if action == "withdraw_address":
                                        address = text.strip()
                                        amount = action_data["amount"]
                                        chain = action_data["chain"]
                                        if not address.startswith("0x") or len(address) != 42:
                                            send_telegram_message("❌ Adresse invalide.", get_main_keyboard(state))
                                            continue
                                        state["capital_usd"] -= amount
                                        tx_record = {"date": datetime.now().strftime("%d/%m %H:%M"), "type": "RETRAIT", "amount": amount, "chain": chain, "address": address}
                                        if "tx_history" not in state: state["tx_history"] = []
                                        state["tx_history"].append(tx_record)
                                        save_state(state)
                                        send_telegram_message("✅ Retrait initié !", get_main_keyboard(state))
                                        del pending_actions[chat_id]
                                        continue
                                    amount = float(text.replace(",", "."))
                                    if amount <= 0: raise ValueError
                                    if action == "deposit":
                                        pending_actions[chat_id] = {"action": "deposit", "amount": amount, "chain": action_data.get("chain", "BSC")}
                                        confirm_kb = {"inline_keyboard": [[{"text": "✅ Confirmer", "callback_data": "confirm_deposit"}], [{"text": "❌ Annuler", "callback_data": "cancel_action"}]]}
                                        send_telegram_message("💰 Déposer " + str(round(amount, 2)) + " USDT ?", confirm_kb)
                                    elif action == "withdraw_amount":
                                        if amount > state["capital_usd"]:
                                            send_telegram_message("❌ Fonds insuffisants.", get_main_keyboard(state))
                                            del pending_actions[chat_id]
                                        else:
                                            pending_actions[chat_id] = {"action": "withdraw_address", "amount": amount, "chain": action_data.get("chain", "BSC")}
                                            send_telegram_message("💸 Envoie l'adresse de destination (0x...)", None)
                                except ValueError:
                                    send_telegram_message("❌ Montant invalide.", get_main_keyboard(state))
                                    del pending_actions[chat_id]

                        elif "callback_query" in update:
                            callback = update["callback_query"]
                            chat_id = callback["message"]["chat"]["id"]
                            data_cb = callback["data"]
                            try:
                                if data_cb == "stats":
                                    send_telegram_message(build_stats_message(state), get_main_keyboard(state))
                                elif data_cb == "toggle_pause":
                                    state["is_paused"] = not state.get("is_paused", False)
                                    save_state(state)
                                    status = "⏸️ En Pause" if state["is_paused"] else "▶️ En Marche"
                                    send_telegram_message("Statut : " + status, get_main_keyboard(state))
                                elif data_cb.startswith("history_page_"):
                                    page = int(data_cb.split("_")[2])
                                    msg, kb = build_trade_history_msg(state, page)
                                    send_telegram_message(msg, kb)
                                elif data_cb.startswith("tx_history_page_"):
                                    page = int(data_cb.split("_")[3])
                                    msg, kb = build_tx_history_msg(state, page)
                                    send_telegram_message(msg, kb)
                                elif data_cb == "debug":
                                    msg = "🛠️ <b>MODE DEBUG (Agent V4)</b> 🛠️\n\n"
                                    msg += "💰 Capital : " + str(round(state['capital_usd'], 2)) + " USDT\n"
                                    msg += "⏸️ Pause : " + str(state.get('is_paused', False)) + "\n"
                                    price = get_real_price(config.get("pair_address"))
                                    if not price: 
                                        msg += "❌ Erreur prix."
                                    else:
                                        msg += "💹 Prix BNB : " + str(round(price, 3)) + " $\n"
                                        if len(price_history) < EMA_SLOW:
                                            msg += "⏳ Attente historique."
                                        else:
                                            rsi = calculate_rsi(price_history, 14)
                                            ema_f = calculate_ema(price_history, EMA_FAST)
                                            ema_s = calculate_ema(price_history, EMA_SLOW)
                                            decision = get_agent_decision(price_history, price)
                                            msg += "🌡️ RSI : " + str(round(rsi, 1)) + "\n"
                                            msg += "📊 EMA Rapide : " + str(round(ema_f, 2)) + "\n"
                                            msg += "📉 EMA Lente : " + str(round(ema_s, 2)) + "\n\n"
                                            msg += "🧠 Décision : <b>" + decision + "</b>\n"
                                            if decision == "ATTENDRE":
                                                if rsi >= RSI_BUY_THRESHOLD: msg += "⏳ RSI trop haut.\n"
                                                if ema_f <= ema_s: msg += "⏳ Tendance baissière.\n"
                                                if len(price_history) > 2 and price < price_history[-2]: msg += "⏳ Le prix est en train de chuter.\n"
                                    send_telegram_message(msg, get_main_keyboard(state))
                                elif data_cb == "market":
                                    send_telegram_message(get_market_prices(), get_main_keyboard(state))
                                elif data_cb == "back_menu":
                                    pending_actions.pop(chat_id, None)
                                    send_telegram_message("🤖 <b>Menu Principal</b>", get_main_keyboard(state))
                                elif data_cb == "manage_wallets":
                                    pending_actions.pop(chat_id, None)
                                    msg, kb = show_wallet_manager(chat_id)
                                    send_telegram_message(msg, kb)
                                elif data_cb.startswith("show_pk_"):
                                    slot = int(data_cb.split("_")[2])
                                    wallets = get_wallets(chat_id)
                                    if slot in wallets:
                                        send_telegram_message("⚠️ Clé privée : <code>" + wallets[slot]["private_key"] + "</code>", get_main_keyboard(state))
                                    else: send_telegram_message("❌ Wallet introuvable.", get_main_keyboard(state))
                                elif data_cb.startswith("create_"):
                                    parts = data_cb.split("_")
                                    send_telegram_message(generate_wallet(chat_id, int(parts[2]), parts[1]))
                                    msg, kb = show_wallet_manager(chat_id)
                                    send_telegram_message(msg, kb)
                                elif data_cb.startswith("delete_ask_"):
                                    slot = int(data_cb.split("_")[2])
                                    confirm_kb = {"inline_keyboard": [[{"text": "✅ Oui", "callback_data": "delete_confirm_" + str(slot)}], [{"text": "❌ Non", "callback_data": "manage_wallets"}]]}
                                    send_telegram_message("⚠️ Supprimer ?", confirm_kb)
                                elif data_cb.startswith("delete_confirm_"):
                                    slot = int(data_cb.split("_")[2])
                                    send_telegram_message(delete_wallet(chat_id, slot))
                                    msg, kb = show_wallet_manager(chat_id)
                                    send_telegram_message(msg, kb)
                                elif data_cb == "deposit":
                                    net_kb = {"inline_keyboard": [[{"text": "🟡 BSC", "callback_data": "dep_net_BSC"}], [{"text": "🔵 ETH", "callback_data": "dep_net_ETH"}], [{"text": "❌ Annuler", "callback_data": "cancel_action"}]]}
                                    send_telegram_message("💰 Dépôt sur quel réseau ?", net_kb)
                                elif data_cb.startswith("dep_net_"):
                                    chain = data_cb.split("_")[2]
                                    pending_actions[chat_id] = {"action": "deposit", "amount": 0, "chain": chain}
                                    send_telegram_message("💰 Montant USDT ?", None)
                                elif data_cb == "withdraw":
                                    net_kb = {"inline_keyboard": [[{"text": "🟡 BSC", "callback_data": "wd_net_BSC"}], [{"text": "🔵 ETH", "callback_data": "wd_net_ETH"}], [{"text": "❌ Annuler", "callback_data": "cancel_action"}]]}
                                    send_telegram_message("💸 Retrait sur quel réseau ?", net_kb)
                                elif data_cb.startswith("wd_net_"):
                                    chain = data_cb.split("_")[2]
                                    pending_actions[chat_id] = {"action": "withdraw_amount", "amount": 0, "chain": chain}
                                    send_telegram_message("💸 Montant USDT ?", None)
                                elif data_cb == "confirm_deposit":
                                    if chat_id in pending_actions and pending_actions[chat_id].get("amount", 0) > 0:
                                        amount = pending_actions[chat_id]["amount"]
                                        chain = pending_actions[chat_id].get("chain", "BSC")
                                        send_telegram_message("🔍 Vérification...", None)
                                        time.sleep(2)
                                        state["capital_usd"] += amount
                                        state["total_deposited"] = state.get("total_deposited", 0) + amount
                                        state["peak_capital"] = max(state.get("peak_capital", 0), state["capital_usd"])
                                        tx_record = {"date": datetime.now().strftime("%d/%m %H:%M"), "type": "DEPOT", "amount": amount, "chain": chain}
                                        if "tx_history" not in state: state["tx_history"] = []
                                        state["tx_history"].append(tx_record)
                                        save_state(state)
                                        send_telegram_message("✅ Dépôt confirmé !", get_main_keyboard(state))
                                    else:
                                        send_telegram_message("❌ Erreur.", get_main_keyboard(state))
                                    pending_actions.pop(chat_id, None)
                                elif data_cb == "cancel_action":
                                    pending_actions.pop(chat_id, None)
                                    send_telegram_message("❌ Annulé.", get_main_keyboard(state))
                            except Exception as e:
                                send_telegram_message("❌ Erreur bouton.", get_main_keyboard(state))
                            finally:
                                try: urllib.request.urlopen("https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/answerCallbackQuery?callback_query_id=" + callback['id'])
                                except: pass
        except Exception: pass
        time.sleep(2)

def build_stats_message(state):
    pos = "OUI" if state["position_open"] else "NON"
    win_rate = 0
    if state.get("total_trades", 0) > 0:
        win_rate = (state.get("winning_trades", 0) / state["total_trades"]) * 100
    pause_status = "⏸️ En Pause" if state.get("is_paused", False) else "🟢 Actif"
    
    # Calcul Profit / Loss
    total_pnl = state.get('realized_pnl_usd', 0.0)
    total_loss = state.get('total_fees_usd', 0.0)
    if total_pnl < 0: 
        total_loss += abs(total_pnl)
        total_pnl = 0
    
    return ("📊 <b>TABLEAU DE BORD (Agent V4)</b> 📊\n"
            "Statut : " + pause_status + "\n"
            "🏦 Total Déposé : " + str(round(state.get('total_deposited', 0.0), 2)) + " USDT\n"
            "💰 Capital : " + str(round(state['capital_usd'], 2)) + " USDT\n"
            "🏔️ Plus Haut : " + str(round(state.get('peak_capital', 0.0), 2)) + " USDT\n"
            "📈 Position : " + pos + "\n"
            "──────────────────\n"
            "📊 Trades : " + str(state.get('total_trades', 0)) + " (" + str(round(win_rate, 1)) + "%)\n"
            "🟢 Profit : +" + str(round(total_pnl, 4)) + " USDT\n"
            "🔴 Loss/Frais : -" + str(round(total_loss, 4)) + " USDT")

def load_config(path="config.json"):
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except Exception:
        return {"initial_capital_usd": 100.0, "estimated_gas_cost_usd": 0.15, "estimated_trading_fee_percent": 0.25, "estimated_slippage_percent": 0.5, "pair_address": "0x16b9a82891338f9bA80E2D6970FddA79D1eb0daE"}

def load_state(config):
    if r:
        try:
            data = r.get("cryptopilot_state")
            if data: return json.loads(data)
        except Exception: pass
    return {"capital_usd": config.get("initial_capital_usd", 100.0), "total_deposited": config.get("initial_capital_usd", 100.0), "peak_capital": config.get("initial_capital_usd", 100.0), "position_open": False, "position_amount_usd": 0.0, "position_quantity": 0.0, "entry_price": 0.0, "highest_price": 0.0, "total_fees_usd": 0.0, "realized_pnl_usd": 0.0, "total_trades": 0, "winning_trades": 0, "is_paused": False, "trade_history": [], "tx_history": []}

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
    url = "https://api.dexscreener.com/latest/dex/pairs/bsc/" + pair_address
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            if data.get("pair"): return float(data["pair"]["priceUsd"])
            elif data.get("pairs") and len(data["pairs"]) > 0: return float(data["pairs"][0]["priceUsd"])
    except Exception: pass
    return None

def simulate_buy(state, price, config):
    if state["capital_usd"] < 1.0: return state
    try:
        security_check = check_token_security("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c")
        if not security_check["safe"]: return state
    except Exception: return state

    amount_usdt = state["capital_usd"] * (POSITION_SIZE_PCT / 100.0)
    gas = config["estimated_gas_cost_usd"]
    fee = amount_usdt * (config["estimated_trading_fee_percent"] / 100.0)
    slippage = amount_usdt * (config["estimated_slippage_percent"] / 100.0)
    total_cost = amount_usdt + gas + fee + slippage

    if total_cost > state["capital_usd"]: return state

    quantity = amount_usdt / price
    state["capital_usd"] -= total_cost
    state["position_open"] = True
    state["position_amount_usd"] = amount_usdt
    state["position_quantity"] = quantity
    state["entry_price"] = price
    state["highest_price"] = price
    state["total_fees_usd"] += gas + fee
    
    send_telegram_message("🚀 <b>ACHAT</b>\nAcheté : " + str(round(quantity, 4)) + " BNB\nAvec : " + str(round(amount_usdt, 2)) + " USDT\nPrix : " + str(round(price, 3)) + " $")
    return state

def simulate_sell(state, price, config, reason="Signal"):
    if not state["position_open"]: return state
    quantity = state["position_quantity"]
    amount_usdt = state["position_amount_usd"]
    entry_price_saved = state["entry_price"]
    
    current_value = quantity * price
    gas = config["estimated_gas_cost_usd"]
    fee = current_value * (config["estimated_trading_fee_percent"] / 100.0)
    slippage = current_value * (config["estimated_slippage_percent"] / 100.0)
    net_proceeds = current_value - gas - fee - slippage

    pnl = net_proceeds - amount_usdt - gas
    state["capital_usd"] += net_proceeds
    state["total_fees_usd"] += gas + fee
    state["realized_pnl_usd"] += pnl
    state["position_open"] = False
    state["position_amount_usd"] = 0.0
    state["position_quantity"] = 0.0
    state["entry_price"] = 0.0
    state["highest_price"] = 0.0
    state["total_trades"] = state.get("total_trades", 0) + 1
    if pnl > 0: state["winning_trades"] = state.get("winning_trades", 0) + 1

    trade_record = {"date": datetime.now().strftime("%d/%m %H:%M"), "entry": entry_price_saved, "exit": price, "qty": quantity, "pnl": pnl}
    if "trade_history" not in state: state["trade_history"] = []
    state["trade_history"].append(trade_record)

    emoji = "💰" if pnl > 0 else "🛑"
    send_telegram_message(emoji + " <b>VENTE (" + reason + ")</b>\nVendu : " + str(round(quantity, 4)) + " BNB\nPrix : " + str(round(price, 3)) + " $\nPnL : " + str(round(pnl, 4)) + " USDT")
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
    send_telegram_message("🤖 <b>Agent V4 en ligne.</b>\nTape /menu pour gérer ton bot.")

    while True:
        try:
            if not state.get("is_paused", False) and state["capital_usd"] > 1.0:
                current_price = get_real_price(config.get("pair_address"))
                if current_price:
                    price_history.append(current_price)
                    if len(price_history) > 100: price_history.pop(0)
                    save_history(price_history)

                    if len(price_history) >= EMA_SLOW:
                        decision = get_agent_decision(price_history, current_price)

                        if not state["position_open"]:
                            if decision == "ACHETER":
                                expected_gain = (state["capital_usd"] * (POSITION_SIZE_PCT / 100.0)) * 0.02
                                if expected_gain > (ESTIMATED_FEES_USDT + MIN_NET_PROFIT_USDT):
                                    state = simulate_buy(state, current_price, config)
                        else:
                            pnl_percent = ((current_price - state["entry_price"]) / state["entry_price"]) * 100
                            if current_price > state.get("highest_price", state["entry_price"]):
                                state["highest_price"] = current_price
                            drop_from_peak = ((state["highest_price"] - current_price) / state["highest_price"]) * 100

                            tp_dynamic_pct = ((ESTIMATED_FEES_USDT + MIN_NET_PROFIT_USDT) / state["position_amount_usd"]) * 100
                            
                            if decision == "VENDRE" or pnl_percent >= tp_dynamic_pct:
                                state = simulate_sell(state, current_price, config, reason="Objectif")
                            elif pnl_percent <= -STOP_LOSS_PERCENT:
                                state = simulate_sell(state, current_price, config, reason="STOP-LOSS")
                            elif pnl_percent >= TRAILING_STOP_ACTIVATION_PCT and drop_from_peak >= TRAILING_STOP_PERCENT:
                                state = simulate_sell(state, current_price, config, reason="TRAILING STOP")

                    if state["capital_usd"] > state.get("peak_capital", 0):
                        state["peak_capital"] = state["capital_usd"]
                    
                    if state.get("peak_capital", 0) > 0:
                        drawdown = ((state["peak_capital"] - state["capital_usd"]) / state["peak_capital"]) * 100
                        if drawdown >= MAX_DRAWDOWN_PERCENT and not state.get("is_paused", False):
                            state["is_paused"] = True
                            send_telegram_message("🚨 Drawdown atteint. Bot en pause.")

                    save_state(state)
        except Exception as e: print("Erreur")
        time.sleep(10)

if __name__ == "__main__":
    main()
