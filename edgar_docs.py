"""EDGAR document layer for the equity-research desk (cf. Francis & Zhang 2026, Sec. 3.1).

Resolves tickers to CIKs, walks a company's filing history, reads EDGAR index pages,
picks the latest 10-K / 10-Q / earnings 8-K, converts filing HTML to plain text and
locates the revenue and diluted-EPS lines of an earnings release.

Every network-facing function takes a `sec` client (see sec_client.SecClient) exposing
.get(url) -> bytes, .json(url) and .text(url). Nothing here prints; callers do that.
"""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

SEC = "https://www.sec.gov"
TICKERS_URL = f"{SEC}/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/"


# --------------------------------------------------------------------------- tickers

def _norm_ticker(ticker):
    """Upper-case and treat '.' and '-' alike (SEC writes class shares as e.g. XXX-B)."""
    return ticker.strip().upper().replace(".", "-")


def load_ticker_map(sec):
    """Fetch SEC's ticker file; return {normalized ticker: {"ticker","cik","title"}}."""
    out = {}
    for row in sec.json(TICKERS_URL).values():
        entry = {"ticker": row["ticker"], "cik": int(row["cik_str"]), "title": row["title"]}
        out.setdefault(_norm_ticker(row["ticker"]), entry)  # first listing wins
    return out


def lookup(ticker_map, ticker):
    """Return {"ticker","cik","title"} for `ticker`; raise LookupError if unknown."""
    try:
        return dict(ticker_map[_norm_ticker(ticker)])
    except KeyError:
        raise LookupError(f"Ticker not found in SEC ticker map: {ticker!r}") from None


# --------------------------------------------------------------------------- submissions

def _submissions(sec, cik):
    return sec.json(f"{SUBMISSIONS}CIK{int(cik):010d}.json")


def company_name(sec, cik):
    """Registrant name from the submissions file."""
    return _submissions(sec, cik).get("name", "")


def _rows(block):
    """Turn a submissions parallel-array block into filing dicts (in the given order)."""
    def col(key, i):
        values = block.get(key) or []
        return (values[i] if i < len(values) else "") or ""

    for i in range(len(block.get("accessionNumber", []))):
        yield {"form": col("form", i), "accession": col("accessionNumber", i),
               "filing_date": col("filingDate", i), "report_date": col("reportDate", i),
               "primary_document": col("primaryDocument", i), "items": col("items", i)}


def iter_filings(sec, cik):
    """Yield filing dicts newest first; older pages are fetched only if iteration gets there."""
    data = _submissions(sec, cik)
    filings = data.get("filings", {})
    yield from _rows(filings.get("recent", {}))
    pages = sorted(filings.get("files", []), key=lambda f: f.get("filingTo", ""), reverse=True)
    for page in pages:
        block = sec.json(SUBMISSIONS + page["name"])
        yield from _rows(block.get("filings", {}).get("recent", block))


def _folder(cik, accession):
    return f"{SEC}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"


# --------------------------------------------------------------------------- index page

class _TableParser(HTMLParser):
    """Collect every <table> as {"attrs", "rows"}; each row is a list of (text, href) td cells."""

    def __init__(self):
        super().__init__()
        self.tables, self._row, self._cell = [], None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.tables.append({"attrs": attrs, "rows": []})
        elif tag == "tr" and self.tables:
            self._row = []
            self.tables[-1]["rows"].append(self._row)
        elif tag == "td" and self._row is not None:
            self._cell = {"text": [], "href": None, "link": []}
            self._row.append(self._cell)
        elif tag == "a" and self._cell is not None and self._cell["href"] is None:
            self._cell["href"] = attrs.get("href")

    def handle_endtag(self, tag):
        if tag in ("td", "tr", "table"):
            self._cell = None
        if tag in ("tr", "table"):
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"].append(data)
            if self._cell["href"] is not None and not self._cell["link"]:
                self._cell["link"].append(data)  # first text after the <a> = link text


def _abs_url(href):
    """Strip the inline-XBRL viewer prefix and make the URL absolute."""
    href = re.sub(r"^(?:https?://www\.sec\.gov)?/ix\?doc=", "", href or "")
    return urljoin(SEC + "/", href) if href else ""


def filing_index(sec, cik, accession):
    """Parse the EDGAR -index.htm page: acceptance time and the Document Format Files table."""
    url = f"{_folder(cik, accession)}{accession}-index.htm"
    html = sec.text(url)
    m = re.search(r'class="?infoHead"?[^>]*>\s*Accepted\s*<.*?class="?info"?[^>]*>([^<]*)<',
                  html, re.I | re.S)
    accepted = " ".join(m.group(1).split()) if m else ""

    p = _TableParser()
    p.feed(html)
    tables = p.tables
    table = (next((t for t in tables if t["attrs"].get("summary") == "Document Format Files"), None)
             or next((t for t in tables if "tablefile" in (t["attrs"].get("class") or "").lower()), None))
    docs = []
    for row in (table or {}).get("rows", []):
        if len(row) < 4:  # header row (th cells) or malformed
            continue
        clean = lambda c: " ".join("".join(c["text"]).replace("\xa0", " ").split())
        link = row[2]
        doc_url = _abs_url(link["href"])
        name = " ".join("".join(link["link"]).split()) or doc_url.rsplit("/", 1)[-1]
        docs.append({"seq": clean(row[0]), "description": clean(row[1]), "name": name,
                     "type": clean(row[3]), "url": doc_url})
    return {"index_url": url, "accepted": accepted, "documents": docs}


# --------------------------------------------------------------------------- filing selection

ITEM_202 = re.compile(r"Item\s*2\.02", re.I)
MAX_8K_CANDIDATES = 10


def _doc_url(cik, filing):
    return _folder(cik, filing["accession"]) + filing["primary_document"] if filing["primary_document"] else None


def _exhibit(documents):
    """EX-99.1 document dict, else the first EX-99* one, else None."""
    exact = [d for d in documents if d["type"].upper() == "EX-99.1"]
    loose = [d for d in documents if d["type"].upper().startswith("EX-99")]
    return (exact or loose or [None])[0]


def pick_filings(sec, cik):
    """Latest original 10-K, 10-Q and Item 2.02 (earnings) 8-K; missing forms map to None.

    Each value is the iter_filings dict plus filing_index output and "doc_url"; the 8-K
    also carries "exhibit" (the EX-99.1 press-release document dict, or None).
    """
    found = {"10-K": None, "10-Q": None, "8-K": None}
    checked = 0
    for f in iter_filings(sec, cik):
        form = f["form"]  # exact match, so amendments (10-K/A, 8-K/A) never qualify
        if form in ("10-K", "10-Q") and found[form] is None:
            found[form] = f
        elif (form == "8-K" and found["8-K"] is None and checked < MAX_8K_CANDIDATES
              and "2.02" in f["items"] and f["primary_document"]):
            checked += 1
            if ITEM_202.search(document_text(sec, _doc_url(cik, f))):
                found["8-K"] = f
        done_8k = found["8-K"] is not None or checked >= MAX_8K_CANDIDATES
        if found["10-K"] and found["10-Q"] and done_8k:
            break

    for form, f in found.items():
        if f is not None:
            f = found[form] = {**f, **filing_index(sec, cik, f["accession"]), "doc_url": _doc_url(cik, f)}
            if form == "8-K":
                f["exhibit"] = _exhibit(f["documents"])
    return found


# --------------------------------------------------------------------------- HTML -> text

SKIP_TAGS = {"script", "style", "ix:header"}
BLOCK_TAGS = {"p", "div", "br", "tr", "li", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5",
              "h6", "hr", "title", "section", "article", "header", "footer", "blockquote",
              "pre", "center", "dl", "dt", "dd", "caption", "thead", "tbody", "tfoot", "body",
              "html", "head", "form", "page", "document"}


class _TextParser(HTMLParser):
    """Stream filing HTML into lines; table rows become 'cell | cell | ...' lines."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines, self.buf = [], []
        self.skip = 0
        self.row = None   # list of finished cell strings while inside a <tr>
        self.cell = None  # list of text fragments while inside a <td>/<th>

    # -- helpers
    def newline(self):
        self.lines.append(" ".join("".join(self.buf).split()))
        self.buf = []

    def close_cell(self):
        if self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None

    def flush_row(self):
        """Emit the open row (if any) as one line and reset the cell state."""
        if self.row is None:
            return
        self.close_cell()
        cells = [c for c in self.row if c]
        merged = []
        for c in cells:
            if merged and merged[-1] == "$":
                merged[-1] = "$" + c             # lone "$" joins the next cell
            elif merged and c in (")", "%"):
                merged[-1] += c                  # lone ")" / "%" joins the previous cell
            else:
                merged.append(c)
        self.row = None
        if merged:
            self.newline()
            self.buf.append(" | ".join(merged))
            self.newline()

    # -- parser callbacks
    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif self.skip:
            return
        elif tag == "tr":
            self.flush_row()
            self.row = []
        elif tag in ("td", "th"):
            if self.row is None:
                self.row = []
            self.close_cell()
            self.cell = []
        elif tag in BLOCK_TAGS:
            if self.cell is not None:
                self.cell.append(" ")
            else:
                self.newline()

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif self.skip:
            return
        elif tag in ("td", "th"):
            if self.row is not None:
                self.close_cell()
        elif tag in ("tr", "table"):
            self.flush_row()
            if tag == "table":
                self.newline()
        elif tag in BLOCK_TAGS:
            if self.cell is not None:
                self.cell.append(" ")
            else:
                self.newline()

    def handle_data(self, data):
        if self.skip:
            return
        data = re.sub(r"\s+", " ", data.replace("\xa0", " "))
        (self.cell if self.cell is not None else self.buf).append(data)

    def result(self):
        self.flush_row()
        self.newline()
        out, blank = [], False
        for line in self.lines:
            if line or not blank:
                out.append(line)
            blank = not line
        return "\n".join(out).strip()


def html_to_text(html):
    """Convert filing HTML / inline XBRL to plain text (see _TextParser for the rules)."""
    p = _TextParser()
    p.feed(html)
    p.close()
    return p.result()


_LOOKS_HTML = re.compile(r"<\s*(html|body|div|p|table|font|span|br)\b", re.I)


def document_text(sec, url):
    """Fetch a filing document as text; HTML is converted, plain .txt returned as is."""
    raw = sec.text(url)
    if url.lower().endswith(".txt") and not _LOOKS_HTML.search(raw[:20000]):
        return raw
    return html_to_text(raw)


# --------------------------------------------------------------------------- release lines

_REV_LABEL = r"(?:total\s+net\s+revenues?|total\s+revenues?|net\s+revenues?|revenues?|total\s+net\s+sales|net\s+sales|total\s+sales)"
REV_RE = re.compile(rf"\b{_REV_LABEL}\b", re.I)
REV_LABEL_CELL = re.compile(rf"{_REV_LABEL}", re.I)
REV_EXCLUDE = re.compile(r"cost of|marketing|selling|deferred|unearned", re.I)
AMOUNT_RE = re.compile(r"\$\s?\(?\d[\d,]*(?:\.\d+)?|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+(?:\.\d+)?\s*(?:billion|million)\b", re.I)
EPS_RE = re.compile(r"diluted", re.I)
PER_SHARE = r"per\s+(?:[a-z]+\s+){0,3}share"  # per share / per diluted share / per common share
EPS_CONTEXT = re.compile(rf"{PER_SHARE}|\bEPS\b|earnings\s+per", re.I)
DECIMAL_RE = re.compile(r"\d*\.\d+(?!\d)(?!\s*%)")
FORWARD_RE = re.compile(r"\b(?:expect\w*|outlook|guidance|forecast\w*|anticipat\w*)\b", re.I)
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"“])")
MAX_HITS = 5


def _label_cell(line):
    """First cell of a ' | ' table row with footnote marks / colons trimmed, else None."""
    if " | " not in line:
        return None
    cell = line.split(" | ", 1)[0]
    return re.sub(r"(?:\s*(?:\(\w{1,2}\)|\*+|:))+\s*$", "", cell).strip()


SHARE_COUNT_RE = re.compile(r"shares\s+used|weighted|average\s+(?:number\s+of\s+)?(?:common\s+)?shares|shares\s+outstanding", re.I)


def _per_share_section(lines, lineno):
    """True if the closest header within ~8 lines above is a per-share header, not a share-count one
    ("Shares used in computing earnings per share" is a share-count header)."""
    for prev in reversed(lines[max(0, lineno - 9):lineno - 1]):
        if SHARE_COUNT_RE.search(prev):
            return False
        if re.search(PER_SHARE, prev, re.I):
            return True
    return False


def release_lines(text):
    """Find the earnings-release lines stating total revenue and diluted EPS.

    Returns {"revenue": [(lineno, line)], "eps": [(lineno, line)]}, lineno 1-based in `text`.
    """
    lines = text.splitlines()
    hits = {"revenue": [], "eps": []}  # entries: (score, lineno, order, unit)
    order = 0
    for lineno, line in enumerate(lines, 1):
        units = SENTENCE_SPLIT.split(line) if len(line) > 300 else [line]
        for unit in units:
            unit = unit.strip()
            if not unit or FORWARD_RE.search(unit):
                continue
            order += 1
            label = _label_cell(unit)
            narrative_q = label is None and "quarter" in unit.lower()

            if REV_RE.search(unit) and AMOUNT_RE.search(unit) and not REV_EXCLUDE.search(unit):
                table_label = label is not None and REV_LABEL_CELL.fullmatch(label)
                score = 3 if narrative_q else 2 if table_label else 1
                hits["revenue"].append((score, lineno, order, unit))

            if EPS_RE.search(unit) and DECIMAL_RE.search(unit):
                is_eps = bool(EPS_CONTEXT.search(unit))
                diluted_row = label is not None and re.match(r"diluted\b", label, re.I)
                if diluted_row and not is_eps:  # bare "Diluted" row: the nearest header decides
                    is_eps = _per_share_section(lines, lineno)
                if is_eps:
                    table_label = label is not None and (diluted_row or (EPS_RE.search(label) and EPS_CONTEXT.search(label)))
                    score = 3 if narrative_q else 2 if table_label else 1
                    hits["eps"].append((score, lineno, order, unit))

    out = {}
    for key, found in hits.items():
        if any(h[0] >= 2 for h in found):
            found = [h for h in found if h[0] >= 2]
        found.sort(key=lambda h: (-h[0], h[1], h[2]))
        out[key] = [(lineno, unit) for _, lineno, _, unit in found[:MAX_HITS]]
    return out
