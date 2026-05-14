# Getijden iFrame pagina

Kleine Flask-app die een HTML-pagina toont met:
- tijdstippen van hoog- en laagwater
- een grafiek met waterstanden per dag
- locatiekeuze via pull-down met zoekveld

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



