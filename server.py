#!/usr/bin/env python3
import os
import re
from urllib.parse import quote
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
MANIFEST = {
    "id": "bitchord-internet-archive-flac",
    "name": "Layz Add On",
    "version": "1.4.0",
    "resources": ["search", "stream"],
}
IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_METADATA = "https://archive.org/metadata/{}"


def num(v):
    if v in (None, ""):
        return None
    m = re.search(r"\d+(?:\.\d+)?", str(v))
    if not m:
        return None
    x = float(m.group(0))
    return int(x) if x.is_integer() else x


def first(d, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def flac_info(url):
    try:
        r = requests.get(
            url,
            headers={"Range": "bytes=0-63"},
            timeout=15,
            stream=True,
        )
        r.raise_for_status()
        b = next(r.iter_content(64), b"")
        r.close()
        if len(b) < 42 or b[:4] != b"fLaC":
            return None, None
        h = b[4:8]
        if (h[0] & 0x7f) != 0 or int.from_bytes(h[1:4], "big") < 34:
            return None, None
        p = int.from_bytes(b[18:26], "big")
        sr = p >> 44
        bd = ((p >> 36) & 0x1f) + 1
        return (
            sr if 1000 <= sr <= 768000 else None,
            bd if 4 <= bd <= 32 else None,
        )
    except (requests.RequestException, StopIteration, ValueError):
        return None, None


def quality(sr, bd):
    try:
        sr = float(sr) if sr is not None else None
    except Exception:
        sr = None
    try:
        bd = int(bd) if bd is not None else None
    except Exception:
        bd = None
    return (
        "HI_RES_LOSSLESS"
        if (bd is not None and bd > 16) or (sr is not None and sr > 48000)
        else "LOSSLESS"
    )


def inspect(identifier, item):
    name = str(item.get("name", ""))
    url = (
        f"https://archive.org/download/{quote(identifier, safe='')}/"
        f"{quote(name, safe='')}"
    )
    sr = num(first(item, "sample_rate", "samplerate", "sampleRate"))
    bd = num(first(item, "bit_depth", "bitdepth", "bitDepth"))
    if sr is None or bd is None:
        ds, dd = flac_info(url)
        sr = sr if sr is not None else ds
        bd = bd if bd is not None else dd
    return {
        "url": url,
        "filename": name,
        "size": item.get("size"),
        "length": num(item.get("length")),
        "bitrate": num(first(item, "bitrate", "bit_rate", "bitRate")),
        "sampleRate": sr,
        "bitDepth": bd,
        "quality": quality(sr, bd),
    }


def get_flac(identifier):
    r = requests.get(
        IA_METADATA.format(quote(identifier, safe="")),
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    candidates = []
    for item in data.get("files", []):
        name = str(item.get("name", ""))
        fmt = str(item.get("format", "")).upper()
        if name.lower().endswith(".flac") and fmt in ("", "FLAC"):
            candidates.append(inspect(identifier, item))
    if not candidates:
        return None
    candidates.sort(
        key=lambda x: (
            int((x.get("bitDepth") or 0) > 16),
            x.get("bitDepth") or 0,
            x.get("sampleRate") or 0,
            num(x.get("size")) or 0,
        ),
        reverse=True,
    )
    return candidates[0]


def normalized(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def artist_hint(query, title, creator):
    q = normalized(query)
    t = normalized(title)
    if q and t:
        remaining = q
        for token in t.split():
            remaining = re.sub(r"\b" + re.escape(token) + r"\b", " ", remaining)
        remaining = re.sub(r"\s+", " ", remaining).strip()
        if remaining:
            return remaining
    if isinstance(creator, list):
        return str(creator[0]) if creator else "Internet Archive"
    return str(creator or "Internet Archive")


def make_track(doc, flac, query):
    title = str(doc.get("title") or flac["filename"])
    creator = doc.get("creator")
    q = flac.get("quality", "LOSSLESS")
    track = {
        "id": str(doc["identifier"]),
        "title": title,
        "artist": artist_hint(query, title, creator),
        "album": str(doc.get("album") or ""),
        "duration": flac.get("length"),
        "artworkURL": "https://archive.org/services/img/" + quote(str(doc["identifier"]), safe=""),
        "format": "flac",
        "quality": q,
        "audioQuality": q,
        "streamQuality": q,
        "codec": "flac",
        "fileCodec": "flac",
        "container": "flac",
        "containerFormat": "flac",
        "mimeType": "audio/flac",
        "mediaType": "audio/flac",
        "sampleRate": flac.get("sampleRate"),
        "bitDepth": flac.get("bitDepth"),
        "bitrate": flac.get("bitrate"),
        "streamURL": flac["url"],
    }
    return track


def manifest_for_mode(mode=None):
    if not mode:
        return MANIFEST
    result = dict(MANIFEST)
    result["mode"] = mode
    return result


def do_search():
    query = (request.args.get("q") or request.args.get("query") or "").strip()
    if not query:
        return jsonify({"tracks": []})
    try:
        limit = min(max(int(request.args.get("limit", "20")), 1), 50)
    except ValueError:
        limit = 20
    try:
        docs = requests.get(
            IA_SEARCH,
            params={
                "q": f"(mediatype:audio) AND ({query})",
                "fl[]": ["identifier", "title", "creator", "album", "year"],
                "rows": limit * 2,
                "page": 1,
                "output": "json",
            },
            timeout=20,
        ).json().get("response", {}).get("docs", [])
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
            tracks.append(make_track(doc, flac, query))
            if len(tracks) >= limit:
                break
        return jsonify({"tracks": tracks})
    except requests.RequestException as e:
        return jsonify({"tracks": [], "error": str(e)}), 502
    except Exception as e:
        return jsonify({"tracks": [], "error": str(e)}), 500


def do_stream(identifier):
    identifier = (identifier or "").strip()
    if not identifier:
        return jsonify({"error": "missing id"}), 400
    try:
        flac = get_flac(identifier)
        if not flac:
            return jsonify({"error": "No FLAC file found for this item"}), 404
        q = flac.get("quality", "LOSSLESS")
        result = {
            "url": flac["url"],
            "format": "flac",
            "quality": q,
            "streamQuality": q,
            "audioQuality": q,
            "codec": "flac",
            "fileCodec": "flac",
            "container": "flac",
            "containerFormat": "flac",
            "mimeType": "audio/flac",
            "mediaType": "audio/flac",
            "encrypted": False,
            "sampleRate": flac.get("sampleRate"),
            "bitDepth": flac.get("bitDepth"),
            "bitrate": flac.get("bitrate"),
            "audioMode": "stereo",
        }
        return jsonify(result)
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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


@app.get("/<mode>/manifest.json")
def mode_manifest(mode):
    return jsonify(manifest_for_mode(mode))


@app.get("/<mode>/search")
def mode_search(mode):
    return do_search()


@app.get("/<mode>/stream/<path:identifier>")
def mode_stream(mode, identifier):
    return do_stream(identifier)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
