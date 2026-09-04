FROM python:3.12-slim-bookworm

# Asennetaan vain Python-paketit (ei enää Chromiumia!)
RUN pip3 install --no-cache-dir websockets requests tzdata

# Kopioidaan skriptit
COPY run.sh /
COPY aanekoski_sync.py /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
