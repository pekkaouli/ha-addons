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
METERING_POINT = str(options.get("metering_point", "")).strip()

# Hinnoittelu (snt/kWh, sis. ALV 25,5 %). Kaikki asetuksista, oletukset
# Aanekosken Energian hinnastosta 19.10.2026 ja helmikuun laskurista.
def _f(avain, oletus):
    try: return float(options.get(avain, oletus))
    except (TypeError, ValueError): return float(oletus)

MARGINAALI      = _f("margin", 0.40)          # porssisahkon myyntimarginaali, sis. ALV
ALV_KERROIN     = 1.0 + _f("vat_percent", 25.5) / 100.0  # porssihinta on veroton
SIIRTO_PAIVA    = _f("transfer_day", 4.20)
SIIRTO_YO       = _f("transfer_night", 2.00)
SAHKOVERO       = _f("tax", 2.83)
HINTA_YLEIS     = _f("price_yleissahko", 10.50)
HINTA_AIKA_PV   = _f("price_aikasahko_day", 11.50)
HINTA_AIKA_YO   = _f("price_aikasahko_night", 9.40)

PAIVA_ALKAA, PAIVA_PAATTYY = 7, 22            # siirron ja aikasahkon paivaraja

# Home Assistantin sisäinen osoite add-on ympäristössä
HA_WS_URL = "ws://supervisor/core/api/websocket"
HA_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Keycloak login asetukset
LOGIN_START_URL = "https://aanekoskenenergia.ecoreaction.fi/online/tracking"
PRODUCT = "EL_ENERGY_CONSUMPTION"
STATISTIC_ID = "sensor.aanekosken_energia_kulutus"

# Spot-hintasarja samasta portaalista (parametrit vanhasta fetch_data.py:sta)
SPOT_TYPE    = "PRICE"
SPOT_NUMBER  = "SPOT_PRICE#NPSPOT"
SPOT_PRODUCT = "PRICE"

# Kustannussarjat ulkoisina tilastoina. Kaksoispiste-muotoinen tunniste ei voi
# tormata entiteettiin - juuri se torma rikkoi kulutustilaston syksylla.
KUST_SPOT   = "aanekoski_sync:kustannus_spot"
KUST_YLEIS  = "aanekoski_sync:kustannus_yleissahko"
KUST_AIKA   = "aanekoski_sync:kustannus_aikasahko"

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


def parse_item_value(item, product=None):
    values = item.get("values") or {}
    entry = values.get(f"{product or PRODUCT}#0")
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


def fetch_series(session, access_token, start_day, end_day,
                 mtype="ELECTRICITY", mnumber=None, product=None, label="Kulutus"):
    """Hakee yhden aikavälin tuntisarjan. Sama rajapinta palvelee sekä
    kulutusta (ELECTRICITY) että spot-hintaa (PRICE)."""
    mnumber = mnumber if mnumber is not None else METERING_POINT
    product = product if product is not None else PRODUCT
    api_url = (
        f"https://aanekoskenenergia.ecoreaction.fi/api/meteringPoints/{mtype}/"
        f"{requests.utils.quote(mnumber, safe='')}/series"
    )
    params = {
        "products": product,
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
        print(f"  {label} {start_day} - {end_day}: VIRHE {resp.status_code} {resp.text[:160]}")
        return []
    rows = resp.json()
    print(f"  {label} {start_day} - {end_day}: {len(rows)} riviä")
    return rows


def fetch_api_data():
    """
    Kirjautuu ja hakee koko historian START_DATEsta tähän päivään.
    Koko historia haetaan joka ajolla, jotta kumulatiivinen sum lasketaan
    aina samasta origosta - muuten HA:n tilastot menevät sekaisin.
    """
    if not METERING_POINT:
        print("VIRHE: Käyttöpaikkatunnus (metering_point) puuttuu asetuksista!")
        return None

    session, access_token = login()
    if not access_token:
        return None

    start_date = parse_start_date(START_DATE_RAW)
    today = datetime.datetime.now(LOCAL_TZ).date()
    if start_date > today:
        print(f"VIRHE: start_date {start_date} on tulevaisuudessa.")
        return None

    print(f"Haetaan kulutus ja spot-hinta {start_date} - {today} (kalenterivuosittain)...")

    kulutus, spot = [], []
    for year in range(start_date.year, today.year + 1):
        a = max(start_date, datetime.date(year, 1, 1))
        b = min(today, datetime.date(year, 12, 31))
        kulutus.extend(fetch_series(session, access_token, a, b, label="Kulutus"))
        spot.extend(fetch_series(session, access_token, a, b,
                                 mtype=SPOT_TYPE, mnumber=SPOT_NUMBER,
                                 product=SPOT_PRODUCT, label="Spot   "))

    print(f"Haettu: kulutus {len(kulutus)} riviä, spot {len(spot)} riviä")
    return {"kulutus": kulutus, "spot": spot}


def tunneittain(rows, product=None, nimi=""):
    """API-rivit -> {aika: arvo}. Sama tunti useasta hausta: viimeisin voittaa."""
    hourly, skipped = {}, 0
    for item in rows:
        ts = parse_item_time(item)
        value = parse_item_value(item, product)
        if ts is None or value is None:
            skipped += 1
            continue
        hourly[ts] = value
    if skipped:
        print(f"  {nimi}: ohitettiin {skipped} riviä (ei tuntia tai ei arvoa)")
    return hourly


def kumulatiivinen(hourly, pyoristys=3):
    """{aika: arvo} -> HA:n tilastoformaatti kumulatiivisella summalla."""
    stats, summa = [], 0.0
    for ts in sorted(hourly):
        summa += hourly[ts]
        stats.append({"start": ts.isoformat(),
                      "state": round(hourly[ts], pyoristys),
                      "sum": round(summa, pyoristys)})
    return stats


def on_paiva(ts):
    """Siirron ja aikasähkön päiväaika 07-22 paikallista aikaa."""
    return PAIVA_ALKAA <= ts.astimezone(LOCAL_TZ).hour < PAIVA_PAATTYY


def build_stats(data):
    """Kulutustilasto (kWh) - säilytetty nimi yhteensopivuuden vuoksi."""
    return kumulatiivinen(tunneittain(data, PRODUCT, "kulutus"))


def build_cost_stats(kwh_tunnit, spot_tunnit):
    """Kolme kustannussarjaa samasta kulutuksesta, EUR per tunti.

    Jokaiseen lisätään sama siirto ja sähkövero, joten Energia-paneelin luku on
    rehellinen ja sarjat ovat keskenään vertailukelpoisia. Perusmaksut jätetään
    pois - ne eivät ole kWh-sidonnaisia eivätkä kuulu tuntitilastoon.
    """
    spot_puuttuu = 0
    sarjat = {KUST_SPOT: {}, KUST_YLEIS: {}, KUST_AIKA: {}}
    for ts, kwh in kwh_tunnit.items():
        paiva = on_paiva(ts)
        siirto = SIIRTO_PAIVA if paiva else SIIRTO_YO
        kiintea_osa = siirto + SAHKOVERO

        spot = spot_tunnit.get(ts)
        if spot is None:
            spot_puuttuu += 1
        else:
            # Portaali palauttaa veroton EUR/MWh -> snt/kWh sis. ALV
            spot_snt = spot / 10.0 * ALV_KERROIN
            sarjat[KUST_SPOT][ts] = kwh * (spot_snt + MARGINAALI + kiintea_osa) / 100.0

        sarjat[KUST_YLEIS][ts] = kwh * (HINTA_YLEIS + kiintea_osa) / 100.0
        aika_hinta = HINTA_AIKA_PV if paiva else HINTA_AIKA_YO
        sarjat[KUST_AIKA][ts] = kwh * (aika_hinta + kiintea_osa) / 100.0

    if spot_puuttuu:
        print(f"  spot-hinta puuttui {spot_puuttuu} tunnilta - ne jätetään spot-sarjasta pois")
    return {sid: kumulatiivinen(h, 4) for sid, h in sarjat.items()}


async def laheta_tilastot(sarjat):
    """sarjat: lista (metadata, stats). Tuo kaikki yhdellä WebSocket-yhteydellä."""
    if not HA_TOKEN:
        print("VIRHE: SUPERVISOR_TOKEN puuttuu!")
        return False
    try:
        async with websockets.connect(HA_WS_URL, max_size=None) as ws:
            await ws.recv()
            await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
            vastaus = await ws.recv()
            if json.loads(vastaus).get("type") != "auth_ok":
                print(f"VIRHE: Tunnistautuminen epäonnistui! {vastaus}")
                return False
            print("WebSocket-tunnistautuminen ok.")

            viesti_id = 1
            for metadata, stats in sarjat:
                if not stats:
                    print(f"  {metadata['statistic_id']}: ei dataa, ohitetaan")
                    continue
                yks = metadata.get("unit_of_measurement")
                print(f"  {metadata['statistic_id']}: {len(stats)} pistettä, "
                      f"yhteensä {stats[-1]['sum']} {yks}")
                for kohta in range(0, len(stats), IMPORT_CHUNK_SIZE):
                    era = stats[kohta:kohta + IMPORT_CHUNK_SIZE]
                    await ws.send(json.dumps({"id": viesti_id,
                                              "type": "recorder/import_statistics",
                                              "metadata": metadata, "stats": era}))
                    try:
                        r = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                    except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as e:
                        print(f"    erä {viesti_id}: yhteys katkesi/aikakatkaisu: {e}")
                        return False
                    if not r.get("success"):
                        print(f"    erä {viesti_id}: VIRHE {r.get('error')}")
                        return False
                    viesti_id += 1
            print("Tilastojen tuonti valmis.")
            return True
    except Exception as e:
        print(f"WebSocket-yhteys epäonnistui: {e}")
        import traceback
        traceback.print_exc()
        return False


def _kust_meta(sid, nimi):
    return {"has_mean": False, "has_sum": True, "name": nimi,
            "source": sid.split(":")[0], "statistic_id": sid,
            "unit_of_measurement": "EUR"}


async def push_to_ha(data):
    if not data:
        print("Ei dataa lähetettäväksi.")
        return

    kwh_tunnit  = tunneittain(data.get("kulutus", []), PRODUCT, "kulutus")
    spot_tunnit = tunneittain(data.get("spot", []), SPOT_PRODUCT, "spot")
    if not kwh_tunnit:
        print("Kulutusdatan parsiminen ei tuottanut tuloksia.")
        return
    print(f"Tunteja: kulutus {len(kwh_tunnit)}, spot {len(spot_tunnit)}")

    kulutus_stats = kumulatiivinen(kwh_tunnit)
    kustannukset = build_cost_stats(kwh_tunnit, spot_tunnit)

    sarjat = [
        ({"has_mean": False, "has_sum": True,
          "name": "Äänekosken Energia Kulutus", "source": "recorder",
          "statistic_id": STATISTIC_ID, "unit_of_measurement": "kWh"}, kulutus_stats),
        (_kust_meta(KUST_SPOT,  "Sähkö: toteutunut (pörssi)"),   kustannukset[KUST_SPOT]),
        (_kust_meta(KUST_YLEIS, "Sähkö: vertailu Yleissähkö"),   kustannukset[KUST_YLEIS]),
        (_kust_meta(KUST_AIKA,  "Sähkö: vertailu Aikasähkö"),    kustannukset[KUST_AIKA]),
    ]
    await laheta_tilastot(sarjat)


async def sync_once():
    data = fetch_api_data()
    await push_to_ha(data)


async def main():
    print("Äänekosken Energia -lisäosa käynnistetty.")
    if not EMAIL or not PASSWORD:
        print("VIRHE: Sähköpostia tai salasanaa ei ole asetettu asetuksissa!")
        return
    if not METERING_POINT:
        print(
            "VIRHE: Käyttöpaikkatunnus (metering_point) puuttuu asetuksista!\n"
            "       Löydät sen portaalista kohdasta Käyttöpaikat."
        )
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
