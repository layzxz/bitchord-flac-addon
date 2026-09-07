#!/usr/bin/env python3
"""BitChord addon: search Internet Archive for real FLAC files.

Returns actual FLAC files and exposes their technical metadata when it can be
read from Internet Archive metadata or the FLAC STREAMINFO block. BitChord
uses FLAC as LOSSLESS and uses the quality label plus sample rate / bit depth
to identify Hi-Res Lossless in the player.
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
    "version": "1.2.3",
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
    """Read FLAC sample rate and bit depth from the STREAMINFO block."""
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

        block_header = data[4:8]
        block_type = block_header[0] & 0x7F
        block_length = int.from_bytes(block_header[1:4], "big")
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
    """Return the quality text BitChord's qualityTier() understands."""
    try:
        sr = float(sample_rate) if sample_rate is not None else None
    except (TypeError, ValueError):
        sr = None
    try:
        bd = int(bit_depth) if bit_depth is not None else None
    except (TypeError, ValueError):
        bd = None

    # BitChord recognizes HI-RES / HI_RES / HIRES as the lossless tier and
    # uses the stream's bit depth/sample rate for the actual Hi-Res display.
    # 24-bit audio is sufficient to advertise Hi-Res; 24/44.1 is still
    # materially higher-resolution than CD 16-bit audio.
    if bd is not None and bd >= 24:
        return "HI_RES_LOSSLESS"
    if sr is not None and sr > 44100 and bd is not None and bd >= 16:
        return "HI_RES_LOSSLESS"
    return "LOSSLESS"


def get_flac(identifier: str):
    response = requests.get(IA_METADATA.format(quote(identifier, safe="")), timeout=20)
    response.raise_for_status()
    data = response.json()

    candidates = []
    for item in data.get("files", []):
        name = str(item.get("name", ""))
        fmt = str(item.get("format", "")).upper()
        if name.lower().endswith(".flac") and fmt in ("", "FLAC"):
            candidates.append(item)

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: (
            number_value(x.get("size")) or 0,
            -int("metadata" in str(x.get("name", "")).lower()),
        ),
        reverse=True,
    )
    item = candidates[0]
    filename = str(item["name"])
    url = f"https://archive.org/download/{quote(identifier, safe='')}/{quote(filename, safe='')}"

    bitrate = number_value(first_value(item, "bitrate", "bit_rate", "bitRate"))
    sample_rate = number_value(
        first_value(item, "sample_rate", "samplerate", "sampleRate")
    )
    bit_depth = number_value(
        first_value(item, "bit_depth", "bitdepth", "bitDepth")
    )

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
        # IMPORTANT: AddonTrack currently reads audioQuality/format for the
        # search-row badge. The numeric sampleRate/bitDepth fields are ignored
        # by that model, so the Hi-Res tier must be stated here as well.
        "audioQuality": flac.get("quality", "LOSSLESS"),
        "streamURL": flac["url"],
    }
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
