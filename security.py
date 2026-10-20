import json
import urllib.request

def check_token_security(token_address):
    url = f"https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={token_address}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            if not data.get("result"):
                return {"safe": False, "reason": "Aucune donnée retournée par l'API."}
            
            token_info = data["result"].get(token_address.lower())
            if not token_info:
                return {"safe": False, "reason": "Token non trouvé dans la base de données."}

            if str(token_info.get("is_honeypot", "0")) == "1":
                return {"safe": False, "reason": "CRITIQUE : Honeypot (impossible de vendre)."}
            
            buy_tax = float(token_info.get("buy_tax", "0") or 0) * 100
            sell_tax = float(token_info.get("sell_tax", "0") or 0) * 100
            if buy_tax > 10 or sell_tax > 10:
                return {"safe": False, "reason": f"Taxes trop élevées (Achat: {buy_tax:.1f}%, Vente: {sell_tax:.1f}%)."}
            
            if str(token_info.get("is_open_source", "1")) == "0":
                return {"safe": False, "reason": "Code du contrat non open source."}
            
            if str(token_info.get("is_mintable", "0")) == "1":
                return {"safe": False, "reason": "Le contrat permet de créer des tokens à l'infini."}

            return {"safe": True, "reason": "Token vérifié et sécurisé."}
    except Exception as e:
        return {"safe": False, "reason": f"Erreur réseau : {e}"}