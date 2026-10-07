#!/usr/bin/env python3
"""Dev server: rebuilds the wiki on change and live-reloads the browser.

Watches notes/ (recursively) and config.toml, re-running build.build() on
any change, and serves wiki_html/ with a small injected script that polls
for a new build and reloads the page automatically. Changes to static
files in wiki_html/ (currently just style.css) trigger a reload without
a rebuild, since build.py doesn't touch them.

Run: python3 watch.py [port]
"""

import mimetypes
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import build as build_mod

ROOT: Path = build_mod.ROOT
SRC: Path = build_mod.SRC
OUT: Path = build_mod.OUT
CONFIG_PATH: Path = build_mod.CONFIG_PATH
STATIC_PATHS: tuple[Path, ...] = (OUT / "style.css",)
POLL_INTERVAL: float = 0.3

RELOAD_SCRIPT: bytes = b"""
<script>
(function () {
  var known = null;
  setInterval(function () {
    fetch('/__reload__', { cache: 'no-store' })
      .then(function (r) { return r.text(); })
      .then(function (v) {
        if (known === null) { known = v; return; }
        if (v !== known) { location.reload(); }
      })
      .catch(function () {});
  }, 500);
})();
</script>
"""

_lock = threading.Lock()
_version = 0


def bump_version() -> None:
    global _version
    with _lock:
        _version += 1


def get_version() -> int:
    with _lock:
        return _version


def source_signature() -> frozenset[tuple[str, float]]:
    paths = list(SRC.rglob("*"))
    if CONFIG_PATH.exists():
        paths.append(CONFIG_PATH)
    return frozenset(
        (str(p), p.stat().st_mtime) for p in paths if p.is_file()
    )


def static_signature() -> frozenset[tuple[str, float]]:
    return frozenset(
        (str(p), p.stat().st_mtime) for p in STATIC_PATHS if p.is_file()
    )


def watch_loop() -> None:
    last_source = None
    last_static = None
    while True:
        source_sig = source_signature()
        static_sig = static_signature()
        if source_sig != last_source:
            last_source = source_sig
            try:
                build_mod.build()
            except Exception as e:  # keep the watcher alive on a bad edit
                print(f"build error: {e}", file=sys.stderr)
            bump_version()
        elif static_sig != last_static:
            bump_version()
        last_static = static_sig
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/__reload__":
            self._send(b"%d" % get_version(), "text/plain")
            return

        rel = self.path.split("?", 1)[0]
        if rel == "/":
            rel = "/index.html"
        file_path = (OUT / rel.lstrip("/")).resolve()
        if OUT.resolve() not in file_path.parents or not file_path.is_file():
            self.send_error(404)
            return

        data = file_path.read_bytes()
        if file_path.suffix == ".html":
            data = data.replace(b"</body>", RELOAD_SCRIPT + b"</body>", 1) \
                if b"</body>" in data else data + RELOAD_SCRIPT
            self._send(data, "text/html")
        else:
            content_type, _ = mimetypes.guess_type(str(file_path))
            self._send(data, content_type or "application/octet-stream")

    def _send(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    threading.Thread(target=watch_loop, daemon=True).start()

    while get_version() == 0:
        time.sleep(0.05)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"serving {url} (watching {SRC} and {CONFIG_PATH.name})")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
