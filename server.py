#!/usr/bin/env python3
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

MANIFEST = {
    "id": "bitchord-internet-archive-flac",
    "name": "Layz Add On",
    "version": "1.6.0",
    "resources": ["search", "stream"],
    "settings": [
        {
            "key": "quality",
            "type": "select",
            "default": "lossless",
            "options": [
                {"label": "Lossless", "value": "lossless"},
                {"label": "High", "value": "high"},
                {"label": "Low", "value": "low"},
            ],
        }
    ],
}

IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_METADATA = "https://archive.org/metadata/{}"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Layz-Add-On/1.6 BitChord"})



def num(value):
    if value in (None, ""):
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    number = float(match.group(0))
    return int(number) if number.is_integer() else number



def first(data, *keys):
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    return None



def flac_info(url):
    """Read FLAC STREAMINFO from the first bytes without downloading the file."""
    try:
        response = SESSION.get(
            url,
            headers={"Range": "bytes=0-63"},
            timeout=(5, 12),
            stream=True,
        )
        response.raise_for_status()
        data = response.raw.read(64)
        response.close()

        if len(data) < 26 or data[:4] != b"fLaC":
            return None, None

        block_type = data[4] & 0x7F
        block_length = int.from_bytes(data[5:8], "big")
        if block_type != 0 or block_length < 34 or len(data) < 26:
            return None, None

        packed = int.from_bytes(data[18:26], "big")
        sample_rate = packed >> 44
        bit_depth = ((packed >> 36) & 0x1F) + 1

        if not 1000 <= sample_rate <= 768000:
            sample_rate = None
        if not 4 <= bit_depth <= 32:
            bit_depth = None
        return sample_rate, bit_depth
    except requests.RequestException:
        return None, None



def quality(sr, bd):
    # This addon only returns real FLAC, so its protocol tier is LOSSLESS.
    return "LOSSLESS"



def quality_label(sr, bd):
    details = []
    if bd is not None:
        details.append(f"{bd}-bit")
    if sr is not None:
        details.append(f"{sr / 1000:g} kHz")
    hi_res = (bd is not None and bd > 16) or (sr is not None and sr > 48000)
    label = "Hi-Res Lossless" if hi_res else "Lossless"
    return label + (f" · {' / '.join(details)}" if details else "")



def inspect(identifier, item):
    name = str(item.get("name", ""))
    url = (
        f"https://archive.org/download/{quote(identifier, safe='')}/"
        f"{quote(name, safe='')}"
    )

    sr = num(first(item, "sample_rate", "samplerate", "sampleRate"))
    bd = num(first(item, "bit_depth", "bitdepth", "bitDepth"))

    # Only probe the actual FLAC header when IA metadata does not already tell us.
    if sr is None or bd is None:
        detected_sr, detected_bd = flac_info(url)
        sr = sr if sr is not None else detected_sr
        bd = bd if bd is not None else detected_bd

    return {
        "url": url,
        "filename": name,
        "size": num(item.get("size")),
        "length": num(item.get("length")),
        "bitrate": num(first(item, "bitrate", "bit_rate", "bitRate")),
        "sampleRate": sr,
        "bitDepth": bd,
        "quality": quality(sr, bd),
    }



def get_flac(identifier):
    response = SESSION.get(
        IA_METADATA.format(quote(identifier, safe="")),
        timeout=(5, 20),
    )
    response.raise_for_status()
    data = response.json()

    candidates = []
    for item in data.get("files", []):
        name = str(item.get("name", ""))
        if not name.lower().endswith(".flac"):
            continue
        candidates.append((name, item))

    if not candidates:
        return None

    # Prefer the largest FLAC first, then inspect the best few in parallel.
    candidates.sort(key=lambda pair: num(pair[1].get("size")) or 0, reverse=True)
    candidates = candidates[:12]

    inspected = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(inspect, identifier, item)
            for _, item in candidates
        ]
        for future in as_completed(futures):
            try:
                inspected.append(future.result())
            except Exception:
                pass

    if not inspected:
        return None

    inspected.sort(
        key=lambda item: (
            int((item.get("bitDepth") or 0) > 16),
            item.get("bitDepth") or 0,
            item.get("sampleRate") or 0,
            item.get("size") or 0,
        ),
        reverse=True,
    )
    return inspected[0]



def normalized(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()



def artist_hint(query, title, creator):
    # IA metadata is inconsistent. Keep the real creator when available.
    if isinstance(creator, list):
        return str(creator[0]) if creator else "Internet Archive"
    if creator:
        return str(creator)
    return "Internet Archive"



def make_track(doc, flac):
    identifier = str(doc["identifier"])
    title = str(doc.get("title") or flac["filename"])
    creator = doc.get("creator")
    tier = flac.get("quality", "LOSSLESS")

    return {
        "id": identifier,
        "title": title,
        "artist": artist_hint("", title, creator),
        "album": str(doc.get("album") or ""),
        "duration": flac.get("length"),
        "artworkURL": f"https://archive.org/services/img/{quote(identifier, safe='')}",
        "format": "flac",
        "audioQuality": tier,
        "quality": quality_label(flac.get("sampleRate"), flac.get("bitDepth")),
        "sampleRate": flac.get("sampleRate"),
        "bitDepth": flac.get("bitDepth"),
        "bitrate": flac.get("bitrate"),
        "streamURL": flac["url"],
    }



def do_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"tracks": []})

    # BitChord does not require a limit parameter; it trims the returned rows.
    try:
        limit = min(max(int(request.args.get("limit", "20")), 1), 30)
    except ValueError:
        limit = 20

    try:
        response = SESSION.get(
            IA_SEARCH,
            params={
                "q": f"(mediatype:audio) AND ({query})",
                "fl[]": ["identifier", "title", "creator", "album", "year"],
                "rows": min(limit * 4, 80),
                "page": 1,
                "output": "json",
            },
            timeout=(5, 20),
        )
        response.raise_for_status()
        docs = response.json().get("response", {}).get("docs", [])
    except (requests.RequestException, ValueError) as exc:
        return jsonify({"tracks": [], "error": str(exc)}), 502

    tracks = []
    # Resolve several IA items concurrently so BitChord does not wait through
    # a serial metadata request for every non-FLAC search hit.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}
        for doc in docs:
            identifier = doc.get("identifier")
            if identifier:
                futures[pool.submit(get_flac, str(identifier))] = doc

        for future in as_completed(futures):
            doc = futures[future]
            try:
                flac = future.result()
            except requests.RequestException:
                continue
            except Exception:
                continue
            if not flac:
                continue

            tracks.append(make_track(doc, flac))
            if len(tracks) >= limit:
                break

    return jsonify({"tracks": tracks})



def do_stream(identifier):
    identifier = (identifier or "").strip()
    if not identifier:
        return jsonify({"error": "missing id"}), 400

    try:
        flac = get_flac(identifier)
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502

    if not flac:
        return jsonify({"error": "No FLAC file found for this item"}), 404

    return jsonify({
        "url": flac["url"],
        "format": "flac",
        "quality": quality_label(flac.get("sampleRate"), flac.get("bitDepth")),
        "audioQuality": flac.get("quality", "LOSSLESS"),
        "codec": "flac",
        "container": "flac",
        "manifest": "none",
        "encrypted": False,
        "sampleRate": flac.get("sampleRate"),
        "bitDepth": flac.get("bitDepth"),
        "bitrate": flac.get("bitrate"),
        "audioMode": "stereo",
    })



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
    return do_search()


@app.get("/stream/<path:identifier>")
def stream(identifier):
    return do_stream(identifier)


# Compatibility with older addon examples that used /stream?id=...
@app.get("/stream")
def stream_query():
    return do_stream(request.args.get("id"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
