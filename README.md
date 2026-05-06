# PTSquawk

Real-time aircraft distress monitor for Portuguese airspace.

Tracks emergency squawk codes transmitted by aircraft over continental Portugal, Açores, and Madeira:

| Code | Meaning |
|------|---------|
| 7700 | General emergency |
| 7600 | Radio communication failure |
| 7500 | Unlawful interference (hijack) |

## Live site

**https://nelsonandreproton.github.io/PTSquawk/**

## Features

- Dark map (CartoDB Dark Matter + Leaflet) consistent with [PTStorms](https://github.com/nelsonandreproton/PTStorms)
- Three monitored regions: continental Portugal, Açores, Madeira — each with a visible bounding box
- Click any aircraft marker to see model, airline, origin, destination, altitude, speed, and heading
- Aircraft data from [OpenSky Network](https://opensky-network.org/) — refreshed every 60 seconds
- Metadata enrichment (aircraft model, operator) from [hexdb.io](https://hexdb.io/)
- Browser notification on new emergency (requires permission)
- Debug mode (👁 button) to show all aircraft regardless of squawk code

## How it works

1. Fetches all aircraft states within three bounding boxes from the OpenSky Network REST API
2. Filters for squawk codes 7700, 7600, 7500
3. Renders aircraft on the map; enriches each popup with model/airline/route data on demand
4. Enrichment results are cached for the session to avoid hammering APIs

## CORS / local development

OpenSky allows requests from `github.io` origins without credentials. Requests from `localhost` or `file://` are blocked by CORS.

To run locally, add your OpenSky credentials directly in `index.html`:

```js
const OPENSKY_USER = 'your_username';
const OPENSKY_PASS = 'your_password';
```

Never commit credentials. Free OpenSky accounts are available at [opensky-network.org](https://opensky-network.org/index.php?option=com_users&view=registration).

## Stack

- Vanilla JS (no build step)
- [Leaflet 1.9.4](https://leafletjs.com/) with CartoDB Dark Matter tiles
- [OpenSky Network API](https://opensky-network.org/apidoc/)
- [hexdb.io API](https://hexdb.io/)
- Hosted on GitHub Pages
