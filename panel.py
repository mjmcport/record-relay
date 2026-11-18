"""Serve the local Record Relay control panel on loopback."""
import argparse
from ipaddress import ip_address, ip_network
import base64
import json
import subprocess
from threading import Lock
from tempfile import NamedTemporaryFile, TemporaryDirectory, gettempdir
import wave
from zipfile import ZipFile

from export_track import export_track
from waveform import waveform_peaks
from html import escape
import os
import re
import shutil
import stat
from urllib.parse import parse_qs, quote, unquote, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import socket
from time import monotonic
from album_service import AlbumService
from catalog import Catalog, Conflict, CatalogError
from release_catalog import RevisionConflict


def forward(path, packet):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(10)
        connection.connect(str(path))
        connection.sendall(packet + b"\n")
        return connection.makefile().readline(64_000).encode()


def panel_handler(path, port, library=None, *, host="127.0.0.1", allowed_network=None,
                  album_service=None, acoustid_client_key=None, fpcalc="fpcalc"):
    address = f"{host}:{port}"
    token = secrets.token_urlsafe(32)
    directory = Path(__file__).parent
    link = ('<a href="/recordings/">Download recordings</a> · '
            '<a href="/album/">Album workbench</a> · ' if library is not None else "")
    html = (directory / "panel.html").read_text().replace("__CONTROL_TOKEN__", token).replace("__RECORDINGS_LINK__", link)
    pages = {
        "/": ("text/html; charset=utf-8", html.encode()),
        "/review/": ("text/html; charset=utf-8", (directory / "review/index.html").read_text().replace("__REVIEW_TOKEN__", token).encode()),
        "/review/review.mjs": ("text/javascript; charset=utf-8", (directory / "review/review.mjs").read_bytes()),
        "/review/splits.mjs": ("text/javascript; charset=utf-8", (directory / "review/splits.mjs").read_bytes()),
        "/album/": ("text/html; charset=utf-8", (directory / "ui/album.html").read_text().replace("__CONTROL_TOKEN__", token).encode()),
        "/album/album.mjs": ("text/javascript; charset=utf-8", (directory / "ui/album.mjs").read_bytes()),
    }

    analysis_lock = Lock()
    fingerprint_lock = Lock()
    last_fingerprint_request = [0.0]

    def fingerprint_rate_available():
        with fingerprint_lock:
            now = monotonic()
            if now - last_fingerprint_request[0] < 1 / 3:
                return False
            last_fingerprint_request[0] = now
            return True

    def analyze(source):
        directory = Path(gettempdir()) / "waveforms"
        directory.mkdir(mode=0o700, exist_ok=True)
        index = directory / (source.name + ".json")
        info = source.stat()
        key = [2, info.st_size, info.st_mtime_ns]
        with analysis_lock:
            if index.is_file() and not index.is_symlink() and index.stat().st_size < 512_000:
                try:
                    cached = json.loads(index.read_bytes())
                    if cached["key"] == key:
                        return cached["waveform"]
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            with wave.open(str(source), "rb") as recording:
                waveform = waveform_peaks(recording, 1000, suggest=True)
            with NamedTemporaryFile(dir=directory, delete=False) as temporary:
                pending = Path(temporary.name)
                try:
                    temporary.write(json.dumps({"key": key, "waveform": waveform}).encode())
                    temporary.close()
                    pending.replace(index)
                finally:
                    pending.unlink(missing_ok=True)
            return waveform

    def recording_name(name):
        return re.fullmatch(r"[a-zA-Z0-9_. -]{1,120}\.wav", name, re.IGNORECASE) and not name.startswith(".")

    def recorded_matches(source):
        sidecar = source.with_suffix(".matches.json")
        if sidecar.is_file() and not sidecar.is_symlink() and sidecar.stat().st_size <= 512_000:
            try:
                value = json.loads(sidecar.read_bytes())
                if isinstance(value, list):
                    return [cue for cue in value if isinstance(cue, dict)
                            and type(cue.get("frame")) is int and isinstance(cue.get("tags"), dict)]
            except (OSError, ValueError):
                pass
        return []

    class Handler(BaseHTTPRequestHandler):
        def recordings(self):
            entries = []
            for file in sorted(library.iterdir()):
                if recording_name(file.name) and file.is_file() and not file.is_symlink():
                    name = escape(file.name, quote=True)
                    cues = recorded_matches(file)
                    tags = cues[0]["tags"] if cues else {}
                    label = " — ".join(str(tags.get(field, "")).strip() for field in ("artist", "album")
                                       if tags.get(field))
                    detail = f"<strong>{escape(label)}</strong> · " if label else ""
                    entries.append(f'<li>{detail}<a href="/review/?side={quote(file.name)}">Review {name}</a> · <button name="name" value="{name}">Download {name}</button></li>')
            content = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Record Relay · Recordings</title><main><h1>Recordings</h1>
<p><a href="/">Capture controls</a> · <a href="/review/">Review track splits</a></p>
<form action="/recordings/download" method="post">
<input name="token" type="hidden" value="{token}">
<ul>{''.join(entries) or '<li>No finished recordings yet.</li>'}</ul></form></main></html>"""
            self.send_body(200, "text/html; charset=utf-8", content.encode())

        def download(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise ValueError
                form = parse_qs(self.rfile.read(length).decode(), strict_parsing=True, max_num_fields=2)
                name, = form["name"]
                supplied_token, = form["token"]
                if not secrets.compare_digest(supplied_token, token):
                    self.send_error(403)
                    return
                if not recording_name(name):
                    raise ValueError
                fd = os.open(library / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except (KeyError, ValueError, TypeError, OSError):
                self.send_error(404)
                return
            with os.fdopen(fd, "rb") as recording:
                info = os.fstat(recording.fileno())
                if not stat.S_ISREG(info.st_mode):
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(info.st_size))
                self.end_headers()
                shutil.copyfileobj(recording, self.wfile)

        def audio_preview(self, name):
            cookie = self.headers.get("Cookie", "")
            if (f"rr_preview={token}" not in cookie.split("; ")
                    or self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none")
                    or not recording_name(name)):
                self.send_error(403)
                return
            try:
                fd = os.open(library / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                self.send_error(404)
                return
            with os.fdopen(fd, "rb") as audio:
                info = os.fstat(audio.fileno())
                if not stat.S_ISREG(info.st_mode):
                    self.send_error(404)
                    return
                size = info.st_size
                start, end = 0, size - 1
                request = self.headers.get("Range")
                if request:
                    match = re.fullmatch(r"bytes=(\d{0,18})-(\d{0,18})", request)
                    if match and match[1]:
                        start = int(match[1])
                        end = min(end, int(match[2])) if match[2] else end
                    elif match and match[2]:
                        start = max(0, size - int(match[2]))
                    if not match or not any(match.groups()) or not 0 <= start <= end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                self.send_response(206 if request else 200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                if request:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                remaining = end - start + 1
                self.send_header("Content-Length", str(remaining))
                self.end_headers()
                audio.seek(start)
                try:
                    while remaining and (block := audio.read(min(remaining, 65536))):
                        self.wfile.write(block)
                        remaining -= len(block)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def review_action(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8_100_000:
                    raise ValueError("Request exceeds the supported size.")
                self.connection.settimeout(15)
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("Expected a review request object.")
                name = data["name"]
                if not recording_name(name):
                    raise ValueError("Select a saved side WAV.")
                source = library / name
                if source.is_symlink() or not source.is_file():
                    raise ValueError("Saved side could not be opened.")
                if self.path == "/recordings/draft":
                    if album_service is not None:
                        catalog = album_service.catalog
                        take = catalog.take_for_locator(name)
                        if take is None:
                            legacy_project = catalog.get_or_create_project(
                                "00000000-0000-4000-8000-000000000001", "Imported recordings")
                            take = catalog.import_legacy_wav(source, project_id=legacy_project["id"])
                        if "draft" in data:
                            result = catalog.save_review_draft(take["id"], data["draft"])
                        else:
                            result = catalog.load_review_draft(take["id"])
                            # Import only legacy drafts that already prove their
                            # master identity. Filename-only drafts stay untouched.
                            if not result:
                                old_draft = Path(gettempdir()) / "review-drafts" / (name + ".json")
                                if old_draft.is_file() and not old_draft.is_symlink():
                                    legacy = json.loads(old_draft.read_bytes())
                                    identity = legacy.get("master_sha256") or legacy.get("sha256") if isinstance(legacy, dict) else None
                                    if identity == take.get("master_sha256"):
                                        catalog.save_review_draft(take["id"], legacy)
                                        result = catalog.load_review_draft(take["id"])
                    else:
                        drafts = Path(gettempdir()) / "review-drafts"
                        drafts.mkdir(mode=0o700, exist_ok=True)
                        draft = drafts / (name + ".json")
                        if "draft" in data:
                            payload = data["draft"]
                            if not isinstance(payload, dict):
                                raise ValueError("Invalid review draft.")
                            with NamedTemporaryFile(dir=drafts, delete=False) as temporary:
                                pending = Path(temporary.name)
                                try:
                                    temporary.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                                    temporary.close()
                                    pending.replace(draft)
                                finally:
                                    pending.unlink(missing_ok=True)
                            result = {"saved": True}
                        else:
                            result = (json.loads(draft.read_bytes()) if draft.is_file()
                                      and not draft.is_symlink() and draft.stat().st_size <= 8_100_000 else {})
                    self.send_body(200, "application/json", json.dumps(result).encode())
                    return
                if self.path == "/recordings/waveform":
                    result = analyze(source)
                    result["matches"] = recorded_matches(source)
                    self.send_body(200, "application/json", json.dumps(result).encode())
                    return
                if self.path == "/recordings/fingerprint":
                    if not acoustid_client_key:
                        raise ValueError("AcoustID is not configured. Lookup requires an application key.")
                    if data.get("noncommercial_ack") is not True:
                        raise ValueError("Confirm the non-commercial AcoustID lookup before sending a fingerprint.")
                    if not fingerprint_rate_available():
                        self.send_body(429, "application/json",
                                       b'{"error":"AcoustID lookup is limited to three requests per second."}')
                        return
                    from fingerprinting import identify_saved_segment
                    result = identify_saved_segment(
                        source, data.get("start"), data.get("end"), acoustid_client_key,
                        fpcalc=fpcalc)
                    self.send_body(200, "application/json", json.dumps(result).encode())
                    return
                batch = self.path == "/recordings/export-side"
                selections = data["tracks"] if batch else [data]
                if not isinstance(selections, list) or not 1 <= len(selections) <= 100:
                    raise ValueError("Choose up to 100 tracks.")
                tracks = []
                previous_end = 0
                for index, entry in enumerate(selections):
                    tags = {key: entry.get(key, "") for key in
                            ("title", "artist", "album", "album_artist", "track")}
                    if any(not isinstance(value, str) or len(value) > 300 or "\0" in value
                           for value in tags.values()):
                        raise ValueError("Tags must be text of no more than 300 characters.")
                    start, end = entry["start"], entry["end"]
                    if type(start) is not int or type(end) is not int or not previous_end <= start < end:
                        raise ValueError("Choose ordered, non-overlapping sample ranges.")
                    previous_end = end
                    label = " - ".join(tags[key] for key in ("artist", "title") if tags[key])
                    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", label).strip(" .")
                    tracks.append((start, end, tags, f"{index + 1:02d} - {cleaned[:100] or 'Track'}"))
                with TemporaryDirectory(prefix="record-relay-export-") as scratch:
                    format = data.get("format", "flac")
                    mime = {"flac": "audio/flac", "mp3": "audio/mpeg", "wav": "audio/wav"}
                    if format not in mime:
                        raise ValueError("Choose a supported export format.")
                    cover = None
                    if image := data.get("cover"):
                        suffix = image["format"]
                        if suffix not in ("png", "jpeg"):
                            raise ValueError("Choose a PNG or JPEG cover.")
                        content = base64.b64decode(image["data"], validate=True)
                        if len(content) > 6_000_000 or not content.startswith(
                                b"\x89PNG\r\n\x1a\n" if suffix == "png" else b"\xff\xd8\xff"):
                            raise ValueError("Cover format is invalid or exceeds 6 MB.")
                        cover = Path(scratch) / ("cover." + suffix)
                        cover.write_bytes(content)
                    output = Path(scratch) / ("track." + format)
                    content_type = mime[format]
                    if batch:
                        if shutil.disk_usage(scratch).free < source.stat().st_size * 3:
                            raise ValueError("There is insufficient scratch space.")
                        bundle = Path(scratch) / "side.zip"
                        with ZipFile(bundle, "w") as archive:
                            for start, end, tags, name in tracks:
                                export_track(source, output, start, end, tags, cover=cover)
                                archive.write(output, name + "." + format)
                                output.unlink()
                        output, content_type = bundle, "application/zip"
                    else:
                        start, end, tags, _ = tracks[0]
                        export_track(source, output, start, end, tags, cover=cover)
                    with output.open("rb") as result:
                        self.send_response(200)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Length", str(output.stat().st_size))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        shutil.copyfileobj(result, self.wfile)
            except (KeyError, ValueError, TypeError, AttributeError, OSError, wave.Error, subprocess.SubprocessError):
                self.send_body(400, "application/json", b'{"error":"Could not process the saved side or export. Check the split, tags, cover and FFmpeg installation."}')

        def end_headers(self):
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def send_body(self, status, content_type, content, *, preview_cookie=False):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            if preview_cookie:
                self.send_header("Set-Cookie", f"rr_preview={token}; Path=/recordings/audio; HttpOnly; SameSite=Strict")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def allowed_client(self):
            return allowed_network is None or ip_address(self.client_address[0]) in allowed_network

        def do_GET(self):
            if not self.allowed_client() or self.headers.get("Host") != address:
                self.send_error(404)
                return
            if self.path == "/recordings/" and library is not None:
                self.recordings()
                return
            url = urlsplit(self.path)
            route = url.path
            if album_service is not None and route == "/api/album":
                project_id = parse_qs(url.query).get("project_id", [None])[0]
                self.send_body(200, "application/json", json.dumps(album_service.bootstrap(project_id)).encode())
                return
            if album_service is not None and route == "/api/release-pin":
                project_id = parse_qs(url.query).get("project_id", [""])[0]
                project = album_service.catalog.get_project(project_id)
                if project is None:
                    self.send_error(404)
                else:
                    pin = album_service.releases.get_pin(project_id)
                    self.send_body(200, "application/json", json.dumps(pin or {"revision": 0, "release": None}).encode())
                return
            if route == "/recordings/audio" and library is not None:
                self.audio_preview(parse_qs(url.query).get("side", [""])[0])
                return
            if route not in pages:
                self.send_error(404)
                return
            self.send_body(200, *pages[route], preview_cookie=route == "/review/")

        def do_POST(self):
            if (not self.allowed_client() or self.headers.get("Host") != address
                    or self.headers.get("Origin") != f"http://{address}"):
                self.send_error(403)
                return
            if self.path == "/recordings/download" and library is not None:
                self.download()
                return
            if not secrets.compare_digest(self.headers.get("X-Control-Token", ""), token):
                self.send_error(403)
                return
            if library is not None and self.path in (
                    "/recordings/waveform", "/recordings/export", "/recordings/export-side",
                    "/recordings/draft", "/recordings/fingerprint"):
                self.review_action()
                return
            if self.path != "/control":
                if album_service is None or not self.path.startswith("/api/"):
                    self.send_error(403)
                    return
                self.album_action()
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size < 4096:
                    raise ValueError
                self.connection.settimeout(15)
                response = forward(path, self.rfile.read(size))
                if not response:
                    raise OSError
            except (ValueError, OSError):
                self.send_body(503, "application/json", b'{"error":"Could not reach the capture session."}')
                return
            self.send_body(200, "application/json", response)

        def album_action(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16_384:
                    raise ValueError("Request exceeds the supported size")
                self.connection.settimeout(15)
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("Expected a JSON object")
                route = urlsplit(self.path).path
                if route == "/api/projects":
                    result, status = album_service.create_project(data), 201
                elif route.startswith("/api/projects/"):
                    project_id = unquote(route.removeprefix("/api/projects/"))
                    result, status = album_service.edit_project(project_id, data), 200
                elif route == "/api/releases/search":
                    result, status = album_service.search_releases(data), 200
                elif route == "/api/releases/preview":
                    result, status = album_service.preview_release(data), 200
                elif route == "/api/releases/pin":
                    result, status = album_service.pin_release(data), 200
                elif route == "/api/takes/start":
                    result, status = album_service.start_take(data), 201
                elif route == "/api/takes/stop":
                    result, status = album_service.stop_take(data), 200
                else:
                    self.send_error(404)
                    return
                self.send_body(status, "application/json", json.dumps(result, ensure_ascii=False).encode())
            except (KeyError, ValueError, TypeError, AttributeError, CatalogError, RevisionConflict) as error:
                code = 409 if isinstance(error, (Conflict, RevisionConflict)) else 400
                self.send_body(code, "application/json", json.dumps({"error": str(error)}).encode())
            except (OSError, RuntimeError) as error:
                self.send_body(503, "application/json", json.dumps({"error": str(error)}).encode())

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket", type=Path)
    parser.add_argument("--port", type=int, default=8766, choices=range(1024, 65536), metavar="PORT")
    parser.add_argument("--library", type=Path, help="Directory containing finished WAV recordings")
    parser.add_argument("--state-dir", type=Path, help="Private persistent state directory (defaults inside the library)")
    parser.add_argument("--listen", default="127.0.0.1", help="Explicit IPv4 listening address")
    parser.add_argument("--allow-network", help="Trusted private subnet; every client is treated as an operator (no per-user authentication)")
    parser.add_argument("--acoustid-client-key-file", type=Path,
                        help="Private file containing an AcoustID application key (lookup only)")
    args = parser.parse_args()
    address = ip_address(args.listen)
    network = ip_network(args.allow_network) if args.allow_network else None
    if (address.version != 4 or address.is_unspecified or address.is_multicast
            or (not address.is_loopback and (
                network is None or network.version != 4 or not network.is_private
                or address not in network or not address.is_private))):
        parser.error("LAN access needs a specific private IPv4 address and its private subnet.")
    if args.library is not None and (args.library.is_symlink() or not args.library.is_dir()):
        parser.error("Provide a real recording directory")
    album_service = None
    acoustid_client_key = None
    if args.acoustid_client_key_file:
        secret_path = args.acoustid_client_key_file.expanduser()
        secret_info = secret_path.lstat()
        parent_info = secret_path.parent.stat()
        if (secret_path.is_symlink() or not secret_path.is_file()
                or secret_info.st_uid != os.getuid() or stat.S_IMODE(secret_info.st_mode) & 0o077
                or parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077):
            parser.error("AcoustID key must be an owner-only file inside an owner-only directory")
        acoustid_client_key = secret_path.read_text().strip()
    if args.library is not None:
        state_dir = args.state_dir or (args.library / ".record-relay")
        if state_dir.is_symlink():
            parser.error("The private state directory may not be a symbolic link")
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        details = state_dir.stat()
        if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
            parser.error("The private state directory must be owned by this user and mode 0700")
        database = state_dir / "catalog.sqlite"
        if any(path.is_symlink() for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm"))):
            parser.error("The private catalog database and SQLite sidecars may not be symbolic links")
        catalog = Catalog(database, args.library)
        session_available = False
        live_request_ids = set()
        try:
            status = json.loads(forward(args.socket, b'{"action":"status"}'))
            session_available = isinstance(status, dict) and "error" not in status
            if session_available and isinstance(status.get("recording_id"), str):
                live_request_ids.add(status["recording_id"])
        except (OSError, ValueError, TypeError):
            pass
        catalog.reconcile(live_request_ids=live_request_ids,
                          defer_unverified_writing=not session_available)
        album_service = AlbumService(catalog, args.socket)
    with ThreadingHTTPServer((args.listen, args.port), panel_handler(
            args.socket, args.port, args.library, host=args.listen, allowed_network=network,
            album_service=album_service, acoustid_client_key=acoustid_client_key)) as server:
        server.serve_forever()
