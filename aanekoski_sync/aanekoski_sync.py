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


class FormParser(HTMLParser):
    """Parsii Keycloak-kirjautumislomakkeen action-URL:n ja piilotetut kentät."""
    def __init__(self):
        super().__init__()
        self.action_url = None
        self.hidden_fields = {}
        self._in_form = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "form" and attrs_dict.get("id") == "kc-form-login":
            self._in_form = True
            self.action_url = attrs_dict.get("action")
        if self._in_form and tag == "input" and attrs_dict.get("type") == "hidden":
            name = attrs_dict.get("name")
            value = attrs_dict.get("value", "")
            if name:
                self.hidden_fields[name] = value


def fetch_api_data():
    """Kirjautuu requests-kirjastolla ja hakee kulutusdata API:sta."""
    print("Kirjaudutaan Äänekosken Energian portaaliin...")
    
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    })
    
    # 1. Hae kirjautumissivu (seuraa redirectit Keycloakiin)
    resp = session.get(LOGIN_START_URL, timeout=30)
    if resp.status_code != 200:
        print(f"Virhe kirjautumissivun haussa: {resp.status_code}")
        return None
    
    # 2. Parsitaan lomakkeen action URL ja piilotetut kentät
    parser = FormParser()
    parser.feed(resp.text)
    
    if not parser.action_url:
        print("Kirjautumislomaketta ei löytynyt sivulta!")
        print(f"Sivun URL: {resp.url}")
        print(f"Sivun alku: {resp.text[:500]}")
        return None
    
    print(f"Keycloak action URL löytyi: {parser.action_url[:80]}...")
    
    # 3. Lähetä tunnukset
    login_data = {
        "username": EMAIL,
        "password": PASSWORD,
        **parser.hidden_fields
    }
    
    resp2 = session.post(parser.action_url, data=login_data, timeout=30)
    
    # Tarkista kirjautuminen
    if "login" in resp2.url.lower() and "error" in resp2.text.lower():
        print("VIRHE: Kirjautuminen epäonnistui! Tarkista sähköposti ja salasana.")
        return None
    
    print(f"Kirjautuminen onnistui! Uudelleenohjattu: {resp2.url[:80]}...")
    
    # 4. Hae kulutusdata API:sta
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
    resp3 = session.get(api_url, timeout=30)
    
    if resp3.status_code != 200:
        print(f"Virhe datan haussa: {resp3.status_code} {resp3.text[:200]}")
        return None
    
    data = resp3.json()
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
