"""Extract: download price snapshots from the MITECO REST API and keep the raw payload.

Endpoints (no API key):
- current:    {base}/EstacionesTerrestres/            -> prices in force right now
- historical: {base}/EstacionesTerrestresHist/{dd-mm-yyyy} -> prices of that day
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from datetime import date
from pathlib import Path

import requests

from pipeline.config import Settings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ExtractError(RuntimeError):
    """The API could not provide a valid snapshot."""


def current_url(settings: Settings) -> str:
    return f"{settings.api_base_url}/EstacionesTerrestres/"


def historical_url(settings: Settings, day: date) -> str:
    return f"{settings.api_base_url}/EstacionesTerrestresHist/{day:%d-%m-%Y}"


def download(url: str, timeout: int, max_retries: int, backoff: float = 2.0) -> bytes:
    """GET with retries and exponential backoff on network errors and 429/5xx."""
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
        else:
            if resp.ok:
                log.info("Downloaded %s (%.1f MB)", url, len(resp.content) / 1e6)
                return resp.content
            if resp.status_code not in RETRYABLE_STATUS:
                raise ExtractError(f"Non-retryable HTTP {resp.status_code} for {url}")
            last_error = requests.HTTPError(f"HTTP {resp.status_code}")
        if attempt < max_retries:
            wait = backoff**attempt
            log.warning(
                "Attempt %d/%d failed (%s); retrying in %.0fs",
                attempt,
                max_retries,
                last_error,
                wait,
            )
            time.sleep(wait)
    raise ExtractError(f"Giving up on {url} after {max_retries} attempts") from last_error


def parse_payload(raw: bytes) -> dict:
    """Decode and validate the API envelope; raise ExtractError if it is not usable."""
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractError("Response is not valid JSON") from exc
    if payload.get("ResultadoConsulta") != "OK":
        raise ExtractError(f"API returned ResultadoConsulta={payload.get('ResultadoConsulta')!r}")
    stations = payload.get("ListaEESSPrecio")
    if not isinstance(stations, list) or not stations:
        raise ExtractError("API returned no stations")
    return payload


def raw_path(raw_dir: Path, day: date) -> Path:
    return raw_dir / f"{day:%Y-%m-%d}.json.gz"


def save_raw(raw: bytes, path: Path) -> None:
    """Write the untouched response gzip-compressed, atomically (tmp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wb") as fh:
        fh.write(raw)
    tmp.replace(path)
    log.info("Saved raw snapshot %s", path)


def read_raw(path: Path) -> dict:
    with gzip.open(path, "rb") as fh:
        return parse_payload(fh.read())


def extract_day(settings: Settings, day: date, *, historical: bool, force: bool = False) -> dict:
    """Return the payload for `day`, downloading it unless a raw copy already exists.

    Historical snapshots never change, so a cached file is reused. The current snapshot
    is always re-downloaded (prices update every 30 minutes).
    """
    path = raw_path(settings.raw_dir, day)
    if historical and path.exists() and not force:
        log.info("Using cached raw snapshot %s", path)
        return read_raw(path)
    url = historical_url(settings, day) if historical else current_url(settings)
    raw = download(url, settings.api_timeout, settings.api_max_retries)
    payload = parse_payload(raw)  # validate before persisting
    save_raw(raw, path)
    return payload
