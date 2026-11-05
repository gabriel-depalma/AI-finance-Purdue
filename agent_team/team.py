"""The desk rebuilt as an agent team (Lab 04), on CrewAI Flows.

The process is a fixed graph in code, the same on every run:
    Step 1 .. Step 7 -> Research Analyst -> Financial Analyst -> Investment Advisor -> Strategy Manager -> Verifier
A model never chooses the next node. Each role node is one model call with the instructions in its file
under roles/, and it receives only the JSON the graph gives it: {task, context, expected_output}. The
Strategy Manager sends a failing part back to its role once (no role runs more than twice), then code
compiles the report. The Verifier runs on every run and cannot be turned off.

Usage:
    python team.py TICKER [--peers T1,T2] [--price P]
    python team.py --verify-only runs/<saved run folder>

Model: Purdue GenAI Studio (OpenAI-compatible) through CrewAI's LLM, key read as API_KEY from ../.env.
"""
import os
import sys

for _name, _value in (("CREWAI_TRACING_ENABLED", "false"), ("CREWAI_DISABLE_TELEMETRY", "true"),
                      ("OTEL_SDK_DISABLED", "true")):
    os.environ.setdefault(_name, _value)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Literal  # noqa: E402

TEAM = Path(__file__).resolve().parent
sys.path.insert(0, str(TEAM.parent))

from crewai import LLM  # noqa: E402
from crewai.flow.flow import Flow, listen, start  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import research_desk  # noqa: E402
import verifier  # noqa: E402
import xbrl_facts  # noqa: E402
from desk_roles import (ITEM_1, ITEM_1A, ITEM_7, ITEM_7A, _matches_any, _numbers, _plain,  # noqa: E402
                        find_section, numbered)
from sec_client import SecClient, read_env, read_user_agent  # noqa: E402

BASE_URL = "https://genai.rcac.purdue.edu/api"
DEFAULT_MODEL = "gpt-oss:120b"
RA, FA, IA, SM = "Research Analyst", "Financial Analyst", "Investment Advisor", "Strategy Manager"
ROLE_FILES = {RA: "research_analyst.md", FA: "financial_analyst.md", IA: "investment_advisor.md",
              SM: "strategy_manager.md"}
PARTS = {  # report part -> roles that write it
    "Overview": [RA],
    "Market Positioning and Growth Outlook": [RA, FA],
    "Valuation Insights": [FA],
    "Future Developments and Strategic Recommendations": [RA, IA],
    "Investment Recommendation": [IA],
}
MAX_RUNS = 2  # no role runs more than twice in one run

# name -> (definition printed in the report, unit)
RATIOS = {
    "revenue_growth": ("(total revenue FY - total revenue prior FY) / total revenue prior FY", "percent"),
    "operating_margin": ("operating income FY / total revenue FY", "percent"),
    "net_margin": ("net income FY / total revenue FY", "percent"),
    "roae": ("net income FY / ((stockholders' equity FY + stockholders' equity prior FY) / 2)", "percent"),
    "debt_to_equity": ("(noncurrent long-term debt + current portion of debt) / stockholders' equity, FY end", "ratio"),
    "debt_to_equity_noncurrent_only": ("noncurrent long-term debt / stockholders' equity, FY end; the filing "
                                       "reports no current portion", "ratio"),
    "free_cash_flow": ("cash from operations FY - additions to property and equipment FY, $ millions", "usd"),
    "fcf_margin": ("(cash from operations FY - additions to property and equipment FY) / total revenue FY", "percent"),
    "pe": ("share price given with --price / GAAP diluted EPS FY", "ratio"),
    "quarter_revenue_growth": ("(quarter's total revenue - same quarter last year) / same quarter last year, "
                               "from the earnings-release table", "percent"),
}


# ---------------------------------------------------------------- output formats (one per role)

class RAOut(BaseModel):
    overview: str
    market_positioning: str
    future_developments: str
    insight: str


class Definition(BaseModel):
    item: str
    definition_used: str
    alternative: str
    source: str


class FAOut(BaseModel):
    growth_and_peers: str
    valuation_insights: str
    definitions: list[Definition]
    insight: str


class DecidingNumber(BaseModel):
    label: str
    value: str
    appears_in: Literal["Research Analyst", "Financial Analyst"]
    why: str


class IAOut(BaseModel):
    thesis: str
    recommendation: Literal["Buy", "Hold", "Sell"]
    deciding_number: DecidingNumber
    strategic_recommendations: str
    insight: str


class PartReview(BaseModel):
    part: Literal["Overview", "Market Positioning and Growth Outlook", "Valuation Insights",
                  "Future Developments and Strategic Recommendations", "Investment Recommendation"]
    status: Literal["pass", "fail"]
    roles: list[Literal["Research Analyst", "Financial Analyst", "Investment Advisor"]]
    problem: str


class SMOut(BaseModel):
    parts: list[PartReview]


# ---------------------------------------------------------------- model calls

class Models:
    """One CrewAI LLM for every role; counts each role's runs and refuses a third."""

    def __init__(self, model):
        self.model = model
        self.llm = LLM(model=f"openai/{model}", base_url=BASE_URL, timeout=900, max_tokens=16000,
                       api_key=read_env("API_KEY", "Add a line: API_KEY=sk-... (Purdue GenAI Studio: "
                                                   "Settings > Account > API Key)"))
        self.runs = {}

    def call(self, role, instructions, packet, schema):
        if self.runs.get(role, 0) >= MAX_RUNS:
            raise RuntimeError(f"{role} has already run {MAX_RUNS} times in this run")
        self.runs[role] = self.runs.get(role, 0) + 1
        messages = [{"role": "system", "content": instructions},
                    {"role": "user", "content": json.dumps(packet, indent=1, ensure_ascii=False, default=str)}]
        began = time.monotonic()
        out = self.llm.call(messages, response_model=schema)
        if isinstance(out, str):
            out = schema.model_validate_json(re.sub(r"^```(?:json)?\s*|\s*```$", "", out.strip()))
        print(f"  [{role}] done in {time.monotonic() - began:.0f}s", flush=True)
        return out.model_dump()


# ---------------------------------------------------------------- the desk, saving into agent_team/runs

class TeamDesk(research_desk.Desk):
    def folder(self):
        """runs/<Company>_<YYYY-MM-DD_HHMMSS>; a suffix is added rather than reuse an existing folder."""
        if self._folder is None:
            name = re.sub(r"[^A-Za-z0-9]+", "_", self.company["title"]).strip("_") or self.company["ticker"]
            base = TEAM / "runs" / f"{name}_{self.run_time:%Y-%m-%d_%H%M%S}"
            path, k = base, 2
            while path.exists():
                path, k = base.with_name(f"{base.name}_{k}"), k + 1
            path.mkdir(parents=True)
            self._folder = path
        return self._folder


def _usd_m(v):
    return f"{v / 1e6:,.1f}"


def _cells_value(cell):
    m = re.search(r"\(?-?\$?\s?(\d[\d,]*(?:\.\d+)?)\)?", cell)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return -v if "(" in cell or cell.strip().startswith("-") else v


class Team:
    def __init__(self, args):
        self.args = args
        self.desk = TeamDesk(args, SecClient(read_user_agent()))
        self.models = Models(args.model)
        self.out, self.ratios = {}, []

    # -- plumbing
    @staticmethod
    def node(name):
        print(f"\n>>> node: {name}", flush=True)

    def save(self, name, node_input, node_output):
        verifier.save_node(self.desk.folder(), name, node_input, node_output)

    def instructions(self, role):
        return (TEAM / "roles" / ROLE_FILES[role]).read_text(encoding="utf-8")

    def role_call(self, role, packet, schema, label=None):
        self.node(label or role)
        try:
            out = self.models.call(role, self.instructions(role), packet, schema)
        except Exception as e:  # one more run is allowed; the Manager then cannot send this role back
            if self.models.runs.get(role, 0) >= MAX_RUNS:
                raise SystemExit(f"{role} failed: {str(e)[:300]}")
            print(f"  [{role}] call failed ({str(e)[:150]}); running it once more", flush=True)
            out = self.models.call(role, self.instructions(role), packet, schema)
        self.save(label or role, packet, out)
        return out

    # -- the seven steps (the team's tools)
    def step(self, n):
        self.node(f"Step {n}: {research_desk.STEP_TITLES[n]}")
        result = self.desk.run(n)
        d = self.desk
        summary = {1: lambda: result, 2: lambda: d.record.get("filings"),
                   3: lambda: {form: str(p) for form, (p, _, _) in d.saved_texts.items()},
                   4: lambda: result, 5: lambda: result,
                   6: lambda: result, 7: lambda: d.record.get("peers")}[n]()
        self.save(f"Step {n} {research_desk.STEP_TITLES[n]}",
                  {"ticker": self.args.ticker, "peers": self.args.peers, "price": self.args.price,
                   "uses_steps": research_desk.DEPENDS[n]}, summary)
        if n == 7:
            self.after_steps()

    def after_steps(self):
        """Ratios computed in code for the company and each peer, and the release's quarter columns."""
        d = self.desk
        t = d.company["ticker"]
        self.add_ratios(t, d.facts, d.args.price)
        for pt, p in (d.done.get(7) or {}).items():
            self.add_ratios(pt, p["facts"], None)
        self.quarter = self.quarter_figures()
        alts = []
        for key, _, kind, unit, tags in xbrl_facts.CONCEPTS:
            used = d.facts["concepts"][key]["tag"]
            for tag in tags:
                s = xbrl_facts.annual_series(d.company_facts, tag, kind, unit) if tag != used else None
                if s and d.facts["fy_end"] in s:
                    alts.append({"ticker": t, "concept": key, "tag_used": used, "tag": tag,
                                 "end": d.facts["fy_end"], "value": s[d.facts["fy_end"]]["value"]})
        self.alternatives = alts
        d.record["alternative_facts"] = alts
        d.save_record()

    def add_ratios(self, ticker, facts, price):
        def v(key, period="fy"):
            f = facts["concepts"][key][period]
            return None if f is None else f["value"]

        rev, rev0, oi, ni = v("revenue"), v("revenue", "prior"), v("operating_income"), v("net_income")
        eq, eq0, ltd, cur = v("equity"), v("equity", "prior"), v("lt_debt"), v("lt_debt_current")
        cfo, capex, eps = v("cfo"), v("capex"), v("eps_diluted")
        exprs = []
        if rev and rev0:
            exprs.append(("revenue_growth", f"({_usd_m(rev)} - {_usd_m(rev0)}) / {_usd_m(rev0)}"))
        if oi is not None and rev:
            exprs.append(("operating_margin", f"{_usd_m(oi)} / {_usd_m(rev)}"))
        if ni is not None and rev:
            exprs.append(("net_margin", f"{_usd_m(ni)} / {_usd_m(rev)}"))
        if ni is not None and eq and eq0:
            exprs.append(("roae", f"{_usd_m(ni)} / (({_usd_m(eq)} + {_usd_m(eq0)}) / 2)"))
        if ltd is not None and eq:
            exprs.append(("debt_to_equity", f"({_usd_m(ltd)} + {_usd_m(cur)}) / {_usd_m(eq)}") if cur is not None
                         else ("debt_to_equity_noncurrent_only", f"{_usd_m(ltd)} / {_usd_m(eq)}"))
        if cfo is not None and capex is not None:
            exprs.append(("free_cash_flow", f"{_usd_m(cfo)} - {_usd_m(capex)}"))
            if rev:
                exprs.append(("fcf_margin", f"({_usd_m(cfo)} - {_usd_m(capex)}) / {_usd_m(rev)}"))
        if price is not None and eps:
            exprs.append(("pe", f"{price:.2f} / {eps:.2f}"))
        for name, expr in exprs:
            self.add_ratio(ticker, name, expr)

    def add_ratio(self, ticker, name, expr):
        r = verifier.eval_expr(expr)  # computed from the rounded inputs it shows, so it recomputes exactly
        unit = RATIOS[name][1]
        value = {"percent": f"{r * 100:.2f}%", "ratio": f"{r:.2f}x", "usd": f"${r:,.1f}M"}[unit]
        self.ratios.append({"ticker": ticker, "name": name, "definition": RATIOS[name][0], "value": value,
                            "citation": f"[ratio {name}: {expr}]", "number": r})

    @staticmethod
    def column_dates(lines, lineno, ncols):
        """The dates of a release table's value columns, from the nearest header line above the row:
        'June 27, 2026 | March 28, 2026 | ...', years '2025 | 2026', or years with other columns
        ('Selected Products | 2026 | 2025 | % Change'). A column with no date is None. None if no header fits."""
        def cell_date(cell):
            cell = cell.strip()
            if verifier.DATE_CELL.fullmatch(cell):
                return datetime.strptime(cell, "%B %d, %Y")
            return datetime(int(cell), 12, 31) if re.fullmatch(r"(?:19|20)\d\d", cell) else None

        for j in range(lineno - 2, max(-1, lineno - 80), -1):
            cells = _plain(lines[j]).split(" | ")
            dates = [cell_date(c) for c in cells]
            if sum(d is not None for d in dates) < 2:
                continue
            if len(dates) == ncols + 1:  # the header has a label cell of its own
                dates = dates[1:]
            return dates if len(dates) == ncols else None
        return None

    @staticmethod
    def quarter_columns(dates):
        """(this quarter, same quarter last year): the newest dated column, and the column about a year before."""
        dated = [k for k in range(len(dates)) if dates[k] is not None]
        now = max(dated, key=lambda k: (dates[k], -k))  # the first of the newest columns
        prior = next((k for k in dated if 350 <= (dates[now] - dates[k]).days <= 380), None)
        return now, prior

    def quarter_figures(self):
        """The quarter's total revenue and diluted EPS rows from the release tables. The quarter's column
        and the same quarter last year are found from the table's header, never assumed from the order."""
        found = self.desk.done.get(6) or {}
        lines = self.desk.saved_texts.get("8-K", (None, ""))[1].splitlines()
        num = lambda s: re.sub(r"[^\d,.]", "", s)
        out = {}
        rows = sorted(found.get("revenue", []), key=lambda r: not re.match(r"\s*total", r[1], re.I))
        for lineno, text in rows:
            cells = _plain(text).split(" | ")
            if len(cells) < 3 or not re.search(r"revenue|sales", cells[0], re.I):
                continue
            dates = self.column_dates(lines, lineno, len(cells) - 1)
            if dates is None:
                continue
            now, prior = self.quarter_columns(dates)
            if prior is None:
                continue
            now_txt, prior_txt = cells[1 + now].strip(), cells[1 + prior].strip()
            quote = " | ".join(cells[:2 + max(now, prior)])
            out["revenue"] = {"line": lineno, "label": cells[0], "column_dates": [f"{x:%Y-%m-%d}" if x else "-" for x in dates],
                              "quarter_value": "$" + now_txt.lstrip("$") + "M",
                              "same_quarter_last_year_value": "$" + prior_txt.lstrip("$") + "M",
                              "citation": f'[Release L{lineno}: "{quote}"]'}
            self.add_ratio(self.desk.company["ticker"], "quarter_revenue_growth",
                           f"({num(now_txt)} - {num(prior_txt)}) / {num(prior_txt)}")
            out["revenue"]["growth_ratio"] = self.ratios[-1]
            break
        for lineno, text in found.get("eps", []):
            cells = _plain(text).split(" | ")
            label = cells[0].lower()
            if len(cells) < 2 or "non-gaap" in label or "adjusted" in label or not (
                    label.strip().startswith("diluted") or ("per share" in label and "reported" in label)):
                continue
            dates = self.column_dates(lines, lineno, len(cells) - 1)
            k = self.quarter_columns(dates)[0] if dates else 0
            if _cells_value(cells[1 + k]) is None:
                continue
            out["diluted_eps"] = {"line": lineno, "label": cells[0], "quarter_value": cells[1 + k].strip(),
                                  "column": f"{dates[k]:%Y-%m-%d}" if dates else "first column (no date header found)",
                                  "citation": f'[Release L{lineno}: "{" | ".join(cells[:2 + k])}"]',
                                  "note": "check that this row is GAAP, not adjusted"}
            break
        return out

    # -- what each role may see
    def research_context(self):
        d = self.desk
        context = {"company": {"name": d.company["title"], "ticker": d.company["ticker"]}}
        if "10-K" in d.saved_texts:
            lines = d.saved_texts["10-K"][1].splitlines()
            spans = [s for s in (find_section(lines, ITEM_1, ITEM_1A), find_section(lines, ITEM_7, ITEM_7A)) if s]
            context["10-K"] = {"accession": d.filings["10-K"]["accession"], "period": d.filings["10-K"]["report_date"],
                               "sections": [numbered(lines, s) for s in (spans or [(0, len(lines))])]}
        if "8-K" in d.saved_texts:
            lines = d.saved_texts["8-K"][1].splitlines()
            context["earnings_release"] = {"accession": d.filings["8-K"]["accession"],
                                           "date": d.filings["8-K"]["report_date"], "lines": numbered(lines)}
        return context

    def fact_rows(self, ticker, facts):
        rows = []
        for key, c in facts["concepts"].items():
            for period in ("fy", "prior"):
                f = c[period]
                if f is None:
                    if period == "fy":
                        rows.append({"item": c["label"], "period": "FY", "value": "not reported",
                                     "note": c.get("note") or "no XBRL value"})
                    continue
                value = f"${f['value']:.2f}" if key == "eps_diluted" else f"${_usd_m(f['value'])}M"
                rows.append({"item": c["label"], "period": "FY" if period == "fy" else "prior FY",
                             "period_end": f["end"], "value": value,
                             "citation": f"[XBRL {ticker} us-gaap:{f['tag']} {f['end']}]"})
        return rows

    def financial_context(self):
        d = self.desk
        t = d.company["ticker"]
        ratios = lambda tk: [{k: r[k] for k in ("name", "definition", "value", "citation")}
                             for r in self.ratios if r["ticker"] == tk]
        release = d.done.get(6) or {}
        return {
            "company": {"name": d.company["title"], "ticker": t, "fiscal_year_end": d.facts["fy_end"],
                        "prior_fiscal_year_end": d.facts["prior_end"]},
            "share_price": {"value": f"${d.args.price:,.2f}", "citation": "[price given with --price]"}
            if d.args.price is not None else "not given",
            "facts": self.fact_rows(t, d.facts),
            "ratios": ratios(t),
            "alternative_definitions_in_xbrl": [
                {"concept": a["concept"], "tag_used": a["tag_used"], "other_tag": a["tag"],
                 "other_value": f"${_usd_m(a['value'])}M" if a["concept"] != "eps_diluted" else f"${a['value']:.2f}",
                 "citation": f"[XBRL {t} us-gaap:{a['tag']} {a['end']}]"} for a in self.alternatives],
            "quarter_from_earnings_release": {
                "accession": d.filings["8-K"]["accession"] if d.filings.get("8-K") else None,
                "table_rows_found_in_code": self.quarter,
                "revenue_lines": [{"line": f"L{n}", "text": x} for n, x in release.get("revenue", [])],
                "diluted_eps_lines": [{"line": f"L{n}", "text": x} for n, x in release.get("eps", [])]},
            "peers": {pt: {"name": p["company"], "fiscal_year_end": p["facts"]["fy_end"],
                           "facts": self.fact_rows(pt, p["facts"]), "ratios": ratios(pt)}
                      for pt, p in (d.done.get(7) or {}).items()},
        }

    # -- the four roles
    TASKS = {
        RA: ("Write the qualitative research: business model, competitive position, catalysts and recent "
             "developments, from the 10-K sections and the earnings release.",
             "JSON with 'overview' (business model and what the company does), 'market_positioning' (competitive "
             "position and the growth drivers the filings describe) and 'future_developments' (catalysts and "
             "recent developments) and 'insight' (one line starting 'Insight:'). Every claim cited with its quoted "
             "line."),
        FA: ("Write the statement analysis: growth, profitability, returns, leverage, cash flow and valuation from "
             "the facts and ratios, the latest quarter from the release, and the peer comparison.",
             "JSON with 'growth_and_peers' (growth, the quarter and the peer comparison), 'valuation_insights' "
             "(profitability, returns, leverage, free cash flow, P/E), 'definitions' and 'insight' (one line "
             "starting 'Insight:'). Every number copied with its citation."),
        IA: ("Write the investment thesis and recommendation from the two analysts' outputs.",
             "JSON with 'thesis', 'recommendation' (Buy, Hold or Sell), 'deciding_number' (its 'value' copied with "
             "its citation), 'strategic_recommendations' and 'insight' (one line starting 'Insight:')."),
    }
    SCHEMAS = {RA: RAOut, FA: FAOut, IA: IAOut}

    def context_for(self, role):
        if role == RA:
            return self.research_context()
        if role == FA:
            return self.financial_context()
        return {RA: self.out[RA], FA: self.out[FA]}

    def send(self, role, previous=None, findings=None, label=None):
        task, expected = self.TASKS[role]
        context = self.context_for(role)
        if previous is not None:
            task = "Revise your previous output to fix the Strategy Manager's findings. Keep what was not criticized. " \
                   "Original task: " + task
            context = {**context, "your_previous_output": previous, "manager_findings": findings}
        return self.role_call(role, {"task": task, "context": context, "expected_output": expected},
                              self.SCHEMAS[role], label)

    def research_analyst(self):
        self.out[RA] = self.send(RA)

    def financial_analyst(self):
        self.out[FA] = self.send(FA)

    def investment_advisor(self):
        self.out[IA] = self.send(IA)

    # -- the Strategy Manager
    def missing_core_claims(self, text):
        t = self.desk.company["ticker"]
        found = {x for _, x, _ in _numbers(text)}
        r = {x["name"]: x["number"] for x in self.ratios if x["ticker"] == t}
        c = self.desk.facts["concepts"]
        fact = lambda k: c[k]["fy"]["value"] if c[k]["fy"] else None
        q = self.quarter
        claims = {
            "1 total revenue, latest fiscal year": [fact("revenue")],
            "2 revenue growth versus the prior year": [r.get("revenue_growth")],
            "3 operating income and operating margin": [fact("operating_income"), r.get("operating_margin")],
            "4 net income": [fact("net_income")],
            "5 GAAP diluted EPS": [fact("eps_diluted")],
            "6 return on average equity": [r.get("roae")],
            "7 debt to equity (current portion included)": [r.get("debt_to_equity", r.get("debt_to_equity_noncurrent_only"))],
            "8 free cash flow": [r.get("free_cash_flow")],
            "9 the latest quarter's total revenue": [_cells_value(q["revenue"]["quarter_value"]) if "revenue" in q else None],
            "10 the quarter's growth versus the same quarter last year": [r.get("quarter_revenue_growth")],
            "11 the quarter's GAAP diluted EPS": [_cells_value(q["diluted_eps"]["quarter_value"]) if "diluted_eps" in q else None],
        }
        return [name for name, values in claims.items()
                if any(x is not None for x in values) and not all(_matches_any(found, x) for x in values if x is not None)]

    def assemble(self):
        ra, fa, ia = self.out[RA], self.out[FA], self.out[IA]
        dn = ia["deciding_number"]
        strategic = re.sub(r"^\s*#+[^\n]*\n", "", ia["strategic_recommendations"])
        used = set(re.findall(r"\[ratio (\w+):", fa["growth_and_peers"] + fa["valuation_insights"]
                              + json.dumps(ia, ensure_ascii=False)))
        defs = [f"- `{n}`: {RATIOS[n][0]}" for n in RATIOS if n in used]
        defs += [f"- **{x['item']}**: uses {x['definition_used']}; alternative: {x['alternative']} ({x['source']})"
                 for x in fa["definitions"]]
        return {
            "Overview": ra["overview"] + "\n\n" + self.insight_line(ra),
            "Market Positioning and Growth Outlook": ra["market_positioning"] + "\n\n" + fa["growth_and_peers"],
            "Valuation Insights": fa["valuation_insights"] + "\n\n" + self.insight_line(fa)
                + ("\n\n### Definitions used\n\n" + "\n".join(defs) if defs else ""),
            "Future Developments and Strategic Recommendations":
                ra["future_developments"] + "\n\n### Strategic recommendations\n\n" + strategic,
            "Investment Recommendation":
                f"**Recommendation: {ia['recommendation']}**\n\n"
                f"**Deciding number:** {dn['label']}: {dn['value']} (from the {dn['appears_in']}'s output). "
                f"{dn['why']}\n\n{self.insight_line(ia)}\n\n### Investment thesis\n\n{ia['thesis']}",
        }

    @staticmethod
    def insight_line(out):
        """The role's Insight line on one line of its own (the Verifier checks its number like any other)."""
        return re.sub(r"\s+", " ", out.get("insight") or "").strip()

    def release_opening(self):
        """The opening paragraph of the earnings release: its first long prose line with a figure in it."""
        lines = self.desk.saved_texts.get("8-K", (None, ""))[1].splitlines()
        return next((line for i, line in enumerate(lines) if i > 3 and len(line.split()) >= 30
                     and re.search(r"[$%]", line) and " | " not in line), "")

    def insight_problems(self):
        """Code checks on each analyst's Insight line: present, sourced to a filing line or tag, and not just
        a number the release's opening paragraph already gives."""
        opening, problems = self.release_opening(), {}
        for role in (RA, FA, IA):
            line = self.insight_line(self.out[role])
            if not line.startswith("Insight:"):
                problems[role] = "no line starting with 'Insight:'"
                continue
            if not verifier.CITE_RE.search(_plain(line)):
                problems[role] = "the Insight line names no filing line or tag ([10-K L..], [Release L..], [XBRL ..] or [ratio ..])"
                continue
            masked = verifier.mask(_plain(line), [c.span() for c in verifier.CITE_RE.finditer(_plain(line))])
            nums = [verifier.parse_number(m) for m in verifier.NUM_RE.finditer(masked) if verifier.is_financial(m)]
            if nums and opening and all(verifier.on_line(n, opening) for n in nums):
                problems[role] = ("the Insight line only repeats numbers the opening paragraph of the earnings "
                                  "release already gives: " + ", ".join(n["text"] for n in nums))
        return problems

    def strategy_manager(self):
        parts = self.assemble()
        checks = {"empty_parts": [p for p, text in parts.items() if len(text.strip()) < 40],
                  "core_claims_the_financial_analyst_did_not_state": self.missing_core_claims(
                      self.out[FA]["growth_and_peers"] + "\n" + self.out[FA]["valuation_insights"]),
                  "insight_line_problems": self.insight_problems()}
        review = self.role_call(SM, {
            "task": "Check that the five parts are present and consistent, and decide pass or fail for each.",
            "context": {"draft": {p: {"written_by": PARTS[p], "text": parts[p]} for p in PARTS},
                        "checks_run_in_code": checks},
            "expected_output": "JSON with 'parts': one entry per part with 'part', 'status', 'roles' (responsible "
                               "roles if it fails, else empty) and 'problem' (empty if it passes)."}, SMOut)["parts"]
        # the Insight-line checks are decided in code: a part whose Insight line fails them is not correct
        insight_part = {RA: "Overview", FA: "Valuation Insights", IA: "Investment Recommendation"}
        for role, problem in checks["insight_line_problems"].items():
            p = next((x for x in review if x["part"] == insight_part[role]), None)
            if p is None:
                p = {"part": insight_part[role], "status": "fail", "roles": [], "problem": ""}
                review.append(p)
            p["status"] = "fail"
            p["roles"] = sorted(set(p["roles"]) | {role})
            p["problem"] = (p["problem"] + " " if p["problem"] else "") + f"Insight line ({role}): {problem}."
        failing = {}
        for p in review:
            sent = p["roles"] or PARTS[p["part"]]
            print(f"  [{SM}] {p['part']}: {p['status']}" + (f" -> back to {', '.join(sent)}" if p["status"] == "fail" else ""))
            if p["status"] == "fail":
                for role in sent:
                    failing.setdefault(role, []).append({"part": p["part"], "problem": p["problem"]})
        notes = []
        for role in (RA, FA, IA):  # each failing part goes back to its role once
            if role not in failing:
                continue
            if self.models.runs.get(role, 0) >= MAX_RUNS:
                notes.append(f"{role}: already ran {MAX_RUNS} times, not sent back")
                continue
            try:
                self.out[role] = self.send(role, previous=self.out[role], findings=failing[role],
                                           label=f"{role} (revision for the {SM})")
            except (Exception, SystemExit) as e:  # keep the first draft rather than lose the report
                notes.append(f"{role}: revision failed ({str(e)[:200]}); first draft kept")
                print(f"  [{role}] revision failed; keeping the first draft", flush=True)
        self.node(f"{SM} (compiling the report)")
        path = self.compile(self.assemble(), review, checks, notes)
        self.save(f"{SM} compile", {"review": review, "revision_notes": notes}, {"report": str(path)})
        print(f"  report -> {path}")

    def compile(self, parts, review, checks, notes):
        d = self.desk
        read = []
        if "10-K" in d.saved_texts:
            read.append(f"10-K {d.filings['10-K']['accession']} (Item 1 Business, Item 7 MD&A)")
        if "8-K" in d.saved_texts:
            read.append(f"8-K {d.filings['8-K']['accession']} (EX-99.1 earnings release)")
        xbrl = sorted({c[p]["accn"] for c in d.facts["concepts"].values() for p in ("fy", "prior") if c[p]})
        read.append(f"XBRL facts from {', '.join(xbrl)}")
        for pt, p in (d.done.get(7) or {}).items():
            accns = sorted({c["fy"]["accn"] for c in p["facts"]["concepts"].values() if c["fy"]})
            read.append(f"peer {pt} XBRL facts from {', '.join(accns)}")
        ia = self.out[IA]
        lines = [f"Filings read: {'; '.join(read)}", f"Run time: {d.run_time:%Y-%m-%d %H:%M:%S}", "",
                 f"# {d.company['title']} ({d.company['ticker']}): equity research report", "",
                 f"**Recommendation: {ia['recommendation']}**"
                 + (f" · share price used: ${d.args.price:,.2f} [price given with --price]"
                    if d.args.price is not None else ""), ""]
        for i, p in enumerate(PARTS, 1):
            lines += [f"## {i}. {p}", "", parts[p].strip(), ""]
        lines += ["---", "", f"## Appendix: {SM} review of the first draft", "",
                  "Each failing part was sent back to its role once; the report above is the revised version.", "",
                  "| Part | Review | Problem | Sent back to |", "|---|---|---|---|"]
        for p in review:
            sent = ", ".join(p["roles"] or PARTS[p["part"]]) if p["status"] == "fail" else ""
            lines.append(f"| {p['part']} | {p['status']} | {p['problem'].replace('|', '/').replace(chr(10), ' ')} | {sent} |")
        lines += ["", f"- Core claims missing from the first draft (code check): "
                      f"{'; '.join(checks['core_claims_the_financial_analyst_did_not_state']) or 'none'}"]
        lines += [f"- {n}" for n in notes]
        lines += [f"- Model calls per role: " + ", ".join(f"{r} {n}" for r, n in self.models.runs.items()),
                  f"- Model: {self.models.model} via Purdue GenAI Studio (CrewAI LLM)", ""]
        path = d.folder() / "report.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def verify(self):
        self.node("Verifier")
        path = verifier.verify(self.desk.folder(), self.models.call)
        print(f"  verified report -> {path}")


# ---------------------------------------------------------------- the graph

class DeskFlow(Flow):
    """Fixed order; each node only calls the next through @listen, so code alone decides the order."""
    suppress_flow_events = True

    def __init__(self, team, **kwargs):
        super().__init__(**kwargs)
        self.team = team

    @start()
    def step_1(self):
        self.team.step(1)

    @listen(step_1)
    def step_2(self):
        self.team.step(2)

    @listen(step_2)
    def step_3(self):
        self.team.step(3)

    @listen(step_3)
    def step_4(self):
        self.team.step(4)

    @listen(step_4)
    def step_5(self):
        self.team.step(5)

    @listen(step_5)
    def step_6(self):
        self.team.step(6)

    @listen(step_6)
    def step_7(self):
        self.team.step(7)

    @listen(step_7)
    def research_analyst(self):
        self.team.research_analyst()

    @listen(research_analyst)
    def financial_analyst(self):
        self.team.financial_analyst()

    @listen(financial_analyst)
    def investment_advisor(self):
        self.team.investment_advisor()

    @listen(investment_advisor)
    def strategy_manager(self):
        self.team.strategy_manager()

    @listen(strategy_manager)
    def verifier(self):
        self.team.verify()


class VerifyFlow(Flow):
    """--verify-only: the Verifier on a saved run, and nothing else."""
    suppress_flow_events = True

    def __init__(self, folder, models, **kwargs):
        super().__init__(**kwargs)
        self.folder, self.models = folder, models

    @start()
    def verifier(self):
        Team.node("Verifier")
        path = verifier.verify(self.folder, self.models.call)
        print(f"  verified report -> {path}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Equity-research desk as an agent team (CrewAI Flows).")
    parser.add_argument("ticker", nargs="?", help="ticker symbol of the company to research")
    parser.add_argument("--peers", nargs="+", default=[], metavar="TICKER", help="peer tickers, comma- or space-separated")
    parser.add_argument("--price", type=float, help="share price, for P/E")
    parser.add_argument("--verify-only", metavar="RUN_FOLDER", help="run only the Verifier on a saved run folder")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Purdue GenAI Studio model (default {DEFAULT_MODEL})")
    args = parser.parse_args(argv)
    args.peers = [p.strip().upper() for arg in args.peers for p in arg.split(",") if p.strip()]

    if args.verify_only:
        folder = Path(args.verify_only)
        if not (folder / "report.md").exists() or not (folder / "facts.json").exists():
            raise SystemExit(f"{folder} is not a saved run (it needs report.md and facts.json)")
        VerifyFlow(folder, Models(args.model)).kickoff()
        return
    if not args.ticker:
        parser.error("give a ticker, or --verify-only with a saved run folder")
    team = Team(args)
    DeskFlow(team).kickoff()
    print(f"\nOutput folder: {team.desk.folder()}")


if __name__ == "__main__":
    main()
