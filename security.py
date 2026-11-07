import json
import urllib.request

def check_token_security(token_address):
    url = f"https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={token_address}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            token_info = data.get("result", {}).get(token_address.lower(), {})
            if str(token_info.get("is_honeypot", "0")) == "1":
                return {"safe": False}
            return {"safe": True}
    except Exception:
        return {"safe": False}
