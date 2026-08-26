ARG BUILD_FROM
FROM $BUILD_FROM

# Asennetaan tarvittavat ohjelmistot (Chromium on välttämätön Playwrightille)
RUN apk add --no-cache \
    chromium \
    jq \
    python3 \
    py3-pip

# Asennetaan Python-paketit
RUN pip3 install --no-cache-dir playwright websockets requests --break-system-packages

# Kerrotaan Playwrightille, että käytetään järjestelmän Chromiumia eikä ladata sen omaa
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium-browser

# Kopioidaan skriptit
COPY run.sh /
COPY aanekoski_sync.py /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
