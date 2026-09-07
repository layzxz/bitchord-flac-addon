#!/usr/bin/env python3
"""
BitChord FLAC addon backed by Internet Archive.

Only returns files that Internet Archive identifies as FLAC.
This project is intended for publicly available / appropriately licensed audio.
"""

from flask import Flask, request, jsonify
import requests
from urllib.parse import quote

app = Flask(__name__)

IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_METADATA = "https://archive.org/metadata/{}"

def ia_search(query, rows=25):
    # Search only items whose metadata exposes FLAC files.
    params = {
        "q": f"({query}) AND mediatype:audio AND format:FLAC",
        "fl[]": ["identifier", "title", "creator", "year"],
        "rows": rows,
        "page": 1,
        "output": "json",
    }
    r = requests.get(IA_SEARCH, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def find_flac(identifier):
    r = requests.get(IA_METADATA.format(quote(identifier, safe="")), timeout=15)
    r.raise_for_status()
    data = r.json()
    files = data.get("files", [])

    candidates = []
    for f in files:
        name = f.get("name", "")
        fmt = str(f.get("format", "")).upper()
        if name.lower().endswith(".flac") or fmt == "FLAC":
            # Ignore obvious metadata/checksum files.
            if name.lower().endswith(".flac"):
                candidates.append(f)

    if not candidates:
        return None

    # Prefer the smallest/first FLAC if an item has multiple variants.
    f = candidates[0]
    filename = f["name"]
    direct = f"https://archive.org/download/{quote(identifier, safe='')}/{quote(filename, safe='')}"
    return {
        "url": direct,
        "filename": filename,
        "size": f.get("size"),
        "format": "FLAC",
        "bitrate": f.get("bitrate"),
        "length": f.get("length"),
        "source": f"https://archive.org/details/{quote(identifier, safe='')}",
    }

@app.get("/")
def root():
    return jsonify(manifest)

@app.get("/manifest.json")
def manifest_route():
    return jsonify(manifest)

@app.get("/health")
def health():
    return jsonify({"ok": True, "name": manifest["name"], "version": manifest["version"]})

@app.get("/search")
def search():
    q = (request.args.get("q") or request.args.get("query") or "").strip()
    limit = min(max(int(request.args.get("limit", 20)), 1), 50)
    if not q:
        return jsonify({"tracks": [], "total": 0})

    try:
        data = ia_search(q, limit)
        docs = data.get("response", {}).get("docs", [])
        tracks = []
        for d in docs:
            identifier = d.get("identifier")
            if not identifier:
                continue
            tracks.append({
                "id": identifier,
                "title": d.get("title") or identifier,
                "artist": d.get("creator") or "",
                "album": "",
                "duration": None,
                "artwork": f"https://archive.org/services/img/{quote(identifier, safe='')}",
                "audioQuality": "LOSSLESS",
                "format": "FLAC",
                "source": "Internet Archive",
            })
        return jsonify({"tracks": tracks, "total": len(tracks)})
    except Exception as e:
        return jsonify({"tracks": [], "total": 0, "error": str(e)}), 502

@app.get("/stream")
def stream():
    identifier = (request.args.get("id") or "").strip()
    if not identifier:
        return jsonify({"error": "missing id"}), 400

    try:
        flac = find_flac(identifier)
        if not flac:
            return jsonify({"error": "No FLAC file found for this item"}), 404

        track = {
            "id": identifier,
            "audioQuality": "LOSSLESS",
            "format": "FLAC",
            "mimeType": "audio/flac",
        }
        return jsonify({
            "streamUrl": flac["url"],
            "url": flac["url"],
            "mimeType": "audio/flac",
            "format": "FLAC",
            "quality": "LOSSLESS",
            "track": track,
            "source": flac["source"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
