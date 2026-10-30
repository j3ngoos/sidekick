#!/usr/bin/env python3
"""Static test pages for SK-03 acceptance: python3 tests/serve.py -> http://127.0.0.1:8899/"""
import functools
import http.server
import os
import sys

PORT = int(os.environ.get("SIDEKICK_TEST_PORT", "8899"))
PAGES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages")

if __name__ == "__main__":
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=PAGES)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    print(f"serving {PAGES} on http://127.0.0.1:{PORT}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
