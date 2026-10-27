"""SEC EDGAR HTTP client: reads SEC_USER_AGENT from .env and sends it on every request."""
import gzip
import json
import time
import zlib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent


def read_user_agent(folder=HERE):
    """Return SEC_USER_AGENT from the .env file in `folder`; exit with a message if missing."""
    path = Path(folder) / ".env"
    if not path.is_file():
        raise SystemExit(f"No .env file at {path}. Add a line: SEC_USER_AGENT=Your Name your@email")
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        if key == "SEC_USER_AGENT":
            value = value.strip().strip('"').strip("'")
            if value:
                return value
    raise SystemExit(f"SEC_USER_AGENT is missing or empty in {path}. SEC requires a name and contact email.")


class SecClient:
    """Rate-limited (under SEC's 10 requests/second), retrying, in-memory-cached GET client."""

    MIN_INTERVAL = 0.15

    def __init__(self, user_agent):
        self.user_agent = user_agent
        self._last = 0.0
        self._cache = {}

    def get(self, url):
        if url in self._cache:
            return self._cache[url]
        for attempt in range(4):
            wait = self.MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            request = Request(url, headers={"User-Agent": self.user_agent,
                                            "Accept-Encoding": "gzip, deflate"})
            try:
                with urlopen(request, timeout=60) as response:
                    body = response.read()
                    encoding = response.headers.get("Content-Encoding", "")
            except HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                hint = " (SEC rejects requests without a valid User-Agent)" if e.code == 403 else ""
                raise SystemExit(f"SEC request failed: HTTP {e.code} for {url}{hint}")
            except URLError as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit(f"SEC request failed: {e.reason} for {url}")
            if encoding == "gzip":
                body = gzip.decompress(body)
            elif encoding == "deflate":
                body = zlib.decompress(body)
            self._cache[url] = body
            return body

    def json(self, url):
        return json.loads(self.get(url))

    def text(self, url):
        body = self.get(url)
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            return body.decode("cp1252", errors="replace")
