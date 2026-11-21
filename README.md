# Board-AirQ

Dashboard **Qualità aria vs Purificatori** per [boards.mrg.im](https://boards.mrg.im) — stile Omarchy, come Studio Dubai / Van Hunter.

Montata a `/airq/` (web) e `/airq/tv.html` (OmarchyTV). Tre numeri + grafico combinato delle ultime 24h:

- **Capacità in uso** (m³/h aria pulita) — `sensor.purifiers_clean_air_rate` (CADR dei 3 Core 300S, 0..720)
- **PM2.5 interna** (µg/m³) — `sensor.indoor_air_quality_pm2_5`
- **PM2.5 esterna** (µg/m³) — `sensor.dubai_outdoor_air_quality`

Dati **LIVE** da Home Assistant locale (127.0.0.1:8123). Nessuno snapshot in git.

## Struttura
- `web/index.html` (+ `tv.html`): la board. Colori e wallpaper seguono il tema Omarchy live (`/theme/omarchy.css` + `/theme/events`). Grafico disegnato su canvas (nessuna dipendenza esterna).
- `api/airq_api.py`: servizio always-on su `127.0.0.1:8142` (`board-airq-api.service`) che legge HA (stati correnti + storia 24h) e serve `/api/data` e `/api/status`. `HA_TOKEN` vive solo nell'env del servizio, mai in git.
- `api/studio2_plugin.py`: plugin del server statico (caricato via `STUDIO2_PLUGINS`) che inoltra `/airq/api/*` al servizio, così la board resta same-origin.
- `board.json`: manifest (id `airq`, root `web`).

## Deploy su Domvs
1. `board-airq-api.service` (unit in `ops/`, `HA_TOKEN` reale nell'env) → porta 8142.
2. `api/studio2_plugin.py` aggiunto a `STUDIO2_PLUGINS` di `studio2-dashboard.service`, poi restart.
3. Board aggiunta alla griglia via hub (`POST /boards/api/add` con `~/boards/hub.token`): clona in `~/boards/repos/airq`, symlink `~/studio2/airq`.

Aggiornamenti: `git push` → l'hub fa `git pull --ff-only` (statici + plugin). Il servizio API va riavviato per ricaricare `airq_api.py`.
