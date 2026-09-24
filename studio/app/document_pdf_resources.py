from __future__ import annotations

import hashlib
import json
import hmac
import mimetypes
import secrets
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, Iterator

from .document_storage import MAX_BYTES, confined
from .document_types import Document, DocumentError, JsonMap, mapping, sequence, text
from .equation_bridge import CONVERTER_VERSION

WEB: Final = Path(__file__).resolve().parents[1] / 'web'
EDGE: Final = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


def resource_snapshot() -> tuple[JsonMap, dict[str, bytes]]:
    manifest_bytes = (WEB / 'dist/document/manifest.json').read_bytes()
    if len(manifest_bytes) > MAX_BYTES:
        raise DocumentError('document_limit', 'Manifest exceeds 20 MiB')
    manifest = mapping(json.loads(manifest_bytes), 'manifest')
    files = {'/styles.css': (WEB / 'styles.css').read_bytes(), '/document.css': (WEB / 'document.css').read_bytes(),
             '/document-print.css': (WEB / 'document-print.css').read_bytes(),
             '/document-print.html': (WEB / 'document-print.html').read_bytes(), '/dist/document/manifest.json': manifest_bytes}
    for entry in sequence(manifest['files'], 'files'):
        item = mapping(entry, 'file')
        relative = text(item['path'], 'path')
        if not (relative.endswith(('.js', '.css', '.woff', '.woff2', '.ttf'))):
            continue
        data = confined(WEB / 'dist/document', relative).read_bytes()
        if hashlib.sha256(data).hexdigest() != item['sha256']:
            raise DocumentError('document_bundle_changed', 'Rebuild the local document assets before printing')
        files[f'/dist/document/{relative}'] = data
    hashes = {key: hashlib.sha256(data).hexdigest() for key, data in files.items()}
    hashes['printHtml'] = hashes['/document-print.html']
    hashes['helper'] = hashlib.sha256((WEB / 'scripts/document-pdf.mjs').read_bytes() + (WEB / 'scripts/document-export.mjs').read_bytes()).hexdigest()
    hashes['packageLock'] = hashlib.sha256((WEB / 'package-lock.json').read_bytes()).hexdigest()
    with EDGE.open('rb') as stream:
        hashes['edge'] = hashlib.file_digest(stream, 'sha256').hexdigest()
    version = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
        "(Get-Item -LiteralPath 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe').VersionInfo.FileVersion"],
        capture_output=True, text=True, timeout=10, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    hashes['edgeFileVersion'] = version.stdout.strip()
    hashes['converter'] = CONVERTER_VERSION
    return hashes, files


@contextmanager
def print_surface(root: Path, current: Document, files: dict[str, bytes]) -> Iterator[tuple[int, str, list[str]]]:
    token = secrets.token_hex(16)
    entries = dict(files)
    entries[f'/document-print/{token}'] = entries.pop('/document-print.html')
    for asset in current['assets']:
        entries[f"/print-assets/{asset['id']}"] = confined(root, asset['relative_path']).read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: str) -> None:
            return

        def do_GET(self) -> None:
            if not hmac.compare_digest(self.headers.get('X-Print-Token', ''), token) or self.path not in {*entries, '/print-data'}:
                self.send_error(404)
                return
            data = (root / 'print.json').read_bytes() if self.path == '/print-data' else entries[self.path]
            mime = 'application/json' if self.path == '/print-data' else 'text/html' if self.path.startswith('/document-print/') else mimetypes.guess_type(self.path)[0] or 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (ConnectionError, BrokenPipeError):
                return

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port, token, [key for key in entries if not key.startswith('/document-print/')] + ['/print-data']
        finally:
            server.shutdown()
            thread.join(timeout=3)
