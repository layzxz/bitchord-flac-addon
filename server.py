#!/usr/bin/env python3
"""BitChord addon: search Internet Archive for real FLAC files."""

import os
import re
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

MANIFEST = {
    "id": "bitchord-internet-archive-flac",
    "name": "Internet Archive FLAC",
    "version": "1.2.0",
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
    """Return a numeric metadata value when IA provides one."""
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


def get_flac(identifier: str):
    response = requests.get(
        IA_METADATA.format(quote(identifier, safe="")), timeout=20
    )
    response.raise_for_status()
    data = response.json()

    candidates = []
    for item in data.get("files", []):
        name = str(item.get("name", ""))
        fmt = str(item.get("format", "")).upper()
        # Only accept an actual .flac file. Never convert another format.
        if name.lower().endswith(".flac") and (fmt in ("", "FLAC")):
            candidates.append(item)

    if not candidates:
        return None

    # Prefer a normal audio FLAC over files in obvious metadata directories.
    candidates.sort(
        key=lambda x: (
            "metadata" in str(x.get("name", "")).lower(),
            str(x.get("name", "")),
        )
    )
    item = candidates[0]
    filename = str(item["name"])

    sample_rate = number_value(
        first_value(item, "sample_rate", "samplerate", "sampleRate")
    )
    bit_depth = number_value(
        first_value(item, "bit_depth", "bitdepth", "bitDepth")
    )
    bitrate = number_value(first_value(item, "bitrate", "bit_rate", "bitRate"))
    length = number_value(item.get("length"))

    return {
        "url": (
            f"https://archive.org/download/{quote(identifier, safe='')}/"
            f"{quote(filename, safe='')}"
        ),
        "filename": filename,
        "size": item.get("size"),
        "length": length,
        "bitrate": bitrate,
        "sampleRate": sample_rate,
        "bitDepth": bit_depth,
        "source": f"https://archive.org/details/{quote(identifier, safe='')}",
    }


def make_track(doc, flac):
    identifier = str(doc["identifier"])
    track = {
        "id": identifier,
        "title": str(doc.get("title") or flac["filename"]),
        "artist": str(doc.get("creator") or "Internet Archive"),
        "album": str(doc.get("album") or ""),
        "duration": flac.get("length"),
        "artworkURL": f"https://archive.org/services/img/{quote(identifier, safe='')}",
        "format": "flac",
        "audioQuality": "LOSSLESS",
        "streamURL": flac["url"],
    }

    # Include technical metadata when Internet Archive provides it.
    if flac.get("sampleRate") is not None:
        track["sampleRate"] = flac["sampleRate"]
    if flac.get("bitDepth") is not None:
        track["bitDepth"] = flac["bitDepth"]
    if flac.get("bitrate") is not None:
        track["bitrate"] = flac["bitrate"]

    return track


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

        # Verify every returned item has a real FLAC file before exposing it.
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
            "quality": "LOSSLESS",
            "audioQuality": "LOSSLESS",
            "mimeType": "audio/flac",
            "codec": "flac",
            "fileCodec": "flac",
            "container": "flac",
            "containerFormat": "flac",
            "encrypted": False,
        }

        # Give BitChord the actual technical metadata when available.
        if flac.get("sampleRate") is not None:
            result["sampleRate"] = flac["sampleRate"]
        if flac.get("bitDepth") is not None:
            result["bitDepth"] = flac["bitDepth"]
        if flac.get("bitrate") is not None:
            result["bitrate"] = flac["bitrate"]

        return jsonify(result)
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
