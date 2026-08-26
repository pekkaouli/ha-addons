FROM python:3.12-slim-bookworm

# Asennetaan tarvittavat ohjelmistot (Chromium on välttämätön Playwrightille)
RUN apt-get update && apt-get install -y \
    chromium \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Asennetaan Python-paketit
RUN pip3 install --no-cache-dir playwright websockets requests

# Kerrotaan Playwrightille, että käytetään järjestelmän Chromiumia
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

# Kopioidaan skriptit
COPY run.sh /
COPY aanekoski_sync.py /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
