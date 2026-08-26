import asyncio
import os
import json
import uuid
import datetime
import time
from playwright.async_api import async_playwright
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

# Supervisorin kautta yhdistäminen suoraan Home Assistantiin
HA_URL = "ws://supervisor/core/api/websocket"
HA_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

async def fetch_api_data():
    print("Käynnistetään selain ja kirjaudutaan...")
    async with async_playwright() as p:
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        if executable:
            browser = await p.chromium.launch(headless=True, executable_path=executable)
        else:
            browser = await p.chromium.launch(headless=True)
            
        context = await browser.new_context()
        page = await context.new_page()
        
        await page.goto("https://aanekoskenenergia.ecoreaction.fi/online/tracking")
        await page.wait_for_timeout(2000)
        
        # Täytä tiedot
        await page.locator("input[name='username']").fill(EMAIL)
        await page.locator("input[name='password']").fill(PASSWORD)
        await page.locator("input[name='password']").press("Enter")
        
        print("Odotetaan kirjautumista...")
        await page.wait_for_url("**/online/tracking*")
        await page.wait_for_timeout(2000)
        
        # Laske päivämäärät
        end = datetime.datetime.now()
        start = end - datetime.timedelta(days=DAYS_TO_FETCH)
        start_str = start.strftime("%Y-%m-%dT00:00:00Z")
        end_str = end.strftime("%Y-%m-%dT23:59:59Z")
        
        # Sähkönkäyttöpaikan ID
        elec_mp = "KAYTTOPAIKKATUNNUS"
        url = f"https://aanekoskenenergia.ecoreaction.fi/api/meteringPoints/ELECTRICITY/{elec_mp}/series?products=EL_ENERGY_CONSUMPTION&resolution=DAYS_AS_HOURS&startDate={start_str}&endDate={end_str}"
        
        print(f"Haetaan dataa aikaväliltä {start_str} - {end_str}...")
        resp = await context.request.get(url)
        data = None
        if resp.ok:
            data = await resp.json()
            print(f"Data haettu onnistuneesti! Data-pisteiden määrä: {len(data)}")
        else:
            print(f"Virhe datan haussa: {resp.status} {resp.status_text}")
        
        await browser.close()
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
        print("VIRHE: SUPERVISOR_TOKEN puuttuu. Varmista, että ajat skriptiä Add-on ympäristössä.")
        return

    async with websockets.connect(HA_URL) as ws:
        # 1. Auth
        auth_req = await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        auth_resp = await ws.recv()
        print("HA Auth:", auth_resp)
        
        # 2. Import statistics
        msg_id = 1
        payload = {
            "id": msg_id,
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
        await ws.send(json.dumps(payload))
        resp = await ws.recv()
        print("HA Import Response:", resp)

async def main():
    print("Äänekosken Energia -lisäosa käynnistetty.")
    if not EMAIL or not PASSWORD:
        print("VIRHE: Sähköpostia tai salasanaa ei ole asetettu asetuksissa!")
        return

    while True:
        now = datetime.datetime.now()
        # Vertaillaan aikaa
        if str(now.hour).zfill(2) == str(RUN_HOUR).zfill(2) and str(now.minute).zfill(2) == str(RUN_MINUTE).zfill(2):
            print(f"Kello on {RUN_HOUR}:{RUN_MINUTE}, aloitetaan synkronointi!")
            try:
                data = await fetch_api_data()
                await push_to_ha(data)
                print("Synkronointi valmis! Odotetaan seuraavaa päivää...")
                # Nukutaan yli minuutin, ettei ajeta heti uudelleen samalla minuutilla
                await asyncio.sleep(120)
            except Exception as e:
                print(f"Kriittinen virhe synkronoinnissa: {e}")
                await asyncio.sleep(60) # Yritetään ehkä minuutin päästä uudelleen tai odotetaan huomiseen
        else:
            # Nukutaan 30 sekuntia ja tarkistetaan aika uudelleen
            await asyncio.sleep(30)

async def startup():
    """Ajetaan ensin kerran heti ja sitten siirrytään ajastettuun tilaan."""
    print("Ajetaan ensimmäinen synkronointi käynnistyksen yhteydessä...")
    try:
        data = await fetch_api_data()
        await push_to_ha(data)
    except Exception as e:
        print(f"Virhe ensimmäisessä ajossa: {e}")
        import traceback
        traceback.print_exc()
    
    print(f"Siirrytään ajastettuun tilaan. Seuraava ajo päivittäin klo {RUN_HOUR}:{RUN_MINUTE}.")
    await main()

if __name__ == "__main__":
    asyncio.run(startup())
