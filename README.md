# Getijden iFrame pagina

Kleine Flask-app die een HTML-pagina toont met:
- tijdstippen van hoog- en laagwater
- een grafiek met waterstanden per dag
- datumkeuze via pull-down

Data komt uit:
- https://rijkswaterstaatdata.nl/waterdata/
- https://ddapi20-waterwebservices.rijkswaterstaat.nl/swagger-ui/index.html

## Configuratie

Environment variabelen:
- `RWS_LOCATION_CODE` (default: `dordrecht.oudemaas.benedenmerwede`)
- `PORT` (default: `8000`)

API endpoints:
- `GET /api/tides?date=YYYY-MM-DD&location=<locatiecode>`
- `GET /api/locations?q=<zoekterm>&limit=60`
- `GET /health`

Parametervalidatie (misbruikpreventie):
- `date` moet `YYYY-MM-DD` zijn en binnen `vandaag - 31 dagen` t/m `vandaag + 183 dagen` vallen.
- `location` accepteert alleen letters/cijfers en `.` `_` `-` (max 80 tekens).
- `limit` moet een geheel getal zijn tussen `1` en `200`.
- `q` is maximaal 80 tekens en control characters worden geweigerd.

HTML endpoints:
- `GET /` volledige pagina met datumkeuze + grafiek
- `GET /vandaag` compacte pagina voor vandaag (zonder grafiek) met alleen hoog- en laagwatertijden

## Lokaal draaien

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open daarna: `http://localhost:8000`

## Docker

Build en run:

```bash
docker build -t getijden-iframe .
docker run --rm -p 8000:8000 \
  -e RWS_LOCATION_CODE=dordrecht.oudemaas.benedenmerwede \
  getijden-iframe
```

De container start de app nu via **Gunicorn** als productie-WSGI server.
De bind-poort volgt daarbij de environment variabele `PORT` (default: `8000`).

Handmatig lokaal met Gunicorn starten kan ook:

```bash
gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 60 app:app
```

## GitHub Actions

Er zijn twee workflows toegevoegd:
- `tests.yml`: draait de testsuite bij elke push en pull request.
- `release-docker.yml`: bouwt en pusht het Docker image naar Docker Hub bij een gepubliceerde GitHub release.

Benodigde GitHub repository secrets:
- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN` (Docker Hub access token)

Image tags bij release:
- `<dockerhub-user>/getijden-iframe:latest`
- `<dockerhub-user>/getijden-iframe:<release-tag>`

## iFrame embed

```html
<iframe
  src="https://jouw-host/"
  width="100%"
  height="520"
  style="border:0"
  loading="lazy"
></iframe>
```

Compacte embed voor alleen vandaag:

```html
<iframe
  src="https://jouw-host/vandaag"
  width="100%"
  height="240"
  style="border:0"
  loading="lazy"
></iframe>
```

## Example docker-compose

To run the app with Docker Compose, create a `docker-compose.yml` file with the following content:

```yaml
services:
  getijden-iframe:
    image: reinoud/getijden-iframe
    restart: unless-stopped
    ports:
      - 127.0.0.1:8000:8000
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
```

