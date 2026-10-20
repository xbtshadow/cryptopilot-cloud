import json
import urllib.request

def check_token_security(token_address):
    url = f"https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={token_address}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            if not data.get("result"):
                return {"safe": False, "reason": "API GoPlus indisponible."}
            
            token_info = data["result"].get(token_address.lower())
            if not token_info:
                return {"safe": False, "reason": "Token introuvable sur GoPlus."}

            if str(token_info.get("is_honeypot", "0")) == "1":
                return {"safe": False, "reason": "Honeypot détecté."}
            
            buy_tax = float(token_info.get("buy_tax", "0") or 0) * 100
            sell_tax = float(token_info.get("sell_tax", "0") or 0) * 100
            if buy_tax > 10 or sell_tax > 10:
                return {"safe": False, "reason": f"Taxes trop élevées."}
            
            if str(token_info.get("is_open_source", "1")) == "0":
                return {"safe": False, "reason": "Contrat non open source."}
            
            return {"safe": True, "reason": "Token sûr."}
    except Exception as e:
        return {"safe": False, "reason": f"Erreur réseau GoPlus : {e}"}
