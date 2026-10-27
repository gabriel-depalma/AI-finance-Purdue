"""XBRL companyfacts extraction and ratio calculation for an equity-research desk.

Data side of the workflow (after Francis & Zhang 2026, Section 3.1): pull SEC
companyfacts JSON, pick one annual (10-K) value per concept for the latest fiscal
year and the year before, and compute a small set of auditable ratios. Each
ratio carries its formula and the arithmetic with the actual numbers.

All functions are pure except `fetch_company_facts`, which calls SEC EDGAR.
Monetary values are kept in USD; the arithmetic strings show $ millions.
"""
from datetime import date

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# (key, label, kind, unit, candidate us-gaap tags in preference order)
CONCEPTS = [
    ("revenue", "Total revenue", "duration", "USD",
     ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
      "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
      "RevenuesNetOfInterestExpense"]),
    ("operating_income", "Operating income", "duration", "USD", ["OperatingIncomeLoss"]),
    ("net_income", "Net income", "duration", "USD",
     ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"]),
    ("eps_diluted", "Diluted EPS", "duration", "USD/shares",
     ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]),
    ("equity", "Stockholders' equity", "instant", "USD",
     ["StockholdersEquity",
      "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
    # LongTermDebt (total, incl. current portion) is deliberately excluded:
    # adding the current portion to it would double-count.
    ("lt_debt", "Long-term debt (noncurrent)", "instant", "USD",
     ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"]),
    ("lt_debt_current", "Current portion of long-term debt", "instant", "USD",
     ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"]),
    ("cfo", "Cash from operations", "duration", "USD",
     ["NetCashProvidedByUsedInOperatingActivities",
      "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]),
    ("capex", "Additions to property and equipment", "duration", "USD",
     ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"]),
]

ANNUAL_DAYS = (350, 380)   # duration length that counts as a fiscal year
PRIOR_GAP = (330, 400)     # distance from fy_end to the prior fiscal year end


def fetch_company_facts(sec, cik):
    """Fetch the companyfacts JSON for `cik` using a `sec_client.SecClient`."""
    return sec.json(COMPANYFACTS_URL.format(cik=int(cik)))


def _days(a, b):
    """Days from ISO date `a` to ISO date `b`."""
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def annual_series(cf, tag, kind, unit):
    """Return {end_date: fact} of annual 10-K values for a us-gaap tag, or None.

    Only forms starting "10-K" are kept. Duration facts need 350-380 days between
    start and end; instant facts must have no start. The preferred `unit` is used,
    or the only unit if the tag reports exactly one. Duplicate end dates keep the
    most recently filed fact (ties broken by accession number).
    """
    units = cf.get("facts", {}).get("us-gaap", {}).get(tag, {}).get("units", {})
    if unit in units:
        used = unit
    elif len(units) == 1:
        used = next(iter(units))
    else:
        return None
    series = {}
    for f in units[used]:
        if not str(f.get("form", "")).startswith("10-K") or "end" not in f or "val" not in f:
            continue
        start = f.get("start")
        if kind == "duration":
            if not start or not ANNUAL_DAYS[0] <= _days(start, f["end"]) <= ANNUAL_DAYS[1]:
                continue
        elif start:
            continue
        fact = {"value": f["val"], "start": start, "end": f["end"], "tag": tag, "unit": used,
                "accn": f.get("accn", ""), "form": f["form"], "filed": f.get("filed", "")}
        old = series.get(f["end"])
        if old is None or (fact["filed"], fact["accn"]) > (old["filed"], old["accn"]):
            series[f["end"]] = fact
    return series or None


def _candidates(cf, kind, unit, tags):
    """Return [(tag, series)] for candidate tags that have annual data, in preference order."""
    return [(t, s) for t in tags if (s := annual_series(cf, t, kind, unit))]


def _choose(found):
    """Pick the candidate whose latest period end is newest; ties go to candidate order."""
    if not found:
        return None, None
    return max(found, key=lambda ts: (max(ts[1]), -found.index(ts)))


def _prior_end(series, fy_end):
    """End date in `series` 330-400 days before fy_end (closest to 365), or None."""
    ends = [e for e in series if PRIOR_GAP[0] <= _days(e, fy_end) <= PRIOR_GAP[1]]
    return min(ends, key=lambda e: abs(_days(e, fy_end) - 365)) if ends else None


def extract(cf):
    """Extract each concept at a common fiscal-year end and the prior year end.

    The revenue concept anchors the fiscal year (net income if revenue is absent).
    For every concept the chosen tag is the candidate with the newest annual period
    end; if it lacks fy_end (or prior_end), the first candidate that has that date
    is used instead and a note is recorded.
    """
    found = {key: _candidates(cf, kind, unit, tags) for key, _, kind, unit, tags in CONCEPTS}
    chosen = {key: _choose(found[key]) for key in found}

    anchor, anchor_note = "revenue", ""
    if chosen["revenue"][0] is None:
        anchor, anchor_note = "net_income", "No annual revenue found; fiscal year anchored on net income."
    tag, series = chosen[anchor]
    fy_end = max(series) if series else None
    prior_end = _prior_end(series, fy_end) if series else None

    concepts = {}
    for key, label, kind, unit, tags in CONCEPTS:
        tag, series = chosen[key]
        notes = [anchor_note] if key == anchor and anchor_note else []
        fy = prior = None
        if tag is None:
            notes.append("No annual 10-K data for any candidate tag.")
        else:
            fy = series.get(fy_end)
            if fy is None and fy_end:
                alt = next(((t, s) for t, s in found[key] if fy_end in s), None)
                if alt:
                    fy = alt[1][fy_end]
                    notes.append(f"{tag} has no value at {fy_end}; used {alt[0]}.")
                else:
                    notes.append(f"No candidate tag has a value at {fy_end}.")
            src_tag = fy["tag"] if fy else tag
            if prior_end:
                prior = dict(found[key]).get(src_tag, {}).get(prior_end)
                if prior is None:
                    alt = next(((t, s) for t, s in found[key] if prior_end in s), None)
                    if alt:
                        prior = alt[1][prior_end]
                        notes.append(f"Prior year ({prior_end}) taken from {alt[0]}.")
        used = fy["tag"] if fy else tag
        used_series = dict(found[key]).get(used)
        concepts[key] = {
            "label": label, "kind": kind, "unit": unit, "candidates": tags,
            "tag": used, "tag_latest": max(used_series) if used_series else None,
            "fy": fy, "prior": prior, "note": " ".join(notes),
        }
    return {"entity": cf.get("entityName"), "fy_end": fy_end, "prior_end": prior_end,
            "anchor": anchor, "concepts": concepts}


# ---------------------------------------------------------------- formatting

def _m(v):
    """USD value as millions with thousands separators, e.g. 391,035.0."""
    return f"{v / 1e6:,.1f}"


def fmt_usd(v):
    """Format USD as "$391,035.0M"; None -> "n/a"."""
    return "n/a" if v is None else f"{'-' if v < 0 else ''}${abs(v) / 1e6:,.1f}M"


def fmt_eps(v):
    """Format a per-share value as "$7.46"; None -> "n/a"."""
    return "n/a" if v is None else f"{'-' if v < 0 else ''}${abs(v):,.2f}"


# ---------------------------------------------------------------- metrics

def _val(facts, key, period="fy"):
    """Value of a concept at "fy" or "prior", or None."""
    fact = facts["concepts"].get(key, {}).get(period)
    return None if fact is None else fact["value"]


def _metric(value, formula, arithmetic="", note=""):
    return {"value": value, "formula": formula, "arithmetic": arithmetic, "note": note}


def _missing(formula, *pairs):
    """Metric with value None and a note naming the missing inputs."""
    names = ", ".join(name for name, v in pairs if v is None)
    return _metric(None, formula, "", f"Missing input: {names}.")


def operating_margin(facts):
    """Operating income / revenue at fy_end."""
    formula = "Operating income / Revenue"
    oi, rev = _val(facts, "operating_income"), _val(facts, "revenue")
    if oi is None or rev is None:
        return _missing(formula, ("operating income", oi), ("revenue", rev))
    if rev == 0:
        return _metric(None, formula, "", "Revenue is zero.")
    v = oi / rev
    return _metric(v, formula, f"{_m(oi)} / {_m(rev)} = {v:.2%}")


def _revenue_growth(facts):
    formula = "(Revenue FY - Revenue prior FY) / Revenue prior FY"
    cur, pri = _val(facts, "revenue"), _val(facts, "revenue", "prior")
    if cur is None or pri is None:
        return _missing(formula, ("revenue FY", cur), ("revenue prior FY", pri))
    if pri == 0:
        return _metric(None, formula, "", "Prior-year revenue is zero.")
    v = (cur - pri) / pri
    return _metric(v, formula, f"({_m(cur)} - {_m(pri)}) / {_m(pri)} = {v:.2%}")


def _roae(facts):
    formula = "Net income / ((Equity FY + Equity prior FY) / 2)"
    ni, e1, e0 = _val(facts, "net_income"), _val(facts, "equity"), _val(facts, "equity", "prior")
    if None in (ni, e1, e0):
        return _missing(formula, ("net income", ni), ("equity FY", e1), ("equity prior FY", e0))
    avg = (e1 + e0) / 2
    if avg == 0:
        return _metric(None, formula, "", "Average equity is zero.")
    v = ni / avg
    note = "Average equity is negative; ratio is not meaningful." if avg < 0 else ""
    return _metric(v, formula, f"{_m(ni)} / (({_m(e1)} + {_m(e0)}) / 2) = {v:.2%}", note)


def _debt_to_equity(facts):
    formula = "(Long-term debt noncurrent + Current portion of LTD) / Equity FY"
    nc, cur, eq = _val(facts, "lt_debt"), _val(facts, "lt_debt_current"), _val(facts, "equity")
    if nc is None and cur is None:
        return _metric(None, formula, "", "No long-term debt reported (noncurrent or current).")
    if eq is None:
        return _missing(formula, ("equity FY", eq))
    notes = [f"{name} not reported; treated as 0." for name, v in
             (("Noncurrent long-term debt", nc), ("Current portion of LTD", cur)) if v is None]
    nc, cur = nc or 0, cur or 0
    if eq == 0:
        return _metric(None, formula, "", "Equity is zero.")
    if eq < 0:
        notes.append("Equity is negative; ratio is not meaningful.")
    v = (nc + cur) / eq
    return _metric(v, formula, f"({_m(nc)} + {_m(cur)}) / {_m(eq)} = {v:.2f}x", " ".join(notes))


def _fcf(facts):
    formula = "Cash from operations - Capital expenditures"
    cfo, capex = _val(facts, "cfo"), _val(facts, "capex")
    if cfo is None or capex is None:
        return _missing(formula, ("cash from operations", cfo), ("capex", capex))
    v = cfo - capex
    return _metric(v, formula, f"{_m(cfo)} - {_m(capex)} = {_m(v)}")


def _pe(facts, price):
    formula = "Share price / Diluted EPS"
    eps = _val(facts, "eps_diluted")
    if price is None:
        return _metric(None, formula, "", "No share price supplied.")
    if eps is None:
        return _metric(None, formula, "", "Diluted EPS not available.")
    if eps <= 0:
        return _metric(None, formula, "", f"Diluted EPS is {eps:.2f} (not positive); P/E not meaningful.")
    v = price / eps
    return _metric(v, formula, f"{price:,.2f} / {eps:.2f} = {v:.2f}x")


def calculate(facts, price=None):
    """Compute desk metrics from `extract` output.

    Returns {name: {"value", "formula", "arithmetic", "note"}} for revenue_growth,
    operating_margin and roae (fractions, shown as %), debt_to_equity and pe (shown
    as x), and fcf (USD, shown in $ millions).
    """
    return {
        "revenue_growth": _revenue_growth(facts),
        "operating_margin": operating_margin(facts),
        "roae": _roae(facts),
        "debt_to_equity": _debt_to_equity(facts),
        "fcf": _fcf(facts),
        "pe": _pe(facts, price),
    }
