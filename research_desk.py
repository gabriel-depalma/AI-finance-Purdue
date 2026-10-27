"""Data side of an equity-research desk, after Francis & Zhang (2026), Section 3.1.

Usage:
    python research_desk.py TICKER [--peers T1 T2 ...] [--price P] [--step N]

Steps: (1) ticker -> CIK, (2) latest 10-K / 10-Q / Item 2.02 8-K, (3) download documents as text,
(4) XBRL facts, (5) calculator, (6) release lines for revenue and diluted EPS, (7) peers.
"""
import argparse
import json
import re
import sys
from datetime import datetime

import edgar_docs
import xbrl_facts
from sec_client import HERE, SecClient, read_user_agent

STEP_TITLES = {
    1: "Resolve ticker to CIK",
    2: "Latest 10-K, 10-Q and earnings 8-K (Item 2.02)",
    3: "Download documents and convert to text",
    4: "XBRL company facts, latest fiscal year",
    5: "Calculator",
    6: "Earnings release: quarter's total revenue and diluted EPS",
    7: "Peers",
}
DEPENDS = {1: [], 2: [1], 3: [2], 4: [1], 5: [4], 6: [2], 7: []}
DOC_FILES = {"10-K": "10-K.txt", "10-Q": "10-Q.txt", "8-K": "8-K_EX-99.1_release.txt"}


class Desk:
    def __init__(self, args, sec):
        self.args = args
        self.sec = sec
        self.run_time = datetime.now()
        self.show = True
        self.done = {}
        self._ticker_map = None
        self._folder = None
        self.saved_texts = {}
        self.record = {"run_time": self.run_time.isoformat(timespec="seconds"),
                       "ticker": args.ticker.upper(), "price": args.price, "peers_requested": args.peers}

    # ---- plumbing -------------------------------------------------------------------------
    def say(self, *lines):
        if self.show:
            for line in lines:
                print(line)

    def run(self, n, show=True):
        """Run step n once; prerequisites run quietly so --step prints only the step asked for."""
        if n in self.done:
            return self.done[n]
        for dep in DEPENDS[n]:
            self.run(dep, show=False)
        previous, self.show = self.show, show
        try:
            self.say("", f"=== Step {n}: {STEP_TITLES[n]} ===")
            self.done[n] = getattr(self, f"step{n}")()
        finally:
            self.show = previous
        return self.done[n]

    def ticker_map(self):
        if self._ticker_map is None:
            self._ticker_map = edgar_docs.load_ticker_map(self.sec)
        return self._ticker_map

    def folder(self):
        """runs/<Company>_<YYYY-MM-DD_HHMMSS>, created on first save."""
        if self._folder is None:
            name = re.sub(r"[^A-Za-z0-9]+", "_", self.company["title"]).strip("_") or self.company["ticker"]
            self._folder = HERE / "runs" / f"{name}_{self.run_time:%Y-%m-%d_%H%M%S}"
            self._folder.mkdir(parents=True, exist_ok=True)
        return self._folder

    def save_record(self):
        path = self.folder() / "facts.json"
        path.write_text(json.dumps(self.record, indent=2, default=str), encoding="utf-8")
        return path

    def save_text(self, form):
        """Download a picked document (the 8-K's release exhibit), convert to text, save once."""
        if form in self.saved_texts:
            return self.saved_texts[form]
        f = self.filings[form]
        doc_url = f["exhibit"]["url"] if form == "8-K" else f["doc_url"]
        text = edgar_docs.document_text(self.sec, doc_url)
        header = (f"Source: {doc_url}\nForm: {form}  Accession: {f['accession']}  "
                  f"Accepted: {f['accepted']}\n{'-' * 80}\n")
        path = self.folder() / DOC_FILES[form]
        path.write_text(header + text, encoding="utf-8")
        self.saved_texts[form] = (path, text, doc_url)
        return self.saved_texts[form]

    # ---- steps ----------------------------------------------------------------------------
    def step1(self):
        try:
            self.company = edgar_docs.lookup(self.ticker_map(), self.args.ticker)
        except LookupError:
            raise SystemExit(f"Ticker {self.args.ticker!r} not found in SEC company_tickers.json")
        c = self.company
        self.record.update(company=c["title"], cik=c["cik"])
        self.say(f"  {c['ticker']} -> CIK {c['cik']:010d}  ({c['title']})",
                 "  source: https://www.sec.gov/files/company_tickers.json")
        return c

    def step2(self):
        self.filings = edgar_docs.pick_filings(self.sec, self.company["cik"])
        self.record["filings"] = {}
        for form in ("10-K", "10-Q", "8-K"):
            f = self.filings.get(form)
            if f is None:
                self.say(f"  {form:5} none found")
                continue
            self.record["filings"][form] = {k: f.get(k) for k in (
                "accession", "filing_date", "report_date", "accepted", "index_url", "doc_url", "items")}
            self.say(f"  {form:5} accession {f['accession']}   Accepted {f['accepted']}   "
                     f"(filed {f['filing_date']}, period {f['report_date'] or 'n/a'})",
                     f"        {f['index_url']}")
            if form == "8-K":
                ex = f.get("exhibit")
                self.record["filings"][form]["exhibit"] = ex
                self.say(f"        items {f['items']}; release exhibit: "
                         + (f"{ex['type']} {ex['name']}" if ex else "none found"))
        return self.filings

    def step3(self):
        for form in ("10-K", "10-Q", "8-K"):
            f = self.filings.get(form)
            if f is None or (form == "8-K" and not f.get("exhibit")):
                self.say(f"  {form:5} skipped (no document)")
                continue
            path, text, url = self.save_text(form)
            self.say(f"  {form:5} {url}", f"        -> {path}  ({len(text):,} chars)")
        return self.saved_texts

    def print_facts(self, facts, indent="  "):
        self.say(f"{indent}Fiscal year ended {facts['fy_end']}  (prior year ended {facts['prior_end']}); "
                 f"anchored on {facts['anchor']}")
        for key, c in facts["concepts"].items():
            fmt = xbrl_facts.fmt_eps if key == "eps_diluted" else xbrl_facts.fmt_usd
            fy, prior = c["fy"], c["prior"]
            self.say(f"{indent}{c['label']:<36} {fmt(fy['value'] if fy else None):>16}   "
                     f"prior {fmt(prior['value'] if prior else None):>16}")
            if c["tag"] is None:
                self.say(f"{indent}    no candidate tag reported: {', '.join(c['candidates'])}")
            else:
                src = f"{fy['form']} {fy['accn']} filed {fy['filed']}" if fy else "no value for this year"
                self.say(f"{indent}    tag us-gaap:{c['tag']}  period end {fy['end'] if fy else facts['fy_end']}"
                         f"  filing {src}")
            if c.get("note"):
                self.say(f"{indent}    note: {c['note']}")

    def step4(self):
        cf = xbrl_facts.fetch_company_facts(self.sec, self.company["cik"])
        self.facts = xbrl_facts.extract(cf)
        self.say(f"  source: https://data.sec.gov/api/xbrl/companyfacts/CIK{self.company['cik']:010d}.json")
        self.print_facts(self.facts)
        self.record["facts"] = self.facts
        self.say(f"  saved -> {self.save_record()}")
        return self.facts

    def step5(self):
        calc = xbrl_facts.calculate(self.facts, self.args.price)
        names = {"revenue_growth": "Revenue growth", "operating_margin": "Operating margin",
                 "roae": "Return on average equity", "debt_to_equity": "Debt to equity",
                 "fcf": "Free cash flow", "pe": "P/E"}
        self.say("  ($ amounts in millions)")
        for key, label in names.items():
            m = calc[key]
            self.say(f"  {label:<26} = {m['formula']}")
            if m["arithmetic"]:
                self.say(f"  {'':<26} = {m['arithmetic']}")
            if m.get("note"):
                self.say(f"  {'':<26}   note: {m['note']}")
        self.record["calculations"] = calc
        self.say(f"  saved -> {self.save_record()}")
        return calc

    def step6(self):
        f = self.filings.get("8-K")
        if f is None or not f.get("exhibit"):
            self.say("  no earnings release found")
            return None
        path, text, url = self.save_text("8-K")
        found = edgar_docs.release_lines(text)
        self.say(f"  release: {path}  (8-K accession {f['accession']}, period {f['report_date']})")
        for key, label in (("revenue", "Total revenue"), ("eps", "Diluted EPS")):
            self.say(f"  {label}:")
            if not found[key]:
                self.say("    no matching line found")
            for lineno, line in found[key]:
                self.say(f"    L{lineno}: {line if len(line) <= 400 else line[:400] + ' ...'}")
        self.record["release_lines"] = {k: [{"line": n, "text": t} for n, t in v] for k, v in found.items()}
        if "company" in self.record:
            self.say(f"  saved -> {self.save_record()}")
        return found

    def step7(self):
        peers = [p for p in (self.args.peers or []) if p.upper() != self.args.ticker.upper()]
        if not peers:
            self.say("  no peers given (use --peers T1 T2 ...)")
            return {}
        if "company" not in self.record:
            self.run(1, show=False)  # needed for the output folder name
        results, summary = {}, []
        for ticker in peers:
            try:
                co = edgar_docs.lookup(self.ticker_map(), ticker)
            except LookupError:
                self.say(f"", f"  {ticker}: not found in SEC company_tickers.json")
                continue
            self.say("", f"  --- {co['ticker']}  CIK {co['cik']:010d}  ({co['title']}) ---")
            facts = xbrl_facts.extract(xbrl_facts.fetch_company_facts(self.sec, co["cik"]))
            self.print_facts(facts, indent="    ")
            om = xbrl_facts.operating_margin(facts)
            self.say(f"    Operating margin = {om['formula']}", f"                     = {om['arithmetic']}")
            if om.get("note"):
                self.say(f"      note: {om['note']}")
            results[co["ticker"]] = {"cik": co["cik"], "company": co["title"], "facts": facts,
                                     "operating_margin": om}
            summary.append((co["ticker"], facts["fy_end"], om["value"]))
        if hasattr(self, "facts"):
            summary.insert(0, (self.company["ticker"] + " (target)", self.facts["fy_end"],
                               xbrl_facts.operating_margin(self.facts)["value"]))
        self.say("", "  Operating margin = operating income / total revenue, latest fiscal year")
        for name, fy_end, value in summary:
            self.say(f"    {name:<16} FY ended {fy_end}   " + ("n/a" if value is None else f"{value:.2%}"))
        self.record["peers"] = results
        self.say(f"  saved -> {self.save_record()}")
        return results


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Equity-research desk: data side (SEC EDGAR).")
    parser.add_argument("ticker", help="ticker symbol of the company to research")
    parser.add_argument("--peers", nargs="+", default=[], metavar="TICKER",
                        help="peer tickers, space- or comma-separated")
    parser.add_argument("--price", type=float, help="share price, for P/E")
    parser.add_argument("--step", type=int, choices=range(1, 8), metavar="N", help="run only step N (1-7)")
    args = parser.parse_args(argv)
    args.peers = [p.strip().upper() for arg in args.peers for p in arg.split(",") if p.strip()]

    desk = Desk(args, SecClient(read_user_agent()))
    for n in ([args.step] if args.step else range(1, 8)):
        desk.run(n)
    if desk._folder is not None:
        print(f"\nOutput folder: {desk._folder}")


if __name__ == "__main__":
    main()
