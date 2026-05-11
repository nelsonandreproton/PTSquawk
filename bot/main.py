"""
PTSquawk bot — Portuguese airspace emergency monitor
- Polls airplanes.live every 60s for squawk 7700/7600/7500
- Sends Telegram alert on first detection
- Persists last 10 events to /data/history.json
- Exposes Flask API: GET /health, GET /history (CORS *)
"""
import html
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify
from flask_cors import CORS
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

# Heartbeat integration — optional: silently skipped if module not available
# (e.g. during local development without the HetznerCheck volume mounted).
try:
    from heartbeat import beat as _hb_beat  # mounted at /hetznercheck via PYTHONPATH
    _HEARTBEAT_AVAILABLE = True
except ImportError:
    _HEARTBEAT_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

POLL_INTERVAL = 60
HISTORY_MAX   = 10
HISTORY_PATH  = Path(os.environ.get("HISTORY_PATH", "/data/history.json"))

EMERGENCY_SQUAWKS = {"7700", "7600", "7500"}
SQUAWK_LABEL = {
    "7700": "Emergência geral",
    "7600": "Falha de comunicações",
    "7500": "Interferência ilícita",
}
SQUAWK_EMOJI = {
    "7700": "🆘",
    "7600": "📻",
    "7500": "⚠️",
}

REGIONS = [
    {"label": "Portugal continental", "lat": 39.5, "lon": -8.0,  "radius": 250},
    {"label": "Açores",               "lat": 38.5, "lon": -28.0, "radius": 250},
    {"label": "Madeira",              "lat": 32.7, "lon": -17.0, "radius": 150},
]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ── HTTP session ──────────────────────────────────────────────────────────────
session = requests.Session()
session.headers["User-Agent"] = "PTSquawk/1.0"


def _is_retryable(exc: BaseException) -> bool:
    """Retry on network errors and 5xx, never on 429."""
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code >= 500
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


# ── airplanes.live fetch ──────────────────────────────────────────────────────
@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _fetch_region_raw(lat: float, lon: float, radius: int) -> list[dict]:
    url = f"https://api.airplanes.live/v2/point/{lat}/{lon}/{radius}"
    r = session.get(url, timeout=15)
    if r.status_code == 429:
        log.warning("airplanes.live rate-limited (429) — skipping cycle")
        return []
    r.raise_for_status()
    return r.json().get("ac", [])


def fetch_all_aircraft() -> list[dict]:
    """Fetch all 3 regions, deduplicate by hex, tolerate per-region failures."""
    seen: set[str] = set()
    result: list[dict] = []
    for region in REGIONS:
        try:
            aircraft = _fetch_region_raw(region["lat"], region["lon"], region["radius"])
            for ac in aircraft:
                h = ac.get("hex", "")
                if h and h not in seen:
                    seen.add(h)
                    result.append(ac)
        except Exception as exc:
            log.warning("Region %s failed: %s", region["label"], exc)
    return result


# ── hexdb enrichment ──────────────────────────────────────────────────────────
_enrich_cache: dict[str, dict] = {}


def enrich_hexdb(icao: str) -> dict:
    if icao in _enrich_cache:
        return _enrich_cache[icao]
    try:
        r = session.get(
            f"https://hexdb.io/api/v1/aircraft/{icao.lower()}",
            timeout=8,
        )
        if r.ok:
            data = r.json()
            entry = {
                "model":    " ".join(filter(None, [data.get("Manufacturer"), data.get("Type")])) or None,
                "operator": data.get("RegisteredOwners") or data.get("OperatorICAO") or None,
            }
            _enrich_cache[icao] = entry
            return entry
    except Exception:
        pass
    _enrich_cache[icao] = {}
    return {}


# ── History ───────────────────────────────────────────────────────────────────
_history_lock = threading.Lock()


def read_history() -> dict:
    with _history_lock:
        try:
            return json.loads(HISTORY_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {"events": []}


def append_history(event: dict) -> None:
    with _history_lock:
        try:
            existing = json.loads(HISTORY_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            existing = {"events": []}
        events = [event] + existing.get("events", [])
        events = events[:HISTORY_MAX]
        tmp = HISTORY_PATH.with_suffix(".tmp")
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"events": events}, ensure_ascii=False, indent=2))
        tmp.replace(HISTORY_PATH)


def seed_known_emergencies() -> set[tuple[str, str]]:
    """Load active emergencies from history to avoid re-firing after restart."""
    known: set[tuple[str, str]] = set()
    history = read_history()
    for ev in history.get("events", []):
        icao   = ev.get("icao24", "")
        squawk = ev.get("squawk", "")
        if icao and squawk:
            known.add((icao, squawk))
    log.info("Seeded %d known emergencies from history", len(known))
    return known


# ── Telegram ──────────────────────────────────────────────────────────────────
def format_telegram(ac: dict, enrich: dict) -> str:
    squawk = ac.get("squawk", "")
    emoji  = SQUAWK_EMOJI.get(squawk, "🚨")
    label  = SQUAWK_LABEL.get(squawk, squawk)

    callsign = html.escape((ac.get("flight") or "").strip() or ac.get("hex", "").upper())
    reg      = html.escape(ac.get("r") or "-")
    model    = html.escape(enrich.get("model") or ac.get("desc") or ac.get("t") or "-")
    operator = html.escape(enrich.get("operator") or "-")

    lat = ac.get("lat")
    lon = ac.get("lon")
    pos = f"{lat:.4f}°N {abs(lon):.4f}°{'W' if lon < 0 else 'E'}" if lat is not None and lon is not None else "-"

    alt_baro = ac.get("alt_baro")
    if alt_baro == "ground":
        alt_str = "Em solo"
    elif isinstance(alt_baro, (int, float)):
        alt_m = round(alt_baro * 0.3048)
        alt_str = f"{alt_baro:,} ft / {alt_m:,} m".replace(",", " ")
    else:
        alt_str = "-"

    gs = ac.get("gs")
    speed_str = f"{round(gs)} kt" if isinstance(gs, (int, float)) else "-"

    track = ac.get("track")
    heading_str = f"{round(track)}°" if isinstance(track, (int, float)) else "-"

    orig = html.escape(ac.get("orig") or "-")
    dest = html.escape(ac.get("dest") or "-")

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"{emoji} <b>Emergência Aérea — Squawk {squawk}</b>\n"
        f"<i>{label}</i>\n"
        f"\n"
        f"✈️ <b>{reg}</b> · {callsign}\n"
        f"🛩 {model} · {operator}\n"
        f"📍 {pos} · altitude {alt_str}\n"
        f"💨 {speed_str} · rumo {heading_str}\n"
        f"🛫 {orig} → {dest}\n"
        f"🕐 {ts}\n"
        f"\n"
        f"#squawk{squawk} #ptsquawk"
    )


def send_telegram(text: str) -> None:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":                  TELEGRAM_CHAT_ID,
                "text":                     text,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if not r.ok:
            log.warning("Telegram error %s: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)


# ── Event builder ─────────────────────────────────────────────────────────────
def build_event(ac: dict, enrich: dict) -> dict:
    alt_baro = ac.get("alt_baro")
    alt_ft   = alt_baro if isinstance(alt_baro, (int, float)) else None
    alt_m    = round(alt_ft * 0.3048) if alt_ft is not None else None
    gs       = ac.get("gs")
    return {
        "id":           f"{ac.get('hex','')}-{int(time.time())}",
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "icao24":       ac.get("hex", ""),
        "callsign":     (ac.get("flight") or "").strip(),
        "squawk":       ac.get("squawk", ""),
        "squawk_label": SQUAWK_LABEL.get(ac.get("squawk", ""), ""),
        "registration": ac.get("r") or "-",
        "model":        enrich.get("model") or ac.get("desc") or ac.get("t") or "-",
        "operator":     enrich.get("operator") or "-",
        "lat":          ac.get("lat"),
        "lon":          ac.get("lon"),
        "alt_ft":       alt_ft,
        "alt_m":        alt_m,
        "speed_kt":     round(gs) if isinstance(gs, (int, float)) else None,
        "heading":      round(ac["track"]) if isinstance(ac.get("track"), (int, float)) else None,
        "on_ground":    ac.get("alt_baro") == "ground",
        "orig":         ac.get("orig") or "-",
        "dest":         ac.get("dest") or "-",
    }


# ── Poll loop ─────────────────────────────────────────────────────────────────
def poll_loop() -> None:
    known_emergencies: set[tuple[str, str]] = seed_known_emergencies()
    log.info("PTSquawk watcher started — polling every %ds", POLL_INTERVAL)

    while True:
        try:
            aircraft = fetch_all_aircraft()
            log.info("Fetched %d aircraft total", len(aircraft))

            current_emergency_keys: set[tuple[str, str]] = set()

            for ac in aircraft:
                squawk = ac.get("squawk")
                if squawk not in EMERGENCY_SQUAWKS:
                    continue
                icao = ac.get("hex", "")
                if not icao:
                    continue

                key = (icao, squawk)
                current_emergency_keys.add(key)

                if key not in known_emergencies:
                    known_emergencies.add(key)
                    enrich = enrich_hexdb(icao)
                    event  = build_event(ac, enrich)
                    append_history(event)
                    msg = format_telegram(ac, enrich)
                    send_telegram(msg)
                    log.info(
                        "New emergency: %s squawk %s (%s)",
                        ac.get("flight", icao).strip(), squawk, ac.get("r", "-"),
                    )

            # Clear resolved emergencies so re-entry fires again
            known_emergencies &= current_emergency_keys

            if _HEARTBEAT_AVAILABLE:
                _hb_beat(
                    "PTSquawk",
                    status="ok",
                    note=f"scanned {len(aircraft)} aircraft",
                    next_in_seconds=180,  # poll every 60s, alert after 3 missed cycles
                )

        except Exception as exc:
            log.error("Poll loop error: %s", exc)

        time.sleep(POLL_INTERVAL)


# ── Flask API ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, resources={r"/history": {"origins": "*"}, r"/health": {"origins": "*"}})


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/history")
def history():
    return jsonify(read_history())


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=5000, threaded=True, use_reloader=False),
        daemon=True,
    )
    flask_thread.start()
    log.info("Flask API listening on :5000")
    poll_loop()


if __name__ == "__main__":
    main()
