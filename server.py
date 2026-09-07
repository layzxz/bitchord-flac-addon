#!/usr/bin/env python3
"""BitChord addon: search Internet Archive for real FLAC files.

Returns actual FLAC files and exposes technical FLAC metadata. BitChord uses
FLAC as LOSSLESS and uses bit depth / sample rate to identify Hi-Res Lossless.
"""

import os
import re
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

MANIFEST = {
    "id": "bitchord-internet-archive-flac",
    "name": "Layz Add On",
    "version": "1.2.4",
    "resources": ["search", "stream"],
}

IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_METADATA = "https://archive.org/metadata/{}"


def ia_search(query: str, rows: int = 20):
    params = {
        "q": f'(mediatype:audio) AND ({query})',
        "fl[]": ["identifier", "title", "creator", "album", "year"],
        "rows": rows,
        "page": 1,
        "output": "json",
    }
    response = requests.get(IA_SEARCH, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def number_value(value):
    if value is None or value == "":
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    number = float(match.group(0))
    return int(number) if number.is_integer() else number


def first_value(item, *keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def flac_streaminfo(url):
    """Read sample rate and bit depth from the first FLAC STREAMINFO block."""
    try:
        response = requests.get(
            url,
            headers={"Range": "bytes=0-63"},
            timeout=15,
            stream=True,
        )
        response.raise_for_status()
        data = next(response.iter_content(64), b"")
        response.close()

        if len(data) < 42 or data[:4] != b"fLaC":
            return None, None

        header = data[4:8]
        block_type = header[0] & 0x7F
        block_length = int.from_bytes(header[1:4], "big")
        if block_type != 0 or block_length < 34:
            return None, None

        info = data[8:42]
        packed = int.from_bytes(info[10:18], "big")
        sample_rate = packed >> 44
        bit_depth = ((packed >> 36) & 0x1F) + 1

        if not (1000 <= sample_rate <= 768000):
            sample_rate = None
        if not (4 <= bit_depth <= 32):
            bit_depth = None
        return sample_rate, bit_depth
    except (requests.RequestException, StopIteration, ValueError):
        return None, None


def quality_label(sample_rate, bit_depth):
    """Return a label BitChord recognizes as its lossless tier."""
    try:
        sr = float(sample_rate) if sample_rate is not None else None
    except (TypeError, ValueError):
        sr = None
    try:
        bd = int(bit_depth) if bit_depth is not None else None
    except (TypeError, ValueError):
        bd = None

    # BitChord's Hi-Res detector is bitDepth > 16 OR sampleRate > 48 kHz.
    # We advertise the same condition so the search row and the actual stream
    # agree with the player.
    if bd is not None and bd > 16:
        return "HI_RES_LOSSLESS"
    if sr is not None and sr > 48000:
        return "HI_RES_LOSSLESS"
    return "LOSSLESS"


def inspect_candidate(identifier, item):
    """Return one FLAC candidate with real technical metadata when possible."""
    filename = str(item.get("name", ""))
    url = f"https://archive.org/download/{quote(identifier, safe='')}/{quote(filename, safe='')}"

    bitrate = number_value(first_value(item, "bitrate", "bit_rate", "bitRate"))
    sample_rate = number_value(first_value(item, "sample_rate", "samplerate", "sampleRate"))
    bit_depth = number_value(first_value(item, "bit_depth", "bitdepth", "bitDepth"))

    if sample_rate is None or bit_depth is None:
        detected_rate, detected_depth = flac_streaminfo(url)
        if sample_rate is None:
            sample_rate = detected_rate
        if bit_depth is None:
            bit_depth = detected_depth

    return {
        "url": url,
        "filename": filename,
        "size": item.get("size"),
        "length": number_value(item.get("length")),
        "bitrate": bitrate,
        "sampleRate": sample_rate,
        "bitDepth": bit_depth,
        "quality": quality_label(sample_rate, bit_depth),
        "source": f"https://archive.org/details/{quote(identifier, safe='')}",
    }


def get_flac(identifier: str):
    response = requests.get(IA_METADATA.format(quote(identifier, safe="")), timeout=20)
    response.raise_for_status()
    data = response.json()

    items = []
    for item in data.get("files", []):
        name = str(item.get("name", ""))
        fmt = str(item.get("format", "")).upper()
        if name.lower().endswith(".flac") and fmt in ("", "FLAC"):
            items.append(item)

    if not items:
        return None

    # Do NOT simply pick the largest FLAC. An IA item can contain several
    # renditions, and the largest file is not guaranteed to be the highest
    # resolution. Inspect the actual FLAC STREAMINFO and prefer the best
    # technical rendition first, then size as a tie-breaker.
    candidates = [inspect_candidate(identifier, item) for item in items]

    def rank(candidate):
        sr = candidate.get("sampleRate") or 0
        bd = candidate.get("bitDepth") or 0
        size = number_value(candidate.get("size")) or 0
        return (
            int(bd > 16),
            bd,
            sr,
            size,
        )

    candidates.sort(key=rank, reverse=True)
    return candidates[0]


def make_track(doc, flac):
    identifier = str(doc["identifier"])
    return {
        "id": identifier,
        "title": str(doc.get("title") or flac["filename"]),
        "artist": str(doc.get("creator") or "Internet Archive"),
        "album": str(doc.get("album") or ""),
        "duration": flac.get("length"),
        "artworkURL": f"https://archive.org/services/img/{quote(identifier, safe='')}",
        "format": "flac",
        "audioQuality": flac.get("quality", "LOSSLESS"),
        "streamURL": flac["url"],
    }


@app.get("/")
def root():
    return jsonify(MANIFEST)


@app.get("/manifest.json")
def manifest():
    return jsonify(MANIFEST)


@app.get("/health")
def health():
    return jsonify({"ok": True, "name": MANIFEST["name"], "version": MANIFEST["version"]})


@app.get("/search")
def search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"tracks": []})
    try:
        limit = min(max(int(request.args.get("limit", "20")), 1), 50)
    except ValueError:
        limit = 20

    try:
        docs = ia_search(query, limit * 2).get("response", {}).get("docs", [])
        tracks = []
        for doc in docs:
            identifier = doc.get("identifier")
            if not identifier:
                continue
            try:
                flac = get_flac(str(identifier))
            except requests.RequestException:
                continue
            if not flac:
                continue
            tracks.append(make_track(doc, flac))
            if len(tracks) >= limit:
                break
        return jsonify({"tracks": tracks})
    except requests.RequestException as exc:
        return jsonify({"tracks": [], "error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"tracks": [], "error": str(exc)}), 500


@app.get("/stream/<path:identifier>")
def stream(identifier):
    identifier = identifier.strip()
    if not identifier:
        return jsonify({"error": "missing id"}), 400
    try:
        flac = get_flac(identifier)
        if not flac:
            return jsonify({"error": "No FLAC file found for this item"}), 404

        result = {
            "url": flac["url"],
            "format": "flac",
            "quality": flac.get("quality", "LOSSLESS"),
            "audioQuality": flac.get("quality", "LOSSLESS"),
            "mimeType": "audio/flac",
            "codec": "flac",
            "fileCodec": "flac",
            "container": "flac",
            "containerFormat": "flac",
            "encrypted": False,
        }
        for key in ("sampleRate", "bitDepth", "bitrate"):
            if flac.get(key) is not None:
                result[key] = flac[key]
        return jsonify(result)
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
