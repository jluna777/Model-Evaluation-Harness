"""Launch the CSV grader for this repo's calibration sheets.

    uv run python tools/grade.py

Serves the repository over localhost (the browser sandbox forbids a file://
page from reading its neighbors, so a loopback server is what lets the
grader find the sheets and emails on its own) and opens the grader in the
default browser. The page then lists every CSV in data/calibration/sheets
as a one-click option, loads the calibration emails automatically, and
saves verdicts straight back to the sheet file.

Endpoints beyond static serving:
- GET  /api/sheets       -> JSON list of sheet CSV filenames
- POST /api/save/<name>  -> overwrite an EXISTING sheet CSV (bytes as sent)

Binds 127.0.0.1 only. Saves are restricted to existing .csv files directly
inside data/calibration/sheets -- no path segments, no new files.
"""

import json
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHEETS = REPO / "data" / "calibration" / "sheets"
GRADER_PATH = "/tools/csv-grader.html"
PORTS = range(8765, 8776)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO), **kwargs)

    def log_message(self, *args):  # keep the terminal quiet
        pass

    def _reply(self, status, body=b"", content_type="application/json"):
        self.send_response(status)
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/sheets":
            names = sorted(p.name for p in SHEETS.glob("*.csv")) if SHEETS.is_dir() else []
            self._reply(200, json.dumps(names).encode("utf-8"))
            return
        super().do_GET()

    def do_POST(self):
        prefix = "/api/save/"
        if not self.path.startswith(prefix):
            self._reply(404)
            return
        name = self.path[len(prefix) :]
        target = SHEETS / name
        valid = (
            name
            and "/" not in name
            and "\\" not in name
            and name == target.name
            and name.lower().endswith(".csv")
            and target.is_file()
            and target.resolve().parent == SHEETS.resolve()
        )
        if not valid:
            self._reply(403)
            return
        length = int(self.headers.get("Content-Length", 0))
        target.write_bytes(self.rfile.read(length))
        self._reply(204)


def main():
    last_error = None
    for port in PORTS:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError as exc:
            last_error = exc
    else:
        raise SystemExit(f"no free port in {PORTS.start}-{PORTS.stop - 1}: {last_error}")

    url = f"http://127.0.0.1:{server.server_address[1]}{GRADER_PATH}"
    print(f"CSV grader: {url}")
    print("Ctrl+C stops the server.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
