# BitChord FLAC Addon — Internet Archive

A small self-hosted HTTP addon that searches Internet Archive for audio items
with FLAC files and returns direct HTTPS FLAC URLs.

## Important

This is a **real FLAC-source project**, not a lossy-to-FLAC converter.
It only selects files whose Internet Archive metadata/file name indicates FLAC.

It does **not** magically turn YouTube Music/AAC/MP3 into true lossless audio.

Use it with public-domain or appropriately licensed audio.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

Then test:

- `http://127.0.0.1:8080/manifest.json`
- `http://127.0.0.1:8080/health`
- `http://127.0.0.1:8080/search?q=classical`
- `http://127.0.0.1:8080/stream?id=ITEM_IDENTIFIER`

For Android/BitChord, the server must be reachable from the phone.
A public HTTPS deployment is recommended.

## BitChord

BitChord's current releases describe custom addons as user-added HTTP source
modules and the Sources screen accepts the addon's root URL or `manifest.json`.

Paste either:

```text
https://YOUR-DOMAIN/manifest.json
```

or:

```text
https://YOUR-DOMAIN/
```

## Deployment

This project is intentionally simple so it can be deployed to most Python
web hosts. Use:

```bash
gunicorn --bind 0.0.0.0:$PORT server:app
```

## API

### Search

`GET /search?q=<query>&limit=20`

Returns:

```json
{
  "tracks": [
    {
      "id": "archive_identifier",
      "title": "Example",
      "artist": "Example Artist",
      "audioQuality": "LOSSLESS",
      "format": "FLAC"
    }
  ],
  "total": 1
}
```

### Stream

`GET /stream?id=<archive_identifier>`

Returns a direct `audio/flac` URL.

## Compatibility note

BitChord's public repository documents the custom-addon feature, but its
private/runtime HTTP contract is not separately documented as a stable public
API. Therefore this project exposes both a root manifest and `/manifest.json`,
plus conventional `/search` and `/stream` endpoints. If BitChord's Test button
reports a schema mismatch, the response shown by Test should be used to adjust
the manifest fields to the exact build you're running.
