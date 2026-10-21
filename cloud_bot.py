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
# 🛑 PARAMÈTRES DE TRADING
# ==========================================
STOP_LOSS_PERCENT = 5.0              
TRAILING_STOP_ACTIVATION_PCT = 10.0  
TRAILING_STOP_PERCENT = 2.0          
TAKE_PROFIT_PERCENT = 50.0           
MAX_DRAWDOWN_PERCENT = 20.0          
ITEMS_PER_PAGE = 5

# --- STRATÉGIE "BUY THE DIP" ---
RSI_BUY_THRESHOLD = 40.0  
RSI_SELL_THRESHOLD = 70.0 

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

last_error_time = 0

def send_telegram_message(message, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return 
    try:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
        encoded_payload = urllib.parse.urlencode(payload).encode('utf-8')
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        req = urllib.request.Request(url, data=encoded_payload)
        urllib.request.urlopen(req, timeout=5)
    except Exception as e: print(f"Erreur envoi Telegram: {e}")

def get_main_keyboard(state):
    btn_pause = "▶️ Reprendre" if state.get("is_paused", False) else "⏸️ Pause"
    return {
        "inline_keyboard": [
            [{"text": "📊 Stats", "callback_data": "stats"}, {"text": "🌐 Marché", "callback_data": "market"}],
            [{"text": "💰 Déposer", "callback_data": "deposit"}, {"text": "💸 Retirer", "callback_data": "withdraw"}],
            [{"text": btn_pause, "callback_data": "toggle_pause"}, {"text": "📜 Trades", "callback_data": "history_page_0"}],
            [{"text": "🧾 Transactions", "callback_data": "tx_history_page_0"}, {"text": "🔑 Mes Wallets", "callback_data": "manage_wallets"}],
            [{"text": "🛠️ Debug", "callback_data": "debug"}]
        ]
    }

# --- GESTION DES WALLETS ---
def get_wallets(chat_id):
    wallets = {}
    if not r: return wallets
    try:
        for slot in [1, 2]:
            data = r.get(f"wallet:{chat_id}:{slot}")
            if data: wallets[slot] = json.loads(data)
    except Exception: pass
    return wallets

def generate_wallet(chat_id, slot, chain="BSC"):
    if not r: return "❌ Erreur : Redis non connecté."
    if not Account: return "❌ Erreur : librairie `eth-account` manquante."
    try:
        acct = Account.create()
        r.set(f"wallet:{chat_id}:{slot}", json.dumps({"chain": chain, "address": acct.address, "private_key": acct.key.hex()}))
        return f"✅ <b>Wallet {chain} généré (Emplacement {slot}) !</b>\n📍 <code>{acct.address}</code>"
    except Exception as e: return f"❌ Erreur : {str(e)}"

def delete_wallet(chat_id, slot):
    if not r: return "❌ Erreur : Redis non connecté."
    try:
        r.delete(f"wallet:{chat_id}:{slot}")
        return f"🗑️ Le Wallet {slot} a été supprimé."
    except Exception as e: return f"❌ Erreur : {str(e)}"

def show_wallet_manager(chat_id):
    wallets = get_wallets(chat_id)
    msg = "🔑 <b>Gestionnaire de Wallets (EVM)</b>\n\n"
    buttons = []
    for slot in [1, 2]:
        if slot in wallets:
            w = wallets[slot]
            msg += f"<b>Emplacement {slot} ({w.get('chain', 'BSC')}):</b>\n<code>{w['address']}</code>\n"
            buttons.append([{"text": f"🔑 Clé Privée {slot}", "callback_data": f"show_pk_{slot}"}, {"text": f"🗑️ Supprimer", "callback_data": f"delete_ask_{slot}"}])
        else:
            msg += f"<b>Emplacement {slot} :</b> (Vide)\n"
            buttons.append([{"text": f"➕ Créer BSC {slot}", "callback_data": f"create_BSC_{slot}"}, {"text": f"➕ Créer ETH {slot}", "callback_data": f"create_ETH_{slot}"}])
        msg += "\n"
    buttons.append([{"text": "⬅️ Retour", "callback_data": "back_menu"}])
    return msg, {"inline_keyboard": buttons}

# --- MARCHÉ ---
def get_market_prices():
    msg = "🌐 <b>PRIX DU MARCHÉ</b> 🌐\n\n"
    url = "https://pro-api.coinmarketcap.com/public-api/v1/cryptocurrency/listings/latest?limit=20&convert=USD"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            for coin in data.get("data", []):
                sym = coin.get("symbol")
                if sym in ["BNB", "ETH"]:
                    price = coin.get("quote", {}).get("USD", {}).get("price")
                    if price is not None:
                        msg += f"💎 <b>{sym}</b> : {price:,.2f} $\n"
            return msg
    except Exception:
        return "❌ Impossible de récupérer les prix."

# --- FONCTIONS D'HISTORIQUE PAGINÉ ---
def build_trade_history_msg(state, page):
    hist = state.get("trade_history", [])
    if not hist: return "📜 Aucun trade enregistré.", get_main_keyboard(state)
    
    total_pages = (len(hist) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    if page < 0: page = 0
    if page >= total_pages: page = total_pages - 1
    
    reversed_hist = list(reversed(hist))
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = reversed_hist[start:end]

    msg = f"📜 <b>HISTORIQUE DES TRADES</b> (Page {page+1}/{total_pages})\n\n"
    for t in page_items:
        emoji = "🟢" if t['pnl'] > 0 else "🔴"
        msg += f"{emoji} {t['date']}\nAchat: {t['entry']:.2f}$ | Vente: {t['exit']:.2f}$\nQuantité: {t.get('qty', 0):.4f} BNB\nPnL: {t['pnl']:+.2f} USDT\n\n"

    buttons = []
    nav_row = []
    if page > 0: nav_row.append({"text": "⬅️ Précédent", "callback_data": f"history_page_{page-1}"})
    if page < total_pages - 1: nav_row.append({"text": "Suivant ➡️", "callback_data": f"history_page_{page+1}"})
    if nav_row: buttons.append(nav_row)
    buttons.append([{"text": "⬅️ Menu", "callback_data": "back_menu"}])

    return msg, {"inline_keyboard": buttons}

def build_tx_history_msg(state, page):
    hist = state.get("tx_history", [])
    if not hist: return "🧾 Aucune transaction (dépôt/retrait) enregistrée.", get_main_keyboard(state)
    
    total_pages = (len(hist) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    if page < 0: page = 0
    if page >= total_pages: page = total_pages - 1
    
    reversed_hist = list(reversed(hist))
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = reversed_hist[start:end]

    msg = f"🧾 <b>HISTORIQUE DES TRANSACTIONS</b> (Page {page+1}/{total_pages})\n\n"
    for t in page_items:
        emoji = "🟢" if t['type'] == "DÉPÔT" else "🔴"
        msg += f"{emoji} <b>{t['type']}</b> - {t['date']}\nMontant : {t['amount']:.2f} USDT\nRéseau : {t.get('chain', 'BSC')}\n"
        if t.get('address'):
            msg += f"Adresse : <code>{t['address']}</code>\n"
        msg += "\n"

    buttons = []
    nav_row = []
    if page > 0: nav_row.append({"text": "⬅️ Précédent", "callback_data": f"tx_history_page_{page-1}"})
    if page < total_pages - 1: nav_row.append({"text": "Suivant ➡️", "callback_data": f"tx_history_page_{page+1}"})
    if nav_row: buttons.append(nav_row)
    buttons.append([{"text": "⬅️ Menu", "callback_data": "back_menu"}])

    return msg, {"inline_keyboard": buttons}

# --- COMMANDES TELEGRAM ---
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
                                            send_telegram_message("❌ Adresse invalide. Une adresse EVM doit commencer par '0x' et faire 42 caractères.", get_main_keyboard(state))
                                            continue

                                        state["capital_usd"] -= amount
                                        
                                        tx_record = {
                                            "date": datetime.now().strftime("%d/%m %H:%M"),
                                            "type": "RETRAIT",
                                            "amount": amount,
                                            "chain": chain,
                                            "address": address
                                        }
                                        if "tx_history" not in state: state["tx_history"] = []
                                        state["tx_history"].append(tx_record)

                                        save_state(state)
                                        send_telegram_message(f"✅ <b>Retrait initié !</b>\nEnvoi de <b>{amount:.2f} USDT</b> sur le réseau <b>{chain}</b> vers :\n<code>{address}</code>\n\n<i>(Simulation : Aucun fond réel n'a été envoyé).</i>\n\n" + build_stats_message(state), get_main_keyboard(state))
                                        del pending_actions[chat_id]
                                        continue

                                    amount = float(text.replace(",", "."))
                                    if amount <= 0: raise ValueError
                                    
                                    if action == "deposit":
                                        pending_actions[chat_id] = {"action": "deposit", "amount": amount, "chain": action_data.get("chain", "BSC")}
                                        confirm_kb = {"inline_keyboard": [[{"text": "✅ Confirmer", "callback_data": "confirm_deposit"}], [{"text": "❌ Annuler", "callback_data": "cancel_action"}]]}
                                        send_telegram_message(f"💰 Tu souhaites déposer <b>{amount:.2f} USDT</b> sur le réseau <b>{action_data.get('chain', 'BSC')}</b>.\nConfirmes-tu ?", confirm_kb)
                                    
                                    elif action == "withdraw_amount":
                                        if amount > state["capital_usd"]:
                                            send_telegram_message(f"❌ Fonds insuffisants. Capital : <b>{state['capital_usd']:.2f} USDT</b>.", get_main_keyboard(state))
                                            del pending_actions[chat_id]
                                        else:
                                            pending_actions[chat_id] = {"action": "withdraw_address", "amount": amount, "chain": action_data.get("chain", "BSC")}
                                            send_telegram_message(f"💸 Tu souhaites retirer <b>{amount:.2f} USDT</b> sur le réseau <b>{action_data.get('chain', 'BSC')}</b>.\n\nEnvoie maintenant <b>l'adresse EVM de destination</b> (commençant par 0x...) :", None)

                                except ValueError:
                                    send_telegram_message("❌ Montant invalide. Envoie un nombre (ex: 50).", get_main_keyboard(state))
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
                                    status = "⏸️ EN PAUSE" if state["is_paused"] else "▶️ EN MARCHE"
                                    send_telegram_message(f"Statut mis à jour : <b>{status}</b>", get_main_keyboard(state))
                                
                                elif data_cb.startswith("history_page_"):
                                    page = int(data_cb.split("_")[2])
                                    msg, kb = build_trade_history_msg(state, page)
                                    send_telegram_message(msg, kb)
                                
                                elif data_cb.startswith("tx_history_page_"):
                                    page = int(data_cb.split("_")[3])
                                    msg, kb = build_tx_history_msg(state, page)
                                    send_telegram_message(msg, kb)

                                elif data_cb == "debug":
                                    msg = "🛠️ <b>MODE DEBUG</b> 🛠️\n\n"
                                    msg += f"💰 Capital : {state['capital_usd']:.2f} USDT\n"
                                    msg += f"Pause : {state.get('is_paused', False)}\n"
                                    msg += f"Trades total : {state.get('total_trades', 0)}\n"
                                    msg += f"Historique : {len(price_history)} points\n\n"
                                    price = get_real_price(config.get("pair_address", "0x16b9a82891338f9bA80E2D6970FddA79D1eb0daE"))
                                    if not price: msg += "❌ Impossible de récupérer le prix."
                                    else:
                                        msg += f"💹 Prix Actuel du BNB : {price:.3f} $\n"
                                        if len(price_history) < config["ema_slow"]:
                                            msg += f"⏳ Attente d'historique : {len(price_history)}/{config['ema_slow']} points."
                                        else:
                                            rsi = calculate_rsi(price_history, config["rsi_period"])
                                            msg += f"🌡️ RSI : {rsi:.1f}\n\n"
                                            
                                            if not state["position_open"]:
                                                msg += f"🎯 Stratégie : Acheter si RSI < {RSI_BUY_THRESHOLD}\n"
                                                if rsi < RSI_BUY_THRESHOLD:
                                                    msg += "✅ Le prix est bas (RSI bas). Le bot va ACHETER au prochain cycle !"
                                                else:
                                                    msg += f"⏳ Le prix est encore trop haut pour acheter. (RSI actuel : {rsi:.1f})"
                                            else:
                                                pnl_percent = ((price - state["entry_price"]) / state["entry_price"]) * 100
                                                msg += f"🎯 Stratégie : Vendre si RSI > {RSI_SELL_THRESHOLD} ou PnL cible.\n"
                                                msg += f"📈 PnL Actuel : {pnl_percent:+.2f}%\n"
                                                if rsi > RSI_SELL_THRESHOLD:
                                                    msg += "✅ Le prix est haut (RSI haut). Le bot va VENDRE au prochain cycle !"
                                                else:
                                                    msg += f"⏳ Le prix n'est pas encore assez haut pour vendre."
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
                                        pk = wallets[slot]["private_key"]
                                        chain = wallets[slot].get("chain", "BSC")
                                        msg = f"⚠️ <b>ATTENTION</b> ⚠️\nVoici la clé privée de ton Wallet {chain} (Emplacement {slot}).\n\n<code>{pk}</code>\n\n<i>Ne partage JAMAIS cette clé.</i>"
                                        send_telegram_message(msg, get_main_keyboard(state))
                                    else: send_telegram_message("❌ Wallet introuvable.", get_main_keyboard(state))
                                elif data_cb.startswith("create_"):
                                    parts = data_cb.split("_")
                                    chain = parts[1]
                                    slot = int(parts[2])
                                    send_telegram_message(generate_wallet(chat_id, slot, chain))
                                    msg, kb = show_wallet_manager(chat_id)
                                    send_telegram_message(msg, kb)
                                elif data_cb.startswith("delete_ask_"):
                                    slot = int(data_cb.split("_")[2])
                                    confirm_kb = {"inline_keyboard": [[{"text": "✅ Oui", "callback_data": f"delete_confirm_{slot}"}], [{"text": "❌ Non", "callback_data": "manage_wallets"}]]}
                                    send_telegram_message(f"⚠️ Supprimer le Wallet {slot} ?", confirm_kb)
                                elif data_cb.startswith("delete_confirm_"):
                                    slot = int(data_cb.split("_")[2])
                                    send_telegram_message(delete_wallet(chat_id, slot))
                                    msg, kb = show_wallet_manager(chat_id)
                                    send_telegram_message(msg, kb)
                                
                                elif data_cb == "deposit":
                                    net_kb = {"inline_keyboard": [
                                        [{"text": "🟡 BSC (BNB)", "callback_data": "dep_net_BSC"}],
                                        [{"text": "🔵 Ethereum (ETH)", "callback_data": "dep_net_ETH"}],
                                        [{"text": "❌ Annuler", "callback_data": "cancel_action"}]
                                    ]}
                                    send_telegram_message("💰 <b>Dépôt</b>\nSur quel réseau EVM souhaites-tu déposer des USDT ?", net_kb)
                                elif data_cb.startswith("dep_net_"):
                                    chain = data_cb.split("_")[2]
                                    pending_actions[chat_id] = {"action": "deposit", "amount": 0, "chain": chain}
                                    send_telegram_message(f"💰 <b>Dépôt sur {chain}</b>\nQuel montant en USDT souhaites-tu déposer ? (ex: 50)", None)

                                elif data_cb == "withdraw":
                                    net_kb = {"inline_keyboard": [
                                        [{"text": "🟡 BSC (BNB)", "callback_data": "wd_net_BSC"}],
                                        [{"text": "🔵 Ethereum (ETH)", "callback_data": "wd_net_ETH"}],
                                        [{"text": "❌ Annuler", "callback_data": "cancel_action"}]
                                    ]}
                                    send_telegram_message("💸 <b>Retrait</b>\nSur quel réseau EVM souhaites-tu retirer tes USDT ?", net_kb)
                                elif data_cb.startswith("wd_net_"):
                                    chain = data_cb.split("_")[2]
                                    pending_actions[chat_id] = {"action": "withdraw_amount", "amount": 0, "chain": chain}
                                    send_telegram_message(f"💸 <b>Retrait sur {chain}</b>\nQuel montant en USDT souhaites-tu retirer ? (ex: 20)", None)

                                elif data_cb == "confirm_deposit":
                                    if chat_id in pending_actions and pending_actions[chat_id].get("amount", 0) > 0:
                                        amount = pending_actions[chat_id]["amount"]
                                        chain = pending_actions[chat_id].get("chain", "BSC")
                                        
                                        send_telegram_message(f"🔍 <i>Connexion à la blockchain {chain}...</i>\nRecherche de la transaction...", None)
                                        time.sleep(2)
                                        
                                        state["capital_usd"] += amount
                                        state["total_deposited"] = state.get("total_deposited", 0) + amount
                                        state["peak_capital"] = max(state.get("peak_capital", 0), state["capital_usd"])
                                        
                                        tx_record = {
                                            "date": datetime.now().strftime("%d/%m %H:%M"),
                                            "type": "DÉPÔT",
                                            "amount": amount,
                                            "chain": chain
                                        }
                                        if "tx_history" not in state: state["tx_history"] =):
 []
                                        state["tx_history"].append(tx_record)
                                        
                                        save_state(state)
                                        send_telegram_message(f"✅ <b>Transaction confirmée sur {chain} !</b>\nDépôt de <b   >{amount:.2f} USDT</b> effectué.\n\n" + build_stats_message(state), get_main_keyboard(state))
                                    url else:
                                        send_telegram_message("❌ Aucune opération en attente.", get_main_keyboard(state))
                                    pending_actions.pop(chat_id, None)
                                
                                elif data_cb == "cancel_action":
                                    pending_actions.pop(chat_id, None)
                                    send_telegram_message("❌ Annulé.", get_main_keyboard(state))
                            except Exception as e:
                                send_telegram_message(f"❌ Erreur Callback : {e}", get_main_keyboard(state))
                            finally:
                                try: urllib.request.urlopen(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery?callback_query_id={callback['id']}")
                                except: pass
        except Exception as e: print(f"Erreur boucle Telegram : {e}")
        time.sleep(2)

def build_stats_message(state):
    pos = "OUI" if state["position_open"] else "NON"
    win_rate = 0
    if state.get("total_trades", 0) > 0:
        win_rate = (state.get("winning_trades", 0) / state["total_trades"]) * 100
    db_status = "🟢 Connecté" if r else "🔴 Local"
    pause_status = "⏸️ EN PAUSE" if state.get("is_paused", False) else "🟢 ACTIF"
    return (f"📊 <b>TABLEAU DE BORD</b> 📊\n"
            f"Statut : {pause_status}\n"
            f"🏦 Total Déposé : <b>{state.get('total_deposited', 0.0):.2f} USDT</b>\n"
            f"💰 Capital Actuel : <b>{state['capital_usd']:.2f} USDT</b>\n"
            f"🏔️ Plus Haut : {state.get('peak_capital', 0.0):.2f} USDT\n"
            f"🗄️ Base de données : {db_status}\n"
            f"──────────────────\n"
            f"📈 Position Ouverte : {pos}\n"
            f"🛑 SL : -{STOP_LOSS_PERCENT}% | 📉 Trailing : +{TRAILING_STOP_ACTIVATION_PCT}%\n"
            f"──────────────────\n"
            f"📊 Trades : {state.get('total_trades', 0)} (Réussite: {win_rate:.1f}%)\n"
            f"💵 PnL Réalisé : {state['realized_pnl_usd']:+.4f} USDT\n"
            f"💸 Frais payés : {state['total_fees_usd']:.4f} USDT")

def load_config(path="config.json"):
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except Exception:
        return {"initial_capital_usd": 0.0, "position_size_percent": 60.0, "ema_fast": 5, "ema_slow": 15, "rsi_period": 10, "estimated_gas_cost_usd": 0.15, "estimated_trading_fee_percent": 0.25, "estimated_slippage_percent": 0.5, "pair_address": "0x16b9a82891338f9bA80E2D6970FddA79D1eb0daE"}

def load_state(config):
    if r:
        try:
            data = r.get("cryptopilot_state")
            if data: return json.loads(data)
        except Exception: pass
    return {"capital_usd": config.get("initial_capital_usd", 0.0), "total_deposited": 0.0, "peak_capital": 0.0, "position_open": False, "position_amount_usd": 0.0, "position_quantity": 0.0, "entry_price": 0.0, "highest_price": 0.0, "total_fees_usd": 0.0, "realized_pnl_usd": 0.0, "total_trades": 0, "winning_trades": 0, "is_paused": False, "trade_history": [], "tx_history": []}

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

def get_real_price(pair_address = f"https://api.dexscreener.com/latest/dex/pairs/bsc/{pair_address}"
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
    global last_error_time
    if state["capital_usd"] < 1.0: return state
    try:
        security_check = check_token_security("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c") # WBNB
        if not security_check["safe"]: 
            if time.time() - last_error_time > 3600:
                send_telegram_message(f"❌ Achat bloqué par le bouclier : {security_check['reason']}")
                last_error_time = time.time()
            return state
    except Exception: return state

    amount_usdt = state["capital_usd"] * (config["position_size_percent"] / 100.0)
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
    
    send_telegram_message(f"🚀 <b>ACHAT</b>\nAcheté : <b>{quantity:.4f} BNB</b>\nAvec : {amount_usdt:.2f} USDT\nPrix : {price:.3f} $")
    return state

def simulate_sell(state, price, config, reason="Signal"):
    if not state["position_open"]: return state

    quantity = state["position_quantity"]
    amount_usdt = state["position_amount_usd"]
    
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
    
    trade_record = {
        "date": datetime.now().strftime("%d/%m %H:%M"),
        "entry": state["entry_price"] if state["entry_price"] else 0,
        "exit": price,
        "qty": quantity,
        "pnl": pnl
    }
    if "trade_history" not in state: state["trade_history"] = []
    state["trade_history"].append(trade_record)

    emoji = "💰" if pnl > 0 else "🛑"
    send_telegram_message(f"{emoji} <b>VENTE ({reason})</b>\nVendu : <b>{quantity:.4f} BNB</b>\nReçu : {net_proceeds:.2f} USDT\nPrix : {price:.3f} $\nPnL Net : {pnl:+.4f} USDT")
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
    
    send_telegram_message("🤖 <b>Bot CryptoPilot en ligne</b>\nStratégie : Acheter la baisse, Vendre la hausse.\nUtilise /menu pour commencer.")

    while True:
        try:
            if not state.get("is_paused", False) and state["capital_usd"] > 1.0:
                current_price = get_real_price(config.get("pair_address", "0x16b9a82891338f9bA80E2D6970FddA79D1eb0daE"))
                if current_price:
                    price_history.append(current_price)
                    if len(price_history) > 100: price_history.pop(0)
                    save_history(price_history)

                    if len(price_history) >= config["ema_slow"]:
                        rsi = calculate_rsi(price_history, config["rsi_period"])

                        if not state["position_open"]:
                            # --- ACHAT SI LE PRIX EST BAS (RSI BAS) ---
                            if rsi < RSI_BUY_THRESHOLD:
                                state = simulate_buy(state, current_price, config)
                        else:
                            pnl_percent = ((current_price - state["entry_price"]) / state["entry_price"]) * 100
                            
                            if current_price > state.get("highest_price", state["entry_price"]):
                                state["highest_price"] = current_price
                                
                            drop_from_peak = ((state["highest_price"] - current_price) / state["highest_price"]) * 100

                            # --- VENTE SI LE PRIX EST HAUT (RSI HAUT) OU SECURITES ---
                            if rsi > RSI_SELL_THRESHOLD:
                                state = simulate_sell(state, current_price, config, reason=f"RSI SURCHAUFFE ({rsi:.1f})")
                            elif pnl_percent <= -STOP_LOSS_PERCENT:
                                state = simulate_sell(state, current_price, config, reason=f"STOP-LOSS (-{STOP_LOSS_PERCENT}%)")
                            elif pnl_percent >= TAKE_PROFIT_PERCENT:
                                state = simulate_sell(state, current_price, config, reason=f"TAKE-PROFIT (+{TAKE_PROFIT_PERCENT}%)")
                            elif pnl_percent >= TRAILING_STOP_ACTIVATION_PCT and drop_from_peak >= TRAILING_STOP_PERCENT:
                                state = simulate_sell(state, current_price, config, reason=f"TRAILING STOP (Chute de {drop_from_peak:.1f}%)")

                    if state["capital_usd"] > state.get("peak_capital", 0):
                        state["peak_capital"] = state["capital_usd"]
                    
                    if state.get("peak_capital", 0) > 0:
                        drawdown = ((state["peak_capital"] - state["capital_usd"]) / state["peak_capital"]) * 100
                        if drawdown >= MAX_DRAWDOWN_PERCENT and not state.get("is_paused", False):
                            state["is_paused"] = True
                            send_telegram_message(f"🚨 <b>PROTECTION DE PORTEFEUILLE ACTIVÉE !</b> 🚨\nTon capital a baissé de {drawdown:.1f}% par rapport à son plus haut ({state['peak_capital']:.2f} USDT).\nLe bot a été mis en pause automatiquement pour sécuriser les <b>{state['capital_usd']:.2f} USDT</b> restants.")

                    save_state(state)
        except Exception as e: print(f"Erreur : {e}")
        time.sleep(10)

if __name__ == "__main__":
    main()
