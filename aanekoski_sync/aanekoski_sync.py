import asyncio
import base64
import datetime
import hashlib
import json
import os
import secrets
import urllib.parse
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import requests
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
START_DATE_RAW = options.get("start_date", "2021-07-01")
RUN_HOUR = options.get("run_hour", "12")
RUN_MINUTE = options.get("run_minute", "00")

# Home Assistantin sisäinen osoite add-on ympäristössä
HA_WS_URL = "ws://supervisor/core/api/websocket"
HA_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Keycloak login asetukset
LOGIN_START_URL = "https://aanekoskenenergia.ecoreaction.fi/online/tracking"
ELEC_MP = "KAYTTOPAIKKATUNNUS"
PRODUCT = "EL_ENERGY_CONSUMPTION"
STATISTIC_ID = "sensor.aanekosken_energia_kulutus"

LOCAL_TZ = ZoneInfo("Europe/Helsinki")

# Kuinka monta datapistettä lähetetään yhdessä WebSocket-viestissä
IMPORT_CHUNK_SIZE = 5000


class FormParser(HTMLParser):
    """Parsii Keycloak-kirjautumislomakkeen action-URL:n."""

    def __init__(self):
        super().__init__()
        self.action_url = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "form" and attrs_dict.get("id") == "kc-form-login":
            self.action_url = attrs_dict.get("action")


def generate_pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def parse_start_date(value):
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        print(f"Virheellinen start_date '{value}', käytetään oletusta 2021-07-01.")
        return datetime.date(2021, 7, 1)


def _tzinfo_from_offset(offset):
    """API:n utcOffset voi olla tunteja, minuutteja tai '+02:00'-muotoinen merkkijono."""
    if offset is None:
        return None
    if isinstance(offset, str):
        text = offset.strip()
        if not text:
            return None
        sign = -1 if text.startswith("-") else 1
        text = text.lstrip("+-")
        hours, _, minutes = text.partition(":")
        try:
            delta = datetime.timedelta(hours=int(hours), minutes=int(minutes or 0))
        except ValueError:
            return None
        return datetime.timezone(sign * delta)
    if isinstance(offset, (int, float)):
        # Pienet arvot ovat tunteja, isot minuutteja
        delta = (
            datetime.timedelta(hours=offset)
            if abs(offset) <= 14
            else datetime.timedelta(minutes=offset)
        )
        return datetime.timezone(delta)
    return None


def parse_item_time(item):
    """
    API ei palauta timestamp-kenttää lainkaan (se on aina null).
    Aika on erillisissä kentissä year/month/day/hour.
    Rivit joilta hour puuttuu ovat päiväaggregaatteja - ne ohitetaan.
    """
    year = item.get("year")
    month = item.get("month")
    day = item.get("day")
    hour = item.get("hour")
    if None in (year, month, day, hour):
        return None
    tz = _tzinfo_from_offset(item.get("utcOffset")) or LOCAL_TZ
    try:
        return datetime.datetime(year, month, day, hour, tzinfo=tz)
    except ValueError:
        return None


def parse_item_value(item):
    values = item.get("values") or {}
    entry = values.get(f"{PRODUCT}#0")
    if not isinstance(entry, dict):
        entry = next(iter(values.values()), None)
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def login():
    """Kirjautuu OIDC PKCE -vuolla ja palauttaa (session, access_token)."""
    print("Kirjaudutaan Äänekosken Energian portaaliin OIDC PKCE -kirjautumisella...")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
    )

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
        return None, None

    # 2. Parsitaan lomakkeen action URL
    parser = FormParser()
    parser.feed(resp.text)

    if not parser.action_url:
        print("Kirjautumislomaketta ei löytynyt Keycloak-sivulta!")
        return None, None

    # 3. Lähetä tunnukset
    login_data = {"username": EMAIL, "password": PASSWORD, "credentialId": ""}
    resp2 = session.post(
        parser.action_url, data=login_data, allow_redirects=False, timeout=30
    )

    # 4. Tarkista uudelleenohjaus (joka sisältää koodin)
    location = resp2.headers.get("Location")
    if not location:
        print("VIRHE: Kirjautuminen epäonnistui tai ei uudelleenohjausta!")
        if "error" in resp2.text.lower():
            print("Väärä sähköposti tai salasana?")
        return None, None

    # 5. Pura koodi URL:sta
    parsed = urllib.parse.urlparse(location)
    fragment_qs = urllib.parse.parse_qs(parsed.fragment)
    query_qs = urllib.parse.parse_qs(parsed.query)

    code = fragment_qs.get("code", query_qs.get("code", [None]))[0]
    if not code:
        print("VIRHE: OIDC koodia ei löytynyt redirect-URL:sta!")
        return None, None

    # 6. Vaihda koodi access tokeniin
    token_url = "https://login.ecoreaction.fi/auth/realms/AanekoskenEnergia/protocol/openid-connect/token"
    token_data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }

    resp3 = session.post(token_url, data=token_data, timeout=30)
    if resp3.status_code != 200:
        print(f"VIRHE Tokenin haussa: {resp3.status_code} {resp3.text}")
        return None, None

    access_token = resp3.json().get("access_token")
    if not access_token:
        print("VIRHE: Access tokenia ei saatu.")
        return None, None

    print("Kirjautuminen onnistui! Access token haettu.")
    return session, access_token


def _iso_bound(day, end_of_day=False):
    time_part = (
        datetime.time(23, 59, 59) if end_of_day else datetime.time(0, 0, 0)
    )
    return datetime.datetime.combine(day, time_part, tzinfo=LOCAL_TZ).isoformat()


def fetch_series(session, access_token, start_day, end_day):
    """Hakee yhden aikavälin tuntisarjan."""
    api_url = (
        f"https://aanekoskenenergia.ecoreaction.fi/api/meteringPoints/ELECTRICITY/"
        f"{ELEC_MP}/series"
    )
    params = {
        "products": PRODUCT,
        "resolution": "DAYS_AS_HOURS",
        "startDate": _iso_bound(start_day),
        "endDate": _iso_bound(end_day, end_of_day=True),
    }
    resp = session.get(
        api_url,
        params=params,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=120,
    )
    if resp.status_code != 200:
        print(f"  {start_day} - {end_day}: VIRHE {resp.status_code} {resp.text[:200]}")
        return []
    rows = resp.json()
    print(f"  {start_day} - {end_day}: {len(rows)} riviä")
    return rows


def fetch_api_data():
    """
    Kirjautuu ja hakee koko historian START_DATEsta tähän päivään.
    Koko historia haetaan joka ajolla, jotta kumulatiivinen sum lasketaan
    aina samasta origosta - muuten HA:n tilastot menevät sekaisin.
    """
    session, access_token = login()
    if not access_token:
        return None

    start_date = parse_start_date(START_DATE_RAW)
    today = datetime.datetime.now(LOCAL_TZ).date()
    if start_date > today:
        print(f"VIRHE: start_date {start_date} on tulevaisuudessa.")
        return None

    print(f"Haetaan kulutushistoria {start_date} - {today} (kalenterivuosittain)...")

    data = []
    for year in range(start_date.year, today.year + 1):
        chunk_start = max(start_date, datetime.date(year, 1, 1))
        chunk_end = min(today, datetime.date(year, 12, 31))
        data.extend(fetch_series(session, access_token, chunk_start, chunk_end))

    print(f"Data haettu onnistuneesti! Rivejä yhteensä: {len(data)}")
    return data


def build_stats(data):
    """Muuntaa API-rivit HA:n tilastoformaattiin kumulatiivisella summalla."""
    hourly = {}
    skipped = 0
    for item in data:
        ts = parse_item_time(item)
        value = parse_item_value(item)
        if ts is None or value is None:
            skipped += 1
            continue
        # Sama tunti voi tulla useasta hausta - viimeisin voittaa
        hourly[ts] = value

    if skipped:
        print(f"Ohitettiin {skipped} riviä (ei tuntia tai ei arvoa).")

    stats = []
    sum_kwh = 0.0
    for ts in sorted(hourly):
        sum_kwh += hourly[ts]
        stats.append(
            {
                "start": ts.isoformat(),
                "state": hourly[ts],
                "sum": round(sum_kwh, 3),
            }
        )
    return stats


async def push_to_ha(data):
    if not data:
        print("Ei dataa lähetettäväksi.")
        return

    stats = build_stats(data)
    if not stats:
        print("Datan parsiminen ei tuottanut tuloksia.")
        return

    print(
        f"Lähetetään {len(stats)} datapistettä Home Assistantiin "
        f"({stats[0]['start']} - {stats[-1]['start']}, yhteensä {stats[-1]['sum']} kWh)..."
    )

    if not HA_TOKEN:
        print("VIRHE: SUPERVISOR_TOKEN puuttuu!")
        return

    metadata = {
        "has_mean": False,
        "has_sum": True,
        "name": "Äänekosken Energia Kulutus",
        "source": "recorder",
        "statistic_id": STATISTIC_ID,
        "unit_of_measurement": "kWh",
    }

    try:
        async with websockets.connect(HA_WS_URL, max_size=None) as ws:
            # 1. Auth
            await ws.recv()
            await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
            auth_resp = await ws.recv()
            if json.loads(auth_resp).get("type") != "auth_ok":
                print(f"VIRHE: Tunnistautuminen epäonnistui! {auth_resp}")
                return
            print("WebSocket-tunnistautuminen ok.")

            # 2. Import statistics paloissa
            message_id = 1
            for offset in range(0, len(stats), IMPORT_CHUNK_SIZE):
                chunk = stats[offset : offset + IMPORT_CHUNK_SIZE]
                await ws.send(
                    json.dumps(
                        {
                            "id": message_id,
                            "type": "recorder/import_statistics",
                            "metadata": metadata,
                            "stats": chunk,
                        }
                    )
                )
                try:
                    resp = await asyncio.wait_for(ws.recv(), timeout=60)
                    print(f"  Erä {message_id} ({len(chunk)} pistettä): {resp}")
                except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as e:
                    print(f"  Erä {message_id}: yhteys katkesi/aikakatkaisu: {e}")
                    return
                message_id += 1

            print("Tilastojen tuonti valmis.")
    except Exception as e:
        print(f"WebSocket-yhteys epäonnistui: {e}")
        import traceback

        traceback.print_exc()


async def sync_once():
    data = fetch_api_data()
    await push_to_ha(data)


async def main():
    print("Äänekosken Energia -lisäosa käynnistetty.")
    if not EMAIL or not PASSWORD:
        print("VIRHE: Sähköpostia tai salasanaa ei ole asetettu asetuksissa!")
        return

    while True:
        now = datetime.datetime.now()
        if str(now.hour).zfill(2) == str(RUN_HOUR).zfill(2) and str(now.minute).zfill(
            2
        ) == str(RUN_MINUTE).zfill(2):
            print(f"Kello on {RUN_HOUR}:{RUN_MINUTE}, aloitetaan synkronointi!")
            try:
                await sync_once()
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
        await sync_once()
    except Exception as e:
        print(f"Virhe ensimmäisessä ajossa: {e}")
        import traceback

        traceback.print_exc()

    print(f"Siirrytään ajastettuun tilaan. Seuraava ajo päivittäin klo {RUN_HOUR}:{RUN_MINUTE}.")
    await main()


if __name__ == "__main__":
    asyncio.run(startup())
