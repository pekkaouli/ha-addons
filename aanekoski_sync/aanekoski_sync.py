import asyncio
import os
import json
import datetime
import time
import requests
from html.parser import HTMLParser
import websockets

# Lue asetukset lisäosan konfiguraatiosta
OPTIONS_PATH = "/data/options.json"
try:
    with open(OPTIONS_PATH) as f:
        options = json.load(f)
except FileNotFoundError:
    print("Varoitus: /data/options.json ei löydy, käytetään oletuksia.")
    options = {}

EMAIL = options.get("email", "")
PASSWORD = options.get("password", "")
DAYS_TO_FETCH = int(options.get("days_to_fetch", 7))
RUN_HOUR = options.get("run_hour", "12")
RUN_MINUTE = options.get("run_minute", "00")

# Home Assistantin sisäinen osoite add-on ympäristössä
HA_WS_URL = "ws://homeassistant:8123/api/websocket"
HA_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Keycloak login asetukset
LOGIN_START_URL = "https://aanekoskenenergia.ecoreaction.fi/online/tracking"
ELEC_MP = "KAYTTOPAIKKATUNNUS"


import base64
import hashlib
import secrets
import urllib.parse

class FormParser(HTMLParser):
    """Parsii Keycloak-kirjautumislomakkeen action-URL:n."""
    def __init__(self):
        super().__init__()
        self.action_url = None
        self._in_form = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "form" and attrs_dict.get("id") == "kc-form-login":
            self._in_form = True
            self.action_url = attrs_dict.get("action")

def generate_pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
    return verifier, challenge

def fetch_api_data():
    """Kirjautuu OIDC PKCE -vuolla ja hakee kulutusdata API:sta."""
    print("Kirjaudutaan Äänekosken Energian portaaliin OIDC PKCE -kirjautumisella...")
    
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    
    redirect_uri = "https://aanekoskenenergia.ecoreaction.fi/online/tracking"
    client_id = "ecoreaction-ui-online"
    
    auth_url = (
        f"https://login.ecoreaction.fi/auth/realms/AanekoskenEnergia/protocol/openid-connect/auth"
        f"?client_id={client_id}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&response_type=code"
        f"&scope=openid"
        f"&state={state}"
        f"&nonce={nonce}"
        f"&code_challenge={challenge}"
        f"&code_challenge_method=S256"
        f"&response_mode=fragment"
    )
    
    # 1. Hae kirjautumissivu (Keycloak)
    resp = session.get(auth_url, timeout=30)
    if resp.status_code != 200:
        print(f"Virhe kirjautumissivun haussa: {resp.status_code}")
        return None
    
    # 2. Parsitaan lomakkeen action URL
    parser = FormParser()
    parser.feed(resp.text)
    
    if not parser.action_url:
        print("Kirjautumislomaketta ei löytynyt Keycloak-sivulta!")
        return None
        
    # 3. Lähetä tunnukset
    login_data = {
        "username": EMAIL,
        "password": PASSWORD,
        "credentialId": ""
    }
    
    resp2 = session.post(parser.action_url, data=login_data, allow_redirects=False, timeout=30)
    
    # 4. Tarkista uudelleenohjaus (joka sisältää koodin)
    location = resp2.headers.get("Location")
    if not location:
        print("VIRHE: Kirjautuminen epäonnistui tai ei uudelleenohjausta!")
        if "error" in resp2.text.lower():
            print("Väärä sähköposti tai salasana?")
        return None
    
    # 5. Pura koodi URL:sta
    parsed = urllib.parse.urlparse(location)
    fragment_qs = urllib.parse.parse_qs(parsed.fragment)
    query_qs = urllib.parse.parse_qs(parsed.query)
    
    code = fragment_qs.get("code", query_qs.get("code", [None]))[0]
    if not code:
        print("VIRHE: OIDC koodia ei löytynyt redirect-URL:sta!")
        return None
        
    # 6. Vaihda koodi access tokeniin
    token_url = "https://login.ecoreaction.fi/auth/realms/AanekoskenEnergia/protocol/openid-connect/token"
    token_data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier
    }
    
    resp3 = session.post(token_url, data=token_data, timeout=30)
    if resp3.status_code != 200:
        print(f"VIRHE Tokenin haussa: {resp3.status_code} {resp3.text}")
        return None
        
    tokens = resp3.json()
    access_token = tokens.get("access_token")
    if not access_token:
        print("VIRHE: Access tokenia ei saatu.")
        return None
    
    print("Kirjautuminen onnistui! Access token haettu.")
    
    # 7. Hae kulutusdata API:sta
    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=DAYS_TO_FETCH)
    start_str = start.strftime("%Y-%m-%dT00:00:00Z")
    end_str = end.strftime("%Y-%m-%dT23:59:59Z")
    
    api_url = (
        f"https://aanekoskenenergia.ecoreaction.fi/api/meteringPoints/ELECTRICITY/"
        f"{ELEC_MP}/series?products=EL_ENERGY_CONSUMPTION"
        f"&resolution=DAYS_AS_HOURS&startDate={start_str}&endDate={end_str}"
    )
    
    print(f"Haetaan dataa aikaväliltä {start_str} - {end_str}...")
    resp4 = session.get(api_url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    
    if resp4.status_code != 200:
        print(f"Virhe datan haussa: {resp4.status_code} {resp4.text[:200]}")
        return None
    
    data = resp4.json()
    print(f"Data haettu onnistuneesti! Data-pisteiden määrä: {len(data)}")
    return data


async def push_to_ha(data):
    if not data:
        print("Ei dataa lähetettäväksi.")
        return

    print("Yhdistetään Home Assistantiin...")
    stats = []
    sum_kwh = 0.0
    
    # Varmistetaan aikajärjestys
    data.sort(key=lambda x: x.get("timestamp", ""))
    
    for item in data:
        values = item.get("values", {})
        el_cons = values.get("EL_ENERGY_CONSUMPTION#0", {})
        val = el_cons.get("value")
        timestamp_str = item.get("timestamp")
        
        if val is not None and timestamp_str:
            sum_kwh += float(val)
            stats.append({
                "start": timestamp_str,
                "state": float(val),
                "sum": sum_kwh
            })

    if not stats:
        print("Datan parsiminen ei tuottanut tuloksia.")
        return

    print(f"Lähetetään {len(stats)} datapistettä Home Assistantiin...")
    
    if not HA_TOKEN:
        print("VIRHE: SUPERVISOR_TOKEN puuttuu!")
        return

    ws_payload = {
        "id": 1,
        "type": "recorder/import_statistics",
        "metadata": {
            "has_mean": False,
            "has_sum": True,
            "name": "Äänekosken Energia Kulutus",
            "source": "recorder",
            "statistic_id": "sensor.aanekosken_energia_kulutus",
            "unit_of_measurement": "kWh"
        },
        "stats": stats
    }

    try:
        async with websockets.connect(HA_WS_URL) as ws:
            # 1. Auth
            auth_req = await ws.recv()
            print(f"WS auth: {auth_req}")
            await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
            auth_resp = await ws.recv()
            auth_data = json.loads(auth_resp)
            print(f"WS auth vastaus: {auth_resp}")
            
            if auth_data.get("type") != "auth_ok":
                print(f"VIRHE: Tunnistautuminen epäonnistui!")
                return
            
            # 2. Import statistics
            await ws.send(json.dumps(ws_payload))
            
            try:
                resp = await asyncio.wait_for(ws.recv(), timeout=30)
                print(f"HA Import Response: {resp}")
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as e:
                print(f"Yhteys katkaistiin/aikakatkaisu vastauksen aikana: {e}")
                print("Data on todennäköisesti tuotu onnistuneesti.")
    except Exception as e:
        print(f"WebSocket-yhteys epäonnistui: {e}")
        import traceback
        traceback.print_exc()


async def main():
    print("Äänekosken Energia -lisäosa käynnistetty.")
    if not EMAIL or not PASSWORD:
        print("VIRHE: Sähköpostia tai salasanaa ei ole asetettu asetuksissa!")
        return

    while True:
        now = datetime.datetime.now()
        if str(now.hour).zfill(2) == str(RUN_HOUR).zfill(2) and str(now.minute).zfill(2) == str(RUN_MINUTE).zfill(2):
            print(f"Kello on {RUN_HOUR}:{RUN_MINUTE}, aloitetaan synkronointi!")
            try:
                data = fetch_api_data()
                await push_to_ha(data)
                print("Synkronointi valmis! Odotetaan seuraavaa päivää...")
                await asyncio.sleep(120)
            except Exception as e:
                print(f"Kriittinen virhe synkronoinnissa: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(60)
        else:
            await asyncio.sleep(30)


async def startup():
    """Ajetaan ensin kerran heti ja sitten siirrytään ajastettuun tilaan."""
    print("Ajetaan ensimmäinen synkronointi käynnistyksen yhteydessä...")
    try:
        data = fetch_api_data()
        await push_to_ha(data)
    except Exception as e:
        print(f"Virhe ensimmäisessä ajossa: {e}")
        import traceback
        traceback.print_exc()
    
    print(f"Siirrytään ajastettuun tilaan. Seuraava ajo päivittäin klo {RUN_HOUR}:{RUN_MINUTE}.")
    await main()


if __name__ == "__main__":
    asyncio.run(startup())
