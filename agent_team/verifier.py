"""The Verifier: checks every number in a compiled report against the saved filings and facts.

It reads only three things from a run folder: report.md, the saved filing texts and facts.json.
It skips the Investment Advisor's thesis. For every other number it checks, in code:
  - [XBRL TICKER us-gaap:Tag YYYY-MM-DD]  the value against the facts file, exactly (at the printed precision);
  - [Release L45: "quote"] / [10-K L123: "quote"]  that the quoted line is in that filing's text
    (allowing for spacing and formatting) and that the number is on it;
  - [ratio name: arithmetic]  by recomputing it from its quoted inputs, which must be in the facts or
    filings, and that the report defines the ratio;
  - [price given with --price]  against the price saved in the facts file.
Then one model call reads each cited line and says whether it states that number for that period in
that definition. A number with no source is unsupported. Each number is marked correct, wrong or
unsupported, and wrong and unsupported numbers are corrected from the filings with the real line or tag.
"""
import ast
import json
import operator
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from desk_roles import _contains, _norm, _plain  # noqa: E402  (text normalisation shared with the desk)

ROLE_FILE = Path(__file__).resolve().parent / "roles" / "verifier.md"
DOC_FILES = {"Release": "8-K_EX-99.1_release.txt", "10-K": "10-K.txt", "10-Q": "10-Q.txt"}
CITE_RE = re.compile(r"\[(XBRL|Release|10-K|10-Q|ratio|price)\b([^\]]*)\]")
NUM_RE = re.compile(
    r"(?<![\w.,])(?P<neg>-(?=[$\d]))?(?P<dollar>\$)?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?![\w]*-[A-Za-z])(?P<unit>\s?%|x(?![a-zA-Z])|\s?(?:billion|million|thousand)\b|\s?[BMK](?![a-zA-Z]))?")
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
MASK_RES = [re.compile(r"\b\d{10}-\d{2}-\d{6}\b"), re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
            re.compile(rf"\b(?:{MONTHS})\s+\d{{1,2}},\s+\d{{4}}\b")]
DATE_CELL = re.compile(rf"(?:{MONTHS})\s+\d{{1,2}},\s+\d{{4}}")
SCALES = {"billion": 1e9, "b": 1e9, "million": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


class Check(BaseModel):
    id: int
    verdict: Literal["states_it", "does_not"]
    reason: str
    corrected_value: str
    corrected_line: int


class Checks(BaseModel):
    items: list[Check]


# ---------------------------------------------------------------- arithmetic and numbers

def eval_expr(expr):
    """Value of arithmetic written with numbers (commas and $ allowed), + - * / and parentheses."""
    clean = re.sub(r"[$,]", "", _plain(expr))

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            return -ev(node.operand) if isinstance(node.op, ast.USub) else ev(node.operand)
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Div) and b == 0:
                raise ValueError("division by zero")
            return OPS[type(node.op)](a, b)
        raise ValueError("only numbers, + - * / and parentheses are allowed")
    return ev(ast.parse(clean, mode="eval"))


def parse_number(m):
    num = m.group("num")
    x = float(num.replace(",", "")) * (-1 if m.group("neg") else 1)
    unit = (m.group("unit") or "").strip().lower()
    return {"text": m.group(0).strip(), "value": x, "decimals": len(num.split(".")[1]) if "." in num else 0,
            "dollar": bool(m.group("dollar")), "unit": unit, "commas": "," in num,
            "suffix": (m.group("unit") or "").rstrip()}


def is_financial(m):
    """Amounts, percentages, multiples and decimals; plain whole numbers are counts or years."""
    return bool(m.group("dollar") or m.group("unit") or "." in m.group("num") or "," in m.group("num"))


def numbers_in(text):
    return [parse_number(m) for m in NUM_RE.finditer(_plain(text))]


def close(x, decimals, target):
    return abs(x - target) <= 0.5 * 10 ** -decimals + 1e-9 * abs(target)


def on_line(n, line):
    """The number, as written, appears on the line at the same or a thousandfold scale."""
    return any(close(n["value"], n["decimals"], y["value"] * k) or close(n["value"], n["decimals"], -y["value"] * k)
               for y in numbers_in(line) for k in (1, 1e-3, 1e3))


def fmt_like(n, value):
    """Write `value` the way the report wrote number n (sign, $, decimals, unit)."""
    d = n["decimals"]
    body = f"{abs(value):,.{d}f}" if n["commas"] or abs(value) >= 10000 else f"{abs(value):.{d}f}"
    return ("-" if value < 0 else "") + ("$" if n["dollar"] else "") + body + n["suffix"]


def mask(line, spans):
    """Blank out citations, dates and accession numbers so their digits are not read as numbers."""
    chars = list(line)
    spans = list(spans) + [m.span() for r in MASK_RES for m in r.finditer(line)]
    for s, e in spans:
        chars[s:e] = " " * (e - s)
    return "".join(chars)


# ---------------------------------------------------------------- the saved run

class Run:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.report = (self.folder / "report.md").read_text(encoding="utf-8").splitlines()
        self.record = json.loads((self.folder / "facts.json").read_text(encoding="utf-8"))
        self.docs = {k: (self.folder / f).read_text(encoding="utf-8").splitlines()
                     for k, f in DOC_FILES.items() if (self.folder / f).exists()}
        self.companies = {self.record["ticker"]: self.record.get("facts") or {}}
        self.companies.update({t: p["facts"] for t, p in (self.record.get("peers") or {}).items()})
        self.facts = []  # (ticker, tag, end, value, concept key)
        for t, f in self.companies.items():
            for key, c in (f.get("concepts") or {}).items():
                for p in ("fy", "prior"):
                    if c.get(p):
                        self.facts.append((t, c[p]["tag"], c[p]["end"], c[p]["value"], key))
        for a in self.record.get("alternative_facts") or []:
            self.facts.append((a["ticker"], a["tag"], a["end"], a["value"], a["concept"]))
        self.definitions = {m.group(1) for line in self.report for m in [re.match(r"^\s*- `(\w+)`:", line)] if m}

    def fact(self, ticker, tag, end):
        return next(((v, key) for t, g, e, v, key in self.facts if t == ticker and g == tag and e == end), None)

    def fact_matches(self, n):
        """Facts whose value equals number n at some printed scale."""
        scales = [SCALES[n["unit"]]] if n["unit"] in SCALES else [1e6, 1e9, 1e3, 1]
        return [(t, g, e, v) for t, g, e, v, key in self.facts
                if n["unit"] not in ("%", "x") and any(close(n["value"], n["decimals"], v / s) for s in scales)]

    def input_traced(self, x_text):
        """A ratio input is in the facts (in $M or per share) or written in a filing text."""
        x = float(x_text.replace(",", ""))
        d = len(x_text.split(".")[1]) if "." in x_text else 0
        if any(close(x, d, v / s) for *_, v, _ in self.facts for s in (1e6, 1)):
            return True
        if self.record.get("price") is not None and close(x, d, self.record["price"]):
            return True
        pat = re.compile(rf"(?<![\d,.]){re.escape(x_text)}(?![\d])")
        return any(pat.search(line) for lines in self.docs.values() for line in lines)


CONCEPT_WORDS = {"revenue": r"revenue|sales", "operating_income": r"operating income", "net_income": r"net income",
                 "eps_diluted": r"diluted", "equity": r"equity", "lt_debt": r"debt", "lt_debt_current": r"debt",
                 "cfo": r"operating activities", "capex": r"property|equipment"}


def tenk_line(run, value, key):
    """The 10-K line that prints a fact (per share, or in $ millions), e.g. 'Diluted | $20.76 | $18.21'."""
    shown = f"{value:.2f}" if key == "eps_diluted" else f"{value / 1e6:,.0f}"
    pat = re.compile(rf"(?<![\d,.]){re.escape(shown)}(?![\d])")
    words = re.compile(CONCEPT_WORDS.get(key, r"."), re.I)
    hit = next((j + 1 for j, line in enumerate(run.docs.get("10-K", []))
                if " | " in line and pat.search(line) and words.search(line.split(" | ")[0])), None)
    return f"; 10-K L{hit}" if hit else ""


def table_header(lines, i):
    """For a table row: the nearest header line above it with column dates, and the line before that."""
    if " | " not in lines[i]:
        return ""
    for j in range(i - 1, max(-1, i - 80), -1):
        if len(DATE_CELL.findall(lines[j])) >= 2:
            before = next((lines[k] for k in range(j - 1, max(-1, j - 4), -1) if lines[k].strip()), "")
            return f"L{j - 1}: {before}\nL{j + 1}: {lines[j]}" if before else f"L{j + 1}: {lines[j]}"
    return ""


# ---------------------------------------------------------------- the check

FACT_KEYS = {"revenue", "operating_income", "net_income", "eps_diluted", "equity", "lt_debt", "lt_debt_current",
             "cfo", "capex"}
# source forms of reports written by the Lab 03 desk: fact ids, inline arithmetic, the sources table
LEGACY_ID_RE = re.compile(r"\[([A-Z][A-Z0-9.]*)_([a-z_]+)\]|\[(calculator|share_price)\]")
LEGACY_XBRL_RE = re.compile(r"us-gaap:(\w+),\s*(?:\d{4}-\d{2}-\d{2}\s+to\s+|at\s+)(\d{4}-\d{2}-\d{2})")
ARITH_VAL_RE = re.compile(r"=\s*(-?\$?\d[\d,]*(?:\.\d+)?(?:\s?%|x|M|B)?)")
ARITH_CHARS = set("0123456789,.$MBK()+-*/ ")


def _cite(start, end, kind, body, text, val=None):
    return {"start": start, "end": end, "kind": kind, "body": body, "text": text, "val": val}


def arithmetic_cites(line):
    """'( (a - b) / b = 10.14% )' and table cells 'a / b = 3.85%': a ratio sourced by its arithmetic."""
    for m in ARITH_VAL_RE.finditer(line):
        j = m.start()
        while j > 0 and line[j - 1] in ARITH_CHARS | {"*"}:
            j -= 1
        raw = line[j:m.start()]
        if "**" in raw:  # '**$9,382.0 M** ( a - b = ...': the bold number before is not part of the arithmetic
            j += raw.rindex("**") + 2
            raw = line[j:m.start()]
        start = j + len(raw) - len(raw.lstrip("* "))
        expr = raw.strip("* ")
        while expr.startswith("(") and expr.count("(") > expr.count(")"):
            expr = expr[1:].strip()
        if not re.search(r"\d[MBK]?\)?\s*[+\-*/]\s*[\d($]", expr):
            continue
        end = m.end()
        rest = line[end:]
        if rest.lstrip(" ").startswith(")"):
            end += len(rest) - len(rest.lstrip(" ")) + 1
        yield _cite(start, end, "ratio", f"arith: {expr}", line[start:end], m.span(1))


def citations(line):
    cites = [_cite(*c.span(), c.group(1), c.group(2), c.group(0)) for c in CITE_RE.finditer(line)]
    taken = lambda s, e: any(s < c["end"] and c["start"] < e for c in cites)
    for c in arithmetic_cites(line):
        if not taken(c["start"], c["end"]):
            cites.append(c)
    for m in LEGACY_ID_RE.finditer(line):
        if taken(*m.span()):
            continue
        if m.group(3):
            kind = "price" if m.group(3) == "share_price" else "id_only"
            cites.append(_cite(*m.span(), kind, "", m.group(0)))
            continue
        name = re.match(r"^(\w+?)_(fy|prior)$", m.group(2))
        if name and name.group(1) in FACT_KEYS:
            cites.append(_cite(*m.span(), "fact_id", f"{m.group(1)} {name.group(1)} {name.group(2)}", m.group(0)))
        else:
            cites.append(_cite(*m.span(), "id_only", m.group(2), m.group(0)))
    for m in LEGACY_XBRL_RE.finditer(line):
        if not taken(*m.span()):
            cites.append(_cite(*m.span(), "xbrl_any", f"{m.group(1)} {m.group(2)}", m.group(0)))
    return sorted(cites, key=lambda c: c["start"])


def ratio_fits(n, body):
    """True if number n is what the ratio citation's arithmetic gives."""
    m = re.match(r"^\s*(\w+)\s*:\s*(.+)$", body)
    try:
        r = eval_expr(re.sub(r"(?<=\d)\s?[MBK]\b", "", m.group(2))) if m else None
    except (ValueError, SyntaxError):
        return False
    if r is None:
        return False
    target = r * 100 if n["unit"] == "%" else r * 1e6 / SCALES[n["unit"]] if n["unit"] in SCALES else r
    return close(n["value"], n["decimals"], target)


def find_items(run):
    """Every financial number in the report body outside the thesis, paired with the citation after it."""
    items, part, in_thesis = [], "", False
    end = next((i for i, line in enumerate(run.report) if i > 1 and line.strip() == "---"), len(run.report))
    for i in range(2, end):
        line = _plain(run.report[i])
        if line.startswith("## "):
            part, in_thesis = re.sub(r"^##\s*\d*\.?\s*", "", line).strip(), False
        if line.startswith("### Investment thesis"):
            in_thesis = True
        # in the Investment Recommendation, all but the recommendation and the deciding number is the thesis
        thesis = part == "Investment Recommendation" and line.strip() and \
            not line.startswith(("**Recommendation", "**Deciding number", "Insight:"))
        if in_thesis or thesis or line.startswith("#"):
            continue
        cites = citations(line)
        masked = mask(line, [(c["start"], c["end"]) for c in cites])
        nums = [m for m in NUM_RE.finditer(masked) if is_financial(m)]
        used, prev_end = set(), 0
        for c in cites:
            pool = [m for m in nums if prev_end <= m.start() and m.end() <= c["start"] and m.start() not in used]
            prev_end = c["end"]
            # the same sentence: no sentence break between the number and the citation, except the period
            # that ends the sentence just before the citation ('... in five years. [Release L16: ...]')
            clause = [m for m in pool if not re.search(r"[.;]\s", line[m.end():c["start"]].rstrip(" .;"))
                      and len(line[m.end():c["start"]]) <= 250]
            if not clause and c["val"]:  # a table cell 'a / b = 3.85%': the result itself is the number
                m = NUM_RE.match(line, c["val"][0])
                if m:
                    items.append({"line": i, "span": m.span(), "part": part, "number": parse_number(m),
                                  "kind": c["kind"], "cite": c["text"], "body": c["body"]})
                continue
            if not clause:
                continue
            # the citation sources the number nearest before it, and any other number of the same
            # clause that its quoted words contain ("increased 11% to $5,907" sources both)
            quote = re.search(r'"(.*)"', c["body"])
            backing = quote.group(1) if quote else ""
            bare = re.match(r"^\s*L(\d+)\s*$", c["body"]) if c["kind"] in DOC_FILES and not quote else None
            if bare:  # [Release L16] with no quote: the cited line backs the clause's numbers
                doc, k = run.docs.get(c["kind"], []), int(bare.group(1))
                backing = doc[k - 1] if 1 <= k <= len(doc) else ""
            chosen = [m for m in clause[:-1] if backing and on_line(parse_number(m), backing)] + [clause[-1]]
            if c["kind"] == "ratio":  # 'A (x%), B (y%) and C (z%) [ratio ...]': the number the arithmetic gives
                fits = [m for m in clause if ratio_fits(parse_number(m), c["body"])]
                if fits:
                    chosen = fits[-1:]
            for m in chosen:
                used.add(m.start())
                items.append({"line": i, "span": m.span(), "part": part, "number": parse_number(m),
                              "kind": c["kind"], "cite": c["text"], "body": c["body"]})
        cited = {it["number"]["text"]: it for it in items if it["line"] == i and it["kind"]}
        for m in nums:
            if m.start() not in used:
                # '10.14% demonstrates ... (10.14% [ratio ...])': the same figure cited later in the line
                same = cited.get(parse_number(m)["text"])
                items.append({"line": i, "span": m.span(), "part": part, "number": parse_number(m),
                              "kind": same["kind"] if same else None, "cite": same["cite"] if same else "",
                              "body": same["body"] if same else ""})
    items.sort(key=lambda it: (it["line"], it["span"][0]))
    for k, it in enumerate(items, 1):
        it["id"] = k
    return items


def code_check(run, it):
    """Fill mark/code/correction for one item; line-sourced items get 'model': pending."""
    n, kind, body = it["number"], it["kind"], it["body"].strip()
    it.update(mark=None, code="", model="", correction="")

    if kind is None:
        it["mark"], it["code"] = "unsupported", "no source"
        # the number as written, on a line of the release, the 10-Q or the 10-K; else the company's own
        # facts before a peer's
        digits = re.sub(r"[^\d,.]", "", n["text"]).strip(",.")
        pat = re.compile(rf"(?<![\d,.]){re.escape(digits)}(?![\d])")
        src = next((f"[{doc} L{j + 1}]" for doc in ("Release", "10-Q", "10-K")
                    for j, line in enumerate(run.docs.get(doc, [])) if pat.search(line)), "")
        found = sorted(run.fact_matches(n), key=lambda f: f[0] != run.record["ticker"])
        if not src and found:
            src = "[XBRL {} us-gaap:{} {}]".format(*found[0][:3])
        it["correction"] = src or "no source found in the filings or facts"
        return

    if kind == "price":
        p = run.record.get("price")
        if p is None:
            it["mark"], it["code"] = "unsupported", "no price saved in the facts file"
        elif close(n["value"], n["decimals"], p):
            it["mark"], it["code"] = "correct", f"equals the price given ({p})"
        else:
            it["mark"], it["code"], it["correction"] = "wrong", f"price given was {p}", \
                f"{fmt_like(n, p)} [price given with --price]"
        return

    if kind == "id_only":
        it["mark"], it["code"] = "unsupported", "cited by a name, with no tag, line or arithmetic"
        same = run.fact_matches(n)
        if same:
            it["correction"] = "[XBRL {} us-gaap:{} {}]".format(*same[0][:3])
        return

    if kind in ("XBRL", "fact_id", "xbrl_any"):
        if kind == "fact_id":  # [TICKER_concept_fy] from the Lab 03 desk
            t, key, period = body.split()
            c = ((run.companies.get(t) or {}).get("concepts") or {}).get(key) or {}
            f = c.get(period)
            t, tag, end = (t, f["tag"], f["end"]) if f else (t, None, None)
        elif kind == "xbrl_any":  # 'us-gaap:Tag, start to end' in a sources table: the company is not named
            tag, end = body.split()
            owners = [x for x in run.facts if x[1] == tag and x[2] == end]
            matching = [x for x in owners if any(close(n["value"], n["decimals"], x[3] / s) for s in (1e6, 1e9, 1e3, 1))]
            t = (matching or owners or [("",)])[0][0]
        else:
            m = re.match(r"^([A-Z][A-Z0-9.\-]*)\s+(?:us-gaap:)?(\w+)\s+(?:(\d{4}-\d{2}-\d{2})\s+to\s+)?(\d{4}-\d{2}-\d{2})$", body)
            if not m:
                it["mark"], it["code"] = "unsupported", "citation is not [XBRL TICKER us-gaap:Tag YYYY-MM-DD]"
                return
            t, tag, _, end = m.groups()
        hit = run.fact(t, tag, end) if tag else None
        if hit is None:
            it["mark"], it["code"] = "unsupported", "tag and period not in the facts file"
            found = run.fact_matches(n)
            if found:
                it["correction"] = "[XBRL {} us-gaap:{} {}]".format(*found[0][:3])
            return
        v, key = hit
        scales = [1] if key == "eps_diluted" else [SCALES[n["unit"]]] if n["unit"] in SCALES else [1e6, 1e9, 1e3, 1]
        if any(close(n["value"], n["decimals"], v / s) for s in scales):
            it["mark"], it["code"] = "correct", "equals the facts file"
        else:
            s = scales[0] if key == "eps_diluted" or n["unit"] in SCALES else 1e6
            it["mark"], it["code"] = "wrong", f"facts file has {v:,.2f}"
            line = tenk_line(run, v, key) if t == run.record["ticker"] else ""
            it["correction"] = f"{fmt_like(n, v / s)} [XBRL {t} us-gaap:{tag} {end}{line}]"
        return

    if kind == "ratio":
        m = re.match(r"^(\w+)\s*:\s*(.+)$", body)
        if not m:
            it["mark"], it["code"] = "unsupported", "citation is not [ratio name: arithmetic]"
            return
        name, expr = m.groups()
        expr = re.sub(r"(?<=\d)\s?[MBK]\b", "", expr)  # '$713,163.0M': inputs in $ millions
        try:
            r = eval_expr(expr)
        except (ValueError, SyntaxError) as e:
            it["mark"], it["code"] = "unsupported", f"arithmetic cannot be recomputed ({e})"
            return
        untraced = [x for x in re.findall(r"\d[\d,]*(?:\.\d+)?", expr) if not run.input_traced(x)]
        if n["unit"] == "%":
            target = r * 100
        elif n["unit"] in SCALES:  # inputs are in $ millions
            target = r * 1e6 / SCALES[n["unit"]]
        else:
            target = r
        shown = f"{expr.strip()} = {fmt_like(n, target)}"
        # a named ratio needs the report's definition; inline arithmetic over traced inputs is its own definition
        if name != "arith" and name not in run.definitions:
            it["mark"], it["code"] = "unsupported", f"ratio '{name}' has no definition in the report"
        elif untraced:
            it["mark"], it["code"] = "unsupported", f"inputs not in the facts or filings: {', '.join(untraced)}"
        elif close(n["value"], n["decimals"], target):
            it["mark"], it["code"] = "correct", f"recomputed: {shown}"
        else:
            it["mark"], it["code"] = "wrong", f"recomputed: {shown}"
            it["correction"] = f"{fmt_like(n, target)} [ratio {name}: {expr.strip()}]"
        return

    # a filing line: [Release L45: "quote"], [10-K L123], ranges L12-L14
    m = re.match(r'^L(\d+)(?:\s*-\s*L?(\d+))?\s*(?::\s*"(.*)")?$', body, re.S)
    lines = run.docs.get(kind)
    if not m or lines is None:
        it["mark"], it["code"] = "unsupported", ("citation is not [Doc Lnn: \"quote\"]" if not m
                                                  else f"no saved {kind} text in the run")
        return
    first, last, quote = int(m.group(1)), int(m.group(2) or m.group(1)), m.group(3)
    quote = quote.replace('\\"', '"') if quote else quote  # '(\"Wiz\")' as the model escaped it
    pieces = [_norm(p) for p in re.split(r"\.\.\.|…", quote or "") if _norm(p)]
    inside = 1 <= first <= last <= len(lines)
    if pieces and not (inside and _contains(_norm(" ".join(lines[first - 1:last])), pieces)):
        where = [j + 1 for j, line in enumerate(lines) if _contains(_norm(line), pieces)]
        if len(where) != 1:
            it["mark"], it["code"] = "unsupported", "quoted words are not in the filing" if not where \
                else "quoted words are on several lines, none of them the cited one"
            # the line of that filing that does carry the number, near the cited one first
            near = sorted(range(len(lines)), key=lambda j: abs(j + 1 - first))
            hit = next((j + 1 for j in near if on_line(n, lines[j]) and len(lines[j].split()) > 3), None)
            if hit:
                it["correction"] = f"{n['text']} [{kind} L{hit}: \"{lines[hit - 1][:160]}\"]"
            return
        first = last = where[0]
        it["code"] = f"quote found on L{first}, not the cited line; "
    elif not inside:
        it["mark"], it["code"] = "unsupported", "cited line is not in the filing"
        return
    it["doc"], it["first"], it["last"] = kind, first, last
    cited = " ".join(lines[first - 1:last])
    it["on_line"] = on_line(n, cited)
    it["code"] += "line found in the filing; " + ("number is on the line" if it["on_line"] else "number is NOT on the line")
    it["model"] = "pending"


def model_items(run, items):
    out = []
    for it in items:
        if it.get("model") != "pending":
            continue
        lines = run.docs[it["doc"]]
        i = it["first"] - 1
        before = [f"L{j + 1}: {lines[j][:300]}" for j in range(max(0, i - 4), i) if lines[j].strip()]
        out.append({"id": it["id"], "number_as_written": it["number"]["text"],
                    "report_sentence": _plain(run.report[it["line"]])[:600],
                    "cited_document": it["doc"],
                    "cited_line": f"L{it['first']}: " + " ".join(lines[it["first"] - 1:it["last"]])[:2000],
                    "table_column_header": table_header(lines, i), "lines_before": before})
    return out


def apply_model(run, items, checks):
    by_id = {c["id"]: c for c in checks}
    for it in items:
        if it.get("model") != "pending":
            continue
        c = by_id.get(it["id"])
        if c is None:
            it["model"] = "no answer"
            it["mark"] = "correct" if it["on_line"] else "wrong"
            continue
        it["model"] = f"{c['verdict']}: {c['reason']}"
        if c["verdict"] == "states_it" and it["on_line"]:
            it["mark"] = "correct"
            continue
        it["mark"] = "wrong"
        k, value = c["corrected_line"], c["corrected_value"].strip()
        lines = run.docs[it["doc"]]
        shown = range(max(1, it["first"] - 80), it["last"] + 1)
        nums = numbers_in(value)
        if value and k in shown and nums and all(on_line(x, lines[k - 1]) for x in nums):
            it["correction"] = f"{value} [{it['doc']} L{k}]"
        else:
            it["correction"] = "no correction found in the cited lines"


def edit_line(line, edits):
    for (s, e), text in sorted(edits, reverse=True):
        line = line[:s] + text + line[e:]
    return line


def save_node(folder, name, node_input, node_output):
    """nodes/NN_name.json, numbered after the files already there, so nothing is overwritten."""
    nodes = Path(folder) / "nodes"
    nodes.mkdir(exist_ok=True)
    n = len(list(nodes.glob("*.json"))) + 1
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    path = nodes / f"{n:02d}_{slug}.json"
    path.write_text(json.dumps({"node": name, "input": node_input, "output": node_output}, indent=2,
                               ensure_ascii=False, default=str), encoding="utf-8")
    return path


def verify(folder, call_model):
    """Verify the run in `folder`. `call_model(role, instructions, packet, schema)` returns a dict.
    Writes verified_report.md (a new name if one exists) and returns its path."""
    run = Run(folder)
    items = find_items(run)
    for it in items:
        code_check(run, it)
    packet = {"task": "For each item, decide whether the cited filing line states the number for the period "
                      "and in the definition the report sentence claims; if not, give the right number.",
              "context": {"items": model_items(run, items)},
              "expected_output": "JSON with 'items': one entry per item with 'id', 'verdict' ('states_it' or "
                                 "'does_not'), 'reason', 'corrected_value' (empty if states_it) and "
                                 "'corrected_line' (0 if none)."}
    checks = []
    if packet["context"]["items"]:
        try:
            checks = call_model("Verifier", ROLE_FILE.read_text(encoding="utf-8"), packet, Checks)["items"]
        except Exception as e:  # keep the code checks rather than lose the verification
            print(f"  [Verifier] model check failed ({str(e)[:150]}); marking line items by the code check only")
    apply_model(run, items, checks)
    for it in items:  # an unsupported number the report sources correctly elsewhere takes that source
        if it["mark"] == "wrong" and it["kind"] == "ratio":  # right number, wrong ratio cited beside it
            same = next((o for o in items if o["mark"] == "correct" and o["kind"] == "ratio"
                         and o["number"]["text"] == it["number"]["text"]), None)
            if same:
                it["correction"] = (f"{it['number']['text']} is {same['cite']}; the ratio cited beside it gives "
                                    f"{it['code'].split(' = ')[-1]}")
        if it["mark"] == "unsupported":
            same = next((o for o in items if o["mark"] == "correct" and o["number"]["text"] == it["number"]["text"]
                         and o["kind"] in ("XBRL", "ratio", "price")), None)
            if same:
                it["correction"] = same["cite"]

    edits = {}
    for it in items:
        orig, mark, corr = it["number"]["text"], it["mark"], it["correction"]
        if mark == "correct":
            continue
        if mark == "wrong":
            text = f"~~{orig}~~ **{corr.split(' [')[0]}** [verified: {corr[len(corr.split(' [')[0]):].strip()}]" \
                if corr.startswith(("$", "-", "(")) or corr[:1].isdigit() else f"~~{orig}~~ [verified: wrong; {corr}]"
        elif corr.startswith("["):
            text = f"{orig} [verified: unsupported as written; source {corr}]"
        else:
            text = f"~~{orig}~~ [verified: unsupported; {it['code']}; {corr}]".replace("; ]", "]")
        edits.setdefault(it["line"], []).append((it["span"], text))

    counts = {k: sum(it["mark"] == k for it in items) for k in ("correct", "wrong", "unsupported")}
    cell = lambda s: str(s).replace("|", "/").replace("\n", " ")[:220]
    table = ["", "## Verifier marks", "",
             f"Every number outside the Investment Advisor's thesis: {len(items)} checked; "
             f"correct {counts['correct']}, wrong {counts['wrong']}, unsupported {counts['unsupported']}. "
             f"Code checks tags against facts.json, quoted lines against the saved filing texts and ratios by "
             f"recomputing them; the model checks that each cited line states the number for that period and "
             f"definition.", "",
             "| # | Part | Number | Source cited | Code check | Model check | Mark | Correction (source) |",
             "|---|---|---|---|---|---|---|---|"]
    table += [f"| {it['id']} | {cell(it['part'])} | {cell(it['number']['text'])} | {cell(it['cite'] or 'none')} | "
              f"{cell(it['code'])} | {cell(it['model'])} | **{it['mark']}** | {cell(it['correction'])} |" for it in items]
    body = [edit_line(_plain(line), edits[i]) if i in edits else line for i, line in enumerate(run.report)]
    out_lines = [body[0]] + table + [""] + body[1:]

    path = run.folder / "verified_report.md"
    if path.exists():
        path = run.folder / f"verified_report_{datetime.now():%Y-%m-%d_%H%M%S}.md"
    path.write_text("\n".join(out_lines), encoding="utf-8")
    save_node(run.folder, "Verifier",
              {"report": "report.md", "filing_texts": sorted(DOC_FILES[k] for k in run.docs),
               "facts": "facts.json", "model_packet": packet},
              {"counts": counts, "verified_report": path.name,
               "items": [{k: it.get(k) for k in ("id", "part", "cite", "code", "model", "mark", "correction")}
                         | {"number": it["number"]["text"]} for it in items]})
    print(f"  [Verifier] {len(items)} numbers: correct {counts['correct']}, wrong {counts['wrong']}, "
          f"unsupported {counts['unsupported']}")
    return path
