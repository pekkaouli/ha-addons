# Systeemikuvaus: Äänekosken Energia Synkronointi (Home Assistant Add-on)

## Yleiskuvaus
Tämä järjestelmä on Home Assistant -lisäosa (Add-on), jonka tarkoituksena on hakea automaattisesti sähkönkulutustiedot Äänekosken Energian (eCoreaction) asiakasportaalista ja tuoda ne Home Assistantin energiaseurantaan. 

Lisäosa toimii taustalla omana Docker-konttinaan Home Assistant -palvelimella ja suorittaa synkronoinnin kerran vuorokaudessa käyttäjän määrittelemänä ajankohtana.

## Arkkitehtuuri
Järjestelmä on rakennettu Home Assistantin Add-on -arkkitehtuurin mukaisesti. Se koostuu seuraavista päätiedostoista:

* **`config.yaml`**: Määrittelee lisäosan metatiedot, käyttöoikeudet (Home Assistant API) ja käyttäjän konfiguroitavat asetukset (sähköposti, salasana, käyttöpaikkatunnus, historian alkupäivä ja ajastuksen kellonaika).
* **`Dockerfile`**: Kertoo Home Assistantille, miten lisäosan virtuaalinen ympäristö (Docker-kontti) rakennetaan (esim. asennetaan Python ja tarvittavat kirjastot).
* **`run.sh`**: Käynnistysskripti, joka suoritetaan kun kontti käynnistyy. Käynnistää varsinaisen Python-ohjelman.
* **`aanekoski_sync.py`**: Järjestelmän sydän. Sisältää kaiken varsinaisen toimintalogiikan.

## Toimintalogiikka (aanekoski_sync.py)

Ohjelman suoritus jakautuu neljään päävaiheeseen:

### 1. Ajastus ja käynnistys
Kun lisäosa käynnistetään, `startup()`-funktio tekee välittömästi ensimmäisen tietojen haun. Tämän jälkeen ohjelma siirtyy `main()`-funktion ikuiseen silmukkaan. Silmukka tarkistaa kellonajan kerran minuutissa ja aktivoituu, kun kello vastaa asetuksissa määriteltyä kellonaikaa (esim. klo 12:00).

### 2. Tunnistautuminen (OIDC PKCE)
Tietojen haku vaatii tunnistautumisen Äänekosken Energian Keycloak-pohjaiseen tunnistautumispalveluun.
* Ohjelma aloittaa OIDC PKCE (Proof Key for Code Exchange) -kirjautumisvuon.
* Se noutaa kirjautumissivun, parsii sieltä dynaamisen lomakkeen lähetysosoitteen ja lähettää käyttäjän sähköpostin ja salasanan.
* Onnistuneen kirjautumisen jälkeen ohjelma sieppaa uudelleenohjausosoitteesta (redirect URL) `code`-parametrin.
* `code` vaihdetaan lopuksi varsinaiseen `access_token` -pääsytunnisteeseen.

### 3. Datan hakeminen (API)
Kun tunnistautuminen on valmis ja `access_token` on saatu:
* Ohjelma hakee **koko historian** asetuksissa määritellystä alkupäivästä (`start_date`) tähän päivään, kalenterivuosi kerrallaan.
* Koko historia haetaan joka ajolla tarkoituksella: kumulatiivinen summa on laskettava aina samasta origosta, muuten Home Assistantin tilastot menevät sekaisin (ks. Tunnetut sudenkuopat).
* Se tekee HTTP GET -pyynnön Äänekosken Energian API:in (`/api/meteringPoints/ELECTRICITY/.../series`).
* Pyyntö kohdistuu asetuksissa määriteltyyn käyttöpaikkaan (`metering_point`) ja pyytää sähkönkulutustietoja tuntiresoluutiolla. Käyttöpaikkatunnus löytyy portaalista kohdasta Käyttöpaikat.
* Palvelin palauttaa tiedot JSON-muodossa. Aika luetaan kentistä `year` / `month` / `day` / `hour`, ei `timestamp`-kentästä (ks. Tunnetut sudenkuopat 2). Rivit joilta `hour` puuttuu ovat päiväaggregaatteja ja ne ohitetaan.

### 4. Datan tuonti Home Assistantiin (WebSocket)
Home Assistantin tilastoihin ei voi suoraan syöttää historiallista dataa tavallisten sensorien kautta. Siksi ohjelma käyttää Home Assistantin sisäistä WebSocket-rajapintaa:
* JSON-data käydään läpi ja muotoillaan Home Assistantin ymmärtämään muotoon (`stats`-lista, jossa on aika, arvo ja kumulatiivinen summa).
* Tunnit deduplikoidaan ja järjestetään ajan mukaan, minkä jälkeen `sum` lasketaan juoksevana summana historian alusta.
* Ohjelma avaa WebSocket-yhteyden Home Assistantiin (`ws://supervisor/core/api/websocket`) ja tunnistautuu automaattisesti taustalla olevalla `SUPERVISOR_TOKEN`illa.
* Se lähettää `recorder/import_statistics` -komennon ja tilastodatan.
* Data lähetetään 5000 pisteen erissä, jottei yksittäisestä viestistä tule liian suurta.
* Tämän seurauksena Home Assistantin tietokantaan syntyy / päivittyy **pitkän aikavälin tilasto** `sensor.aanekosken_energia_kulutus`, jota voidaan käyttää Energia-paneelissa. Huom: kyseessä ei ole tavallinen entiteetti - sillä ei ole tilaa eikä se näy entiteettilistassa.

## Turvallisuus ja Ympäristö
* Tunnukset (sähköposti ja salasana) tallennetaan turvallisesti Home Assistantin omaan `/data/options.json` -tiedostoon, jonne ei ole ulkopuolelta pääsyä.
* Ohjelma liikennöi ulospäin vain HTTPS-salatuilla yhteyksillä Äänekosken Energian palvelimille.
* Home Assistantin sisäinen kommunikointi tapahtuu suljetussa Docker-verkossa.

## Tunnetut sudenkuopat

Versiossa 1.3.0 korjattiin summabugi ja poistettiin yksi latentti riski. Kolmas sudenkuoppa löytyi vasta tuotannossa. Kaikki kolme kannattaa pitää mielessä jatkokehityksessä:

1. **Kumulatiivinen summa nollautui joka ajolla.** `recorder/import_statistics` tulkitsee `sum`-kentän koko tilaston elinkaaren absoluuttisena juoksevana summana, ei haetun ikkunan summana. Kun lisäosa haki liukuvan 7 päivän ikkunan ja aloitti summan nollasta, jokainen ajo ylikirjoitti viimeiset päivät pienemmillä summilla kuin ympäröivä data. Energia-paneeli laskee kuukauden kulutuksen summien erotuksena, joten sarjaan syntyi valtavia negatiivisia ja positiivisia piikkejä. Korjaus: haetaan koko historia kiinteästä alkupäivästä.

2. **`timestamp`-kentän hauraus.** Tämä *ei* aiheuttanut vikaa, vaikka v1.3.0:n commit-viesti niin väittää - virhe korjataan tässä. Kulutussarjassa `timestamp` on olemassa ja versiot 1.2.x toimivat sen varassa. Sen sijaan **spot-hintasarjassa päiväresoluutiolla kenttä on `null`**, ja koska `item.get("timestamp", "")` palauttaa `None` eikä oletusarvoa (avain on olemassa), `data.sort(key=...)` olisi kaatunut virheeseen `TypeError: '<' not supported between instances of 'NoneType' and 'NoneType'` heti jos kenttä olisi tyhjentynyt. Aika rakennetaan nyt `year`/`month`/`day`/`hour`-kentistä - samoin kuin vanha, toimiva `fetch_data.py` teki. Latentti riski poistui, mutta **varsinainen bugi oli vain kohta 1.**

3. **Nimitörmäys oikean entiteetin kanssa.** Kavalin näistä. Jos Home Assistantissa on **oikea entiteetti** samalla nimellä kuin tuotu tilasto (`sensor.aanekosken_energia_kulutus`) ja sillä on `state_class: total_increasing`, HA:n recorder kompiloi sille itse tunnittain tilastorivin samaan `statistic_id`:hen. Tuotu sarja hajoaa hiljaisesti tunnin sisällä, riippumatta lisäosan versiosta. Näin kävi MQTT-discoveryn jättämän jäänne-entiteetin takia: sen tila oli jäätynyt arvoon 75989.13, ja tilastoon ilmestyi rivi `sum=0.0`. Korjaus oli poistaa entiteetti rekisteristä.

   Ensimmäinen tarkistus kun luvut näyttävät järjettömiltä: `GET /api/states/sensor.aanekosken_energia_kulutus`. Jos vastaus on 200, tämä on syy. Rakenteellinen suoja olisi vaihtaa ulkoiseen tilastoon (`aanekoski_sync:kulutus`, `source: aanekoski_sync`), koska kaksoispiste-muotoiset tunnisteet eivät voi törmätä entiteetteihin.

**Jos tilastot ovat jo sotkeutuneet**, pelkkä koodikorjaus ei riitä — virheelliset rivit jäävät tietokantaan. Poista tilasto `sensor.aanekosken_energia_kulutus` Kehittäjätyökalut → Tilastot -näkymästä, minkä jälkeen lisäosa kirjoittaa historian puhtaalta pöydältä.
