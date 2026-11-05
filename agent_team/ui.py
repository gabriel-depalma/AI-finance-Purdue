"""A one-page interface for the agent team (Lab 05).

The page takes a ticker, optional peers and an optional price, and runs the same command line as the
terminal (team.py) in the background. It shows each node's name as the node starts, then the verified
report with its table of marks. It changes nothing in team.py: every terminal command works as before.

Start it with start_ui.bat (double-click), or:  .venv\\Scripts\\python.exe ui.py
"""
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TEAM = Path(__file__).resolve().parent
RUNS = TEAM / "runs"
PORT = 8765
TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Research Desk</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/4.3.0/marked.min.js"></script>
<style>
 :root { --bg:#f7f7f5; --panel:#fff; --ink:#1d1d1b; --muted:#6b6b66; --line:#e2e1dc; --accent:#2f5d8a;
         --ok:#2e7d4f; --bad:#b3261e; --warn:#9a6700; }
 @media (prefers-color-scheme: dark) { :root { --bg:#161615; --panel:#1f1f1d; --ink:#ecebe6; --muted:#a3a29b;
         --line:#33332f; --accent:#7fb0e0; --ok:#6fcf97; --bad:#ff8a80; --warn:#e0b44c; } }
 * { box-sizing:border-box; }
 body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
 main { max-width:1100px; margin:0 auto; padding:24px 16px 64px; }
 h1 { font-size:22px; margin:0 0 4px; } .sub { color:var(--muted); margin:0 0 20px; }
 form { display:flex; flex-wrap:wrap; gap:12px; align-items:flex-end; background:var(--panel);
        border:1px solid var(--line); border-radius:10px; padding:16px; }
 label { display:flex; flex-direction:column; gap:4px; font-size:13px; color:var(--muted); }
 input { font:inherit; color:var(--ink); background:var(--bg); border:1px solid var(--line); border-radius:6px;
         padding:8px 10px; min-width:120px; }
 button { font:inherit; font-weight:600; color:#fff; background:var(--accent); border:0; border-radius:6px;
          padding:9px 20px; cursor:pointer; } button:disabled { opacity:.5; cursor:default; }
 .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px; margin-top:16px; }
 ol.nodes { margin:0; padding-left:22px; } ol.nodes li { padding:2px 0; } ol.nodes li.now { font-weight:600; color:var(--accent); }
 #status { color:var(--muted); margin-top:8px; } .err { color:var(--bad); white-space:pre-wrap; }
 details { margin-top:8px; } pre.log { max-height:220px; overflow:auto; font-size:12px; background:var(--bg);
          padding:8px; border-radius:6px; white-space:pre-wrap; }
 #report { overflow-x:auto; } #report table { border-collapse:collapse; font-size:13px; margin:8px 0; }
 #report th, #report td { border:1px solid var(--line); padding:4px 6px; vertical-align:top; text-align:left; }
 #report code { font-size:12px; } #report h1 { font-size:20px; } #report h2 { font-size:17px; margin-top:24px; }
 .m-correct { color:var(--ok); } .m-wrong { color:var(--bad); } .m-unsupported { color:var(--warn); }
</style></head><body><main>
<h1>Research Desk</h1>
<p class="sub">The agent team on SEC filings: steps 1-7, Research Analyst, Financial Analyst, Investment Advisor,
Strategy Manager, Verifier.</p>
<form id="f">
 <label>Ticker <input id="ticker" required placeholder="COST" autocomplete="off"></label>
 <label>Peers (optional) <input id="peers" placeholder="WMT,TGT,BJ" autocomplete="off"></label>
 <label>Price (optional) <input id="price" placeholder="992.13" inputmode="decimal" autocomplete="off"></label>
 <button id="run" type="submit">Run</button>
</form>
<div class="panel" id="progress" hidden>
 <strong>Nodes</strong><ol class="nodes" id="nodes"></ol>
 <div id="status"></div>
 <details><summary>Full output</summary><pre class="log" id="log"></pre></details>
</div>
<div class="panel" id="report" hidden></div>
</main>
<script>
const $ = id => document.getElementById(id);
$("f").addEventListener("submit", e => {
  e.preventDefault();
  const q = new URLSearchParams({ticker: $("ticker").value.trim(), peers: $("peers").value.trim(),
                                 price: $("price").value.trim().replace(/[$,]/g, "")});
  $("run").disabled = true; $("nodes").innerHTML = ""; $("log").textContent = "";
  $("progress").hidden = false; $("report").hidden = true;
  $("status").className = ""; $("status").textContent = "Running... this takes several minutes.";
  const es = new EventSource("/run?" + q);
  es.addEventListener("node", ev => {
    document.querySelectorAll("#nodes li").forEach(li => li.classList.remove("now"));
    const li = document.createElement("li"); li.textContent = ev.data; li.className = "now"; $("nodes").appendChild(li);
  });
  es.addEventListener("log", ev => { $("log").textContent += ev.data + "\\n"; $("log").scrollTop = 1e9; });
  es.addEventListener("fail", ev => { es.close(); $("run").disabled = false;
    $("status").className = "err"; $("status").textContent = ev.data; });
  es.addEventListener("done", ev => { es.close();
    fetch("/report?folder=" + encodeURIComponent(ev.data)).then(r => r.json()).then(r => {
      $("run").disabled = false;
      document.querySelectorAll("#nodes li").forEach(li => li.classList.remove("now"));
      $("status").textContent = "Done. Saved in runs/" + ev.data + "/" + r.file;
      $("report").innerHTML = marked.parse(r.markdown); $("report").hidden = false;
      $("report").querySelectorAll("strong").forEach(s => {
        const t = s.textContent.trim(); if (["correct","wrong","unsupported"].includes(t)) s.className = "m-" + t; });
    });
  });
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) return; es.close(); $("run").disabled = false;
    $("status").className = "err"; $("status").textContent = "Lost the connection to the page server."; };
});
</script></body></html>"""


def latest_report(folder):
    reports = sorted(folder.glob("verified_report*.md"), key=lambda p: p.stat().st_mtime)
    return reports[-1] if reports else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the window quiet
        pass

    def send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def event(self, name, data):
        lines = "".join(f"data: {line}\n" for line in str(data).splitlines() or [""])  # one event, many lines
        self.wfile.write(f"event: {name}\n{lines}\n".encode("utf-8"))
        self.wfile.flush()

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if url.path == "/report":
            folder = RUNS / Path(q.get("folder", "")).name  # a run folder name only, never a path
            report = latest_report(folder) if folder.is_dir() else None
            if report is None:
                return self.send(404, json.dumps({"error": "no verified report"}), "application/json")
            return self.send(200, json.dumps({"file": report.name, "markdown": report.read_text(encoding="utf-8")}),
                             "application/json")
        if url.path == "/run":
            return self.run_team(q)
        self.send(404, "not found", "text/plain")

    def run_team(self, q):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        ticker, price = q.get("ticker", "").strip(), q.get("price", "").strip()
        peers = [p for p in re.split(r"[,\s]+", q.get("peers", "").strip()) if p]
        if not TICKER_RE.match(ticker) or not all(TICKER_RE.match(p) for p in peers):
            return self.event("fail", "A ticker is letters (and . or -), for example COST; peers are tickers "
                                      "separated by commas.")
        args = [sys.executable, "-u", str(TEAM / "team.py"), ticker.upper()]
        if peers:
            args += ["--peers", ",".join(p.upper() for p in peers)]
        if price:
            try:
                args += ["--price", f"{float(price)}"]
            except ValueError:
                return self.event("fail", "The price must be a number, for example 992.13.")
        proc = subprocess.Popen(args, cwd=TEAM, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        folder, tail = None, []
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                tail = (tail + [line])[-15:]
                if line.startswith(">>> node: "):
                    self.event("node", line[len(">>> node: "):])
                elif line.strip():
                    self.event("log", line)
                m = re.match(r"^Output folder: (.+)$", line.strip())
                if m:
                    folder = Path(m.group(1)).name
            proc.wait()
            if proc.returncode == 0 and folder:
                self.event("done", folder)
            else:
                self.event("fail", "The run stopped. Last lines:\n" + "\n".join(t for t in tail if t.strip()))
        except (BrokenPipeError, ConnectionResetError):  # the page was closed: stop the run
            proc.kill()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Research Desk page: {url}  (close this window to stop it)")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
