"""The four roles of Francis & Zhang (2026), Figure 1, on top of the desk's seven data steps.

The Strategy Manager sends each role one JSON message {task, context, expected_output}. The
Research Analyst, Financial Analyst and Investment Advisor are separate model calls with their
own instructions and see only that message. The Manager checks the five report parts (helped by
checks run in code), sends any failing part back to its role once, and compiles the report.

Provider: Purdue GenAI Studio, whose API is OpenAI-compatible, called through the official OpenAI
Python SDK with the key read as API_KEY from .env.
"""
import ast
import json
import operator
import re
import time

import openai

import xbrl_facts
from sec_client import read_env

BASE_URL = "https://genai.rcac.purdue.edu/api"
DEFAULT_MODEL = "gpt-oss:120b"
MAX_TOKENS = 16000
MAX_TOOL_TURNS = 30

RA, FA, IA = "Research Analyst", "Financial Analyst", "Investment Advisor"
PARTS = {  # report part -> roles that write it
    "Overview": [RA],
    "Market Positioning and Growth Outlook": [RA, FA],
    "Valuation Insights": [FA],
    "Future Developments and Strategic Recommendations": [RA, IA],
    "Investment Recommendation": [IA],
}

# ---------------------------------------------------------------- role instructions

COMMON = """You are one role on a four-role equity-research team. You receive one JSON message from \
the Strategy Manager with "task", "context" and "expected_output". Use only what is in "context": you \
have no other information about the company, its market or its share price, and you do not fill gaps \
from memory. If the context lacks something, say that it is not in the material. Write precise analyst \
prose in Markdown inside the JSON string fields (use "###" subheadings at most, never "#" or "##"). \
Return exactly the JSON object that expected_output describes."""

RA_SYSTEM = COMMON + """

You are the Research Analyst. You cover the qualitative analysis: the business model, the competitive \
position, catalysts and recent developments. Your sources are the 10-K's Item 1 (Business) and Item 7 \
(Management's Discussion and Analysis) and the earnings release, given as numbered lines ("L123: text").

Every claim quotes its line. Put a citation right after each claim, in exactly this form:
[10-K L123: "words copied exactly from that line"] or [Release L45: "words copied exactly from that line"]
Copy the quoted words character for character from that single line; a span of 3 to 20 words is best. \
A sentence without a citation is not allowed. Code checks every quote against the line it cites and \
reports mismatches to the Strategy Manager.

Do not calculate anything. Repeat a number only as it is written in a line you quote."""

FA_SYSTEM = COMMON + """

You are the Financial Analyst. You write the statement analysis from the data in the context: the \
XBRL facts, the ratios with their arithmetic, the quarter's lines from the earnings release, and the peers.

Rules:
- Every number you write comes from the context or from a calculator result, with its source in \
brackets right after it. For an XBRL fact: [us-gaap:Tag, period, form accession]. For a release line: \
[Release L45]. For a ratio from the context or the calculator: [calculator]. A number cited to a \
release or 10-K line must appear on that line; code checks it.
- A fact whose value is "not reported" is missing, not zero: never type 0 for it. Leave it out of the \
ratio and say in the text that the filing does not report it.
- Every ratio is shown with its arithmetic, copied from the "arithmetic" field of the ratio or of the \
calculator result.
- Never compute in your head, not even a difference, a sum or a percentage, and do not round or \
convert units yourself. Before you write, call the calculator tool once for every number you need \
that is not already in the context (for example peers' growth rates, peers' leverage, conversion or \
coverage ratios), using the fact ids in the context. Write "[calculator]" only after a number that \
the calculator tool returned to you. Code checks every number in your output against the data and the \
calculator results and reports any that do not match.
- Wherever the material offers two definitions of one item, state which one you use and name the \
other, and record the case in "definitions". Typical cases: a total revenue line and a net sales line \
for the same period in the release (the total can include fees or other income); GAAP and adjusted \
EPS; two XBRL tags with values for one concept (listed in alternative_definitions_in_xbrl). Record only \
items where two definitions exist.
- State at least these eleven core claims, each with its source: (1) total revenue for the latest \
fiscal year; (2) revenue growth versus the prior year; (3) operating income and operating margin; \
(4) net income; (5) GAAP diluted EPS; (6) return on average equity; (7) debt to equity with the current \
portion included; (8) free cash flow; (9) the latest quarter's total revenue; (10) the quarter's \
growth versus the same quarter last year; (11) the quarter's GAAP diluted EPS (an adjusted or \
non-GAAP EPS does not count). They are a floor, not the whole analysis: keep the rest of your analysis \
(peers, extra ratios, valuation) around them. Code checks that each one is stated.
- "growth_and_peers" and "valuation_insights" are analyst prose in Markdown: sentences and bullet \
points that interpret the numbers, never nested JSON.
- When you have all the calculator results you need, reply with only the JSON object.
- Peers' fiscal years can end on other dates than the company's; say so when you compare them."""

IA_SYSTEM = COMMON + """

You are the Investment Advisor. Your context is only the Research Analyst's and the Financial \
Analyst's outputs. Use nothing else: no market knowledge, no price, estimate or fact that is not in them.

Write the investment thesis and a recommendation of Buy, Hold or Sell. Name the one number that \
decides the call: copy it exactly as written in one of the two outputs, say which output it is in, \
and explain why it decides the call. Every fact or number you use must appear in the two outputs; keep \
their citations in brackets. Then write short strategic recommendations: what an investor should watch \
and what would change the call."""

SM_SYSTEM = """You are the Strategy Manager of a four-role equity-research team (Research Analyst, \
Financial Analyst, Investment Advisor). You review a draft report in five parts: Overview; Market \
Positioning and Growth Outlook; Valuation Insights; Future Developments and Strategic Recommendations; \
Investment Recommendation. Each part lists the roles that wrote it. You also receive the results of \
checks run in code: quotes verified against the lines they cite, numbers checked against the lines \
they cite, numbers traced to the data and the calculator, and the deciding number traced to the \
Advisor's inputs.

For each part, decide pass or fail. A part fails if it is missing or thin; if it contradicts another \
part (a number, a fact, the direction of the argument, or a recommendation that does not follow from \
the thesis and the deciding number); or if the code checks show a material problem in it (quotes that \
do not match, numbers not on the line they cite, numbers that cannot be traced, claims without \
citations). If the checks list core claims \
the Financial Analyst did not state, fail Valuation Insights (or Market Positioning and Growth Outlook \
for the quarter's figures) and send it to the Financial Analyst. Style alone is not a failure. \
For each failing part, name the responsible role(s) and state the problem precisely enough for that \
role to fix it in one revision. Return exactly the JSON object that expected_output describes."""


def _schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


TEXT = {"type": "string"}
RA_SCHEMA = _schema({"overview": TEXT, "market_positioning": TEXT, "future_developments": TEXT})
FA_SCHEMA = _schema({
    "growth_and_peers": TEXT, "valuation_insights": TEXT,
    "definitions": {"type": "array", "items": _schema(
        {"item": TEXT, "definition_used": TEXT, "alternative": TEXT, "source": TEXT})},
})
IA_SCHEMA = _schema({
    "thesis": TEXT,
    "recommendation": {"type": "string", "enum": ["Buy", "Hold", "Sell"]},
    "deciding_number": _schema({"label": TEXT, "value": TEXT,
                                "appears_in": {"type": "string", "enum": [RA, FA]}, "why": TEXT}),
    "strategic_recommendations": TEXT,
})
SM_SCHEMA = _schema({"parts": {"type": "array", "items": _schema({
    "part": {"type": "string", "enum": list(PARTS)},
    "status": {"type": "string", "enum": ["pass", "fail"]},
    "roles": {"type": "array", "items": {"type": "string", "enum": [RA, FA, IA]}},
    "problem": TEXT,
})}})

UNITS = ["percent", "ratio", "usd", "per_share", "number"]
CALCULATOR_TOOL = {"type": "function", "function": {
    "name": "calculator",
    "description": ("Evaluate arithmetic over the fact ids listed in the context's 'facts' and 'ratios' "
                    "(and plain numbers) with + - * / and parentheses. Returns the result and the "
                    "arithmetic with the values substituted. Use it for every number you need that is "
                    "not already in the context. Ratios are fractions: for a percentage write e.g. "
                    "(A - B) / B with unit 'percent' and never multiply by 100 yourself."),
    "parameters": _schema({
        "label": {"type": "string", "description": "What the result is, e.g. 'Net margin, FY'."},
        "expression": {"type": "string",
                       "description": "e.g. (XYZ_revenue_fy - XYZ_revenue_prior) / XYZ_revenue_prior"},
        "unit": {"type": "string", "enum": UNITS},
    }),
}}

# ---------------------------------------------------------------- calculator


OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _id(ticker, *parts):
    return "_".join([re.sub(r"\W", "_", ticker.upper()), *parts])


def _display(value, unit):
    if value is None:
        return "n/a"
    return {"percent": lambda v: f"{v * 100:.2f}%", "ratio": lambda v: f"{v:.2f}x",
            "usd": xbrl_facts.fmt_usd, "per_share": lambda v: f"${v:.2f}",
            "number": lambda v: f"{v:,.4g}"}[unit](value)


class Calculator:
    """Safe arithmetic over named facts; every call is logged with its arithmetic."""

    def __init__(self):
        self.values, self.units, self.sources, self.log = {}, {}, {}, []

    def add(self, name, value, unit, source):
        if value is not None:
            self.values[name], self.units[name], self.sources[name] = value, unit, source

    def _eval(self, node):
        if isinstance(node, ast.Expression):
            return self._eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            if node.value != int(node.value) or abs(node.value) > 1000:
                raise ValueError(f"type fact ids, not values: {node.value} is not allowed "
                                 "(only whole-number constants up to 1000, such as 2 or 100)")
            if node.value == 0:
                raise ValueError("0 is not allowed: a fact that is not reported is missing, not zero; "
                                 "leave it out and say the filing does not report it")
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in self.values:
                raise ValueError(f"unknown fact id {node.id!r}")
            return self.values[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = self._eval(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            a, b = self._eval(node.left), self._eval(node.right)
            if isinstance(node.op, ast.Div) and b == 0:
                raise ValueError("division by zero")
            return OPERATORS[type(node.op)](a, b)
        raise ValueError("only numbers, fact ids, + - * / and parentheses are allowed")

    def run(self, label, expression, unit):
        value = self._eval(ast.parse(expression, mode="eval"))
        names = sorted(set(re.findall(r"[A-Za-z_]\w*", expression)) & set(self.values), key=len, reverse=True)
        arithmetic = expression
        for n in names:
            arithmetic = re.sub(rf"\b{n}\b", _display(self.values[n], self.units[n]), arithmetic)
        entry = {"label": label, "expression": expression, "arithmetic": f"{arithmetic} = {_display(value, unit)}",
                 "value": value, "unit": unit, "result": _display(value, unit),
                 "inputs": {n: self.sources[n] for n in names}}
        self.log.append(entry)
        return entry

    def tool_result(self, call):
        """Run one tool call and return (label, shown result, tool message). The model's arguments
        are untrusted: validate them before evaluating."""
        try:
            args = json.loads(call.function.arguments or "{}")
            if call.function.name != "calculator" or not isinstance(args, dict):
                raise ValueError("the only tool is 'calculator' with a JSON object of arguments")
            if not all(isinstance(args.get(k), str) and args.get(k) for k in ("label", "expression", "unit")):
                raise ValueError("arguments need non-empty strings label, expression and unit")
            if args["unit"] not in UNITS:
                raise ValueError(f"unit must be one of {UNITS}")
            entry = self.run(args["label"], args["expression"], args["unit"])
            content = json.dumps({k: entry[k] for k in ("label", "result", "arithmetic", "inputs")})
            label, shown = args["label"], entry["result"]
        except (ValueError, SyntaxError, json.JSONDecodeError) as e:
            content, label, shown = f"Error: {e}", "?", f"error ({e})"
        return label, shown, {"role": "tool", "tool_call_id": call.id, "content": content}


# ---------------------------------------------------------------- source material

ITEM_1 = re.compile(r"^\s*item\s*1\s*[.:\-–—]?\s*(?:\|\s*)?business\b", re.I)
ITEM_1A = re.compile(r"^\s*item\s*1a\b", re.I)
ITEM_7 = re.compile(r"^\s*item\s*7\s*[.:\-–—]?\s*(?:\|\s*)?management", re.I)
ITEM_7A = re.compile(r"^\s*item\s*(?:7a|8)\b", re.I)


def find_section(lines, start_re, end_re):
    """(start, end) 0-based line span of the longest start..end run, skipping the table of contents."""
    best = None
    for s, line in enumerate(lines):
        if start_re.match(line):
            e = next((j for j in range(s + 1, len(lines)) if end_re.match(lines[j])), None)
            if e is not None and (best is None or e - s > best[1] - best[0]):
                best = (s, e)
    return best


def numbered(lines, span=None):
    s, e = span or (0, len(lines))
    return "\n".join(f"L{i + 1}: {lines[i]}" for i in range(s, e) if lines[i].strip())


# ---------------------------------------------------------------- checks run in code

BRACKET_RE = re.compile(r"\[[^\[\]]*(?:10-K|Release)[^\[\]]*\]")
CITE_RE = re.compile(r'(10-K|Release)\s+L(\d+)(?:\s*-\s*L?(\d+))?\s*:\s*"([^"]+)"')
NUM_RE = re.compile(r"(?<![\w.])\(?-?\$?\d[\d,]*(?:\.\d+)?")


def _plain(s):
    """Fold the typographic characters models emit (non-breaking hyphens, curly quotes, NBSP)."""
    for chars, to in (("‐‑‒–—−", "-"), ("“”„", '"'), ("’‘", "'"), ("  ", " "), ("【", "["), ("】", "]")):
        s = re.sub(f"[{chars}]", to, s)
    return s


def _norm(s):
    s = _plain(s).replace("|", " ").lower()
    return re.sub(r"\s+", " ", s).strip(" .,;:")


def _contains(haystack, pieces):
    """True if every fragment occurs in `haystack`, in order."""
    pos = 0
    for p in pieces:
        pos = haystack.find(p, pos)
        if pos < 0:
            return False
        pos += len(p)
    return bool(pieces)


def check_quotes(texts, docs, spans):
    """Verify each [doc Lnn: "quote"] (or Lnn-Lmm, several per bracket, "..." for omissions) against
    the cited lines. A quote found word for word on another line of the material counts as verified
    with a corrected line number. Also lists paragraphs that carry no citation at all."""
    ok, bad, uncited, corrections = 0, [], [], []
    for text in map(_plain, texts):
        for bracket in BRACKET_RE.findall(text):
            for m in CITE_RE.finditer(bracket):
                doc, first, last, quote = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2)), m.group(4)
                lines = docs.get(doc) or []
                doc_spans = spans.get(doc, [(0, len(lines))])
                inside = all(any(s <= n - 1 < e for s, e in doc_spans)
                             for n in (first, last)) and 1 <= first <= last <= len(lines)
                cited = _norm(" ".join(lines[first - 1:last])) if inside else ""
                pieces = [_norm(p) for p in re.split(r"\.\.\.|…", quote) if _norm(p)]
                if inside and _contains(cited, pieces):
                    ok += 1
                    continue
                elsewhere = [i + 1 for s, e in doc_spans for i in range(s, e) if _contains(_norm(lines[i]), pieces)]
                if len(elsewhere) == 1:
                    ok += 1
                    fixed = re.sub(r"L\d+(?:\s*-\s*L?\d+)?", f"L{elsewhere[0]}", m.group(0), count=1)
                    corrections.append((m.group(0), fixed))
                else:
                    bad.append(f"{m.group(0)[:160]} -> " + ("line not in the material" if not inside
                                                            else f"line says: {cited[:160]!r}"))
        for para in re.split(r"\n\s*\n|\n(?=\s*[-*] )", text):
            body = para.strip()
            if len(body) > 60 and not body.startswith("#") and not BRACKET_RE.search(body):
                uncited.append(body[:160])
    return {"quotes_verified": ok, "quotes_failed": bad, "paragraphs_without_citation": uncited,
            "line_numbers_corrected": corrections}


LINE_CITE_RE = re.compile(r"(10-K|Release)\s+L(\d+)(?:\s*-\s*L?(\d+))?")


def check_line_numbers(texts, docs):
    """Each number written just before a [doc Lnn] citation must appear on the cited line(s), at any
    printed scale. The clause runs back to the previous citation or the end of the previous sentence."""
    bad = []
    for text in map(_plain, texts):
        start = 0
        for m in BRACKET_RE.finditer(text):
            clause = re.split(r"(?<!\d)[.;]\s|\n", text[start:m.start()])[-1]
            start = m.end()
            cited = []
            for c in LINE_CITE_RE.finditer(m.group(0)):
                lines = docs.get(c.group(1)) or []
                first, last = int(c.group(2)), int(c.group(3) or c.group(2))
                cited += lines[first - 1:last] if 1 <= first <= last <= len(lines) else []
            if not cited:
                continue
            allowed = allowed_numbers([], cited)
            for tok, x, d in _numbers(clause):
                if not _matches(x, d, allowed):
                    bad.append(f"{tok.strip()} {m.group(0)[:80]} -> not on the cited line")
    return bad


def _numbers(text):
    text = _plain(text)
    text = re.sub(r"\[[^\]]*\]", " ", text)                # citations carry tags, accessions, line numbers
    text = re.sub(r"\b\d{10}-\d{2}-\d{6}\b", " ", text)    # accession numbers
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", text)     # dates
    out = []
    for tok in NUM_RE.findall(text):
        tok = tok.lstrip("(")
        raw = tok.strip("$").replace(",", "")
        if raw.startswith("-"):
            raw = raw[1:]
        try:
            x = float(raw)
        except ValueError:
            continue
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        if decimals == 0 and "$" not in tok and (x <= 31 or 1900 <= x <= 2100):
            continue                                       # counts, quarters, years
        out.append((tok, x, decimals))
    return out


def _matches(x, decimals, allowed):
    tol = 0.5 * 10 ** -decimals + 1e-9
    return any(abs(abs(a) - x) <= tol for a in allowed)


def _matches_any(found, value):
    """True if any number written in a text equals `value` at some printed scale, after rounding."""
    targets = {value, value / 1e3, value / 1e6, value / 1e9, value * 100}
    return any(abs(a) >= 10 ** -_decimals(x) and abs(abs(a) - x) <= 0.5 * 10 ** -_decimals(x) + 1e-9
               for a in targets for x in found)  # ignore scales below the printed precision ($0.0M is not 0.00138)


def _decimals(x):
    s = repr(float(x))
    return len(s.split(".")[1].rstrip("0")) if "." in s else 0


def allowed_numbers(values, texts=()):
    """Every scale an analyst might print a source number in, plus numbers written in source text."""
    allowed = set()
    for v in values:
        if v is None:
            continue
        allowed.update({v, v / 1e3, v / 1e6, v / 1e9, v * 100})
    for t in texts:  # a number in the source text may be restated in thousands, millions or billions
        allowed.update(y for _, x, _ in _numbers(t) for y in (x, x / 1e3, x / 1e6, x * 1e3, x * 1e6))
    return allowed


def untraced(texts, allowed):
    return sorted({tok.strip() for t in texts for tok, x, d in _numbers(t) if not _matches(x, d, allowed)})


# ---------------------------------------------------------------- the desk's roles


class Roles:
    def __init__(self, desk, model=DEFAULT_MODEL):
        self.desk = desk
        self.model = model
        self.client = openai.OpenAI(base_url=BASE_URL, timeout=900, api_key=read_env(
            "API_KEY", "Add a line: API_KEY=sk-... (Purdue GenAI Studio: Settings > Account > API Key)"))
        self.calc = Calculator()
        self.tokens = {"input": 0, "output": 0}

    @staticmethod
    def _problem(data, schema, path="reply"):
        """First way `data` breaks `schema` (types, required fields, enums), or None if it fits."""
        kind = schema.get("type")
        if kind == "object":
            if not isinstance(data, dict):
                return f"{path} must be a JSON object"
            for key in schema["required"]:
                if key not in data:
                    return f"{path} is missing '{key}'"
                problem = Roles._problem(data[key], schema["properties"][key], f"{path}.{key}")
                if problem:
                    return problem
        elif kind == "array":
            if not isinstance(data, list):
                return f"{path} must be a JSON array"
            for i, item in enumerate(data):
                problem = Roles._problem(item, schema["items"], f"{path}[{i}]")
                if problem:
                    return problem
        elif kind == "string":
            if not isinstance(data, str):
                return f"{path} must be a string"
            if "enum" in schema and data not in schema["enum"]:
                return f"{path} must be one of {schema['enum']}"
            if len(data) > 40 and data.lstrip().startswith(("{", "[{", "```")):
                return f"{path} must be Markdown prose (sentences and bullet points), not JSON or code"
        return None

    @classmethod
    def _parse(cls, text, schema):
        """(data, None) if the reply is a JSON object that fits the schema, else (None, problem)."""
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None, "the reply is not valid JSON"
        problem = cls._problem(data, schema)
        return (None, problem) if problem else (data, None)

    # -- one role call: a separate request with the role's own instructions and only the packet
    def call(self, role, system, packet, schema, tools=None):
        print(f"  [{role}] running ...", flush=True)
        start = time.monotonic()
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(packet, indent=1, default=str)}]
        retried = False
        for _ in range(MAX_TOOL_TURNS):
            request = dict(model=self.model, messages=messages, max_tokens=MAX_TOKENS)
            if tools:  # this server ignores tools when a response format is forced, so the JSON is
                request["tools"] = tools  # asked for in the instructions and checked by _parse instead
            else:
                request["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "output", "schema": schema, "strict": True}}
            try:
                response = self.client.chat.completions.create(**request)
            except openai.BadRequestError as e:  # this server sometimes rejects a valid request once
                print(f"      server rejected the request ({e.status_code}: {e.response.text[:200]!r}); "
                      "retrying once", flush=True)
                time.sleep(5)
                response = self.client.chat.completions.create(**request)
            if response.usage:
                self.tokens["input"] += response.usage.prompt_tokens or 0
                self.tokens["output"] += response.usage.completion_tokens or 0
            choice = response.choices[0]
            message = choice.message
            if message.tool_calls:
                messages.append({"role": "assistant", "content": message.content or "",
                                 "tool_calls": [tc.model_dump() for tc in message.tool_calls]})
                for tc in message.tool_calls:
                    label, shown, result = self.calc.tool_result(tc)
                    print(f"      calculator: {label} -> {shown}", flush=True)
                    messages.append(result)
                continue
            if choice.finish_reason == "length":
                raise SystemExit(f"{role}: the reply hit max_tokens ({MAX_TOKENS}) before finishing.")
            data, problem = self._parse(message.content, schema)
            if data is None and tools:
                # Phase 2: the server enforces the JSON format only without tools, so hand the draft and
                # the calculator results to one more request that has the format and no tools.
                print(f"      reply did not fit the format ({problem}); formatting it with the schema enforced",
                      flush=True)
                results = [{k: e[k] for k in ("label", "result", "arithmetic")} for e in self.calc.log]
                format_messages = messages[:2] + [{"role": "user", "content": (
                    "Calculator results you obtained:\n" + json.dumps(results, indent=1, ensure_ascii=False)
                    + "\n\nYour draft:\n" + (message.content or "")
                    + "\n\nNow reply with the final JSON object for expected_output, keeping the draft's "
                      "content, numbers and citations. Each text field is analyst prose in Markdown: full "
                      "sentences and bullet points, with each number followed by its source in brackets and "
                      "each ratio by its arithmetic. Never put JSON inside a text field.")}]
                for attempt in range(2):
                    final = self.client.chat.completions.create(
                        model=self.model, max_tokens=MAX_TOKENS, messages=format_messages,
                        response_format={"type": "json_schema", "json_schema": {
                            "name": "output", "schema": schema, "strict": True}})
                    if final.usage:
                        self.tokens["input"] += final.usage.prompt_tokens or 0
                        self.tokens["output"] += final.usage.completion_tokens or 0
                    data, problem = self._parse(final.choices[0].message.content, schema)
                    if data is not None:
                        break
                    print(f"      formatted reply still wrong ({problem})"
                          + ("; asking once more" if attempt == 0 else ""), flush=True)
                    format_messages = format_messages + [
                        {"role": "assistant", "content": final.choices[0].message.content or ""},
                        {"role": "user", "content": f"That does not fit: {problem}. Reply again."}]
                if data is None:
                    raise SystemExit(f"{role}: the model did not return the JSON object it was asked for ({problem}).")
            if data is not None:
                print(f"  [{role}] done in {time.monotonic() - start:.0f}s", flush=True)
                return data
            if retried:
                raise SystemExit(f"{role}: the model did not return the JSON object it was asked for ({problem}).")
            retried = True  # open models occasionally drift from the format; ask once more
            print(f"      reply did not fit the format ({problem}); asking once more", flush=True)
            messages += [{"role": "assistant", "content": message.content or ""},
                         {"role": "user", "content": f"Your reply does not fit the required format: {problem}. "
                                                     "Reply again with only the JSON object, matching this "
                                                     f"schema exactly: {json.dumps(schema)}"}]
        raise SystemExit(f"{role}: more than {MAX_TOOL_TURNS} calculator turns.")

    # -- material each role may see
    def research_context(self):
        d = self.desk
        context = {"company": {"name": d.company["title"], "ticker": d.company["ticker"]}}
        self.docs, self.spans = {}, {}
        if "10-K" in d.saved_texts:
            lines = d.saved_texts["10-K"][1].splitlines()
            self.docs["10-K"] = lines
            spans = [s for s in (find_section(lines, ITEM_1, ITEM_1A), find_section(lines, ITEM_7, ITEM_7A)) if s]
            if not spans:  # headings not recognised: hand over the whole 10-K rather than nothing
                spans = [(0, len(lines))]
            self.spans["10-K"] = spans
            context["10-K"] = {"accession": d.filings["10-K"]["accession"],
                               "period": d.filings["10-K"]["report_date"],
                               "sections": [numbered(lines, s) for s in spans]}
        if "8-K" in d.saved_texts:
            lines = d.saved_texts["8-K"][1].splitlines()
            self.docs["Release"] = lines
            self.spans["Release"] = [(0, len(lines))]
            context["earnings_release"] = {"accession": d.filings["8-K"]["accession"],
                                           "date": d.filings["8-K"]["report_date"], "lines": numbered(lines)}
        return context

    def _fact_rows(self, ticker, facts):
        rows = []
        for key, c in facts["concepts"].items():
            unit = "per_share" if key == "eps_diluted" else "usd"
            for period in ("fy", "prior"):
                f = c[period]
                if f is None:
                    if period == "fy":  # say so, so the analyst does not fill in 0
                        rows.append({"id": None, "label": c["label"], "period": "FY", "value": "not reported",
                                     "source": c.get("note") or "no XBRL value"})
                    continue
                name = _id(ticker, key, period)
                span = f"{f['start']} to {f['end']}" if f.get("start") else f"at {f['end']}"
                source = f"us-gaap:{f['tag']}, {span}, {f['form']} {f['accn']} filed {f['filed']}"
                self.calc.add(name, f["value"], unit, source)
                rows.append({"id": name, "label": c["label"], "period": "FY" if period == "fy" else "prior FY",
                             "value": _display(f["value"], unit), "source": source})
        return rows

    def financial_context(self):
        d = self.desk
        t = d.company["ticker"]
        calc = d.done.get(5) or {}
        ratios = []
        for key, m in calc.items():
            unit = "usd" if key == "fcf" else "ratio" if key in ("debt_to_equity", "pe") else "percent"
            self.calc.add(_id(t, key), m["value"], unit, f"step 5 calculator: {m['formula']}")
            ratios.append({"id": _id(t, key), "name": key, "formula": m["formula"],
                           "arithmetic": m["arithmetic"] or None, "value": _display(m["value"], unit),
                           "note": m.get("note") or None})
        if d.args.price is not None:
            self.calc.add("price", d.args.price, "per_share", "share price given with --price")
        alternatives = []
        for key, _, kind, unit, tags in xbrl_facts.CONCEPTS:
            used = d.facts["concepts"][key]["tag"]
            for tag in tags:
                s = xbrl_facts.annual_series(d.company_facts, tag, kind, unit) if tag != used else None
                if s and d.facts["fy_end"] in s:
                    f = s[d.facts["fy_end"]]
                    alternatives.append({"concept": key, "tag_used": used, "other_tag": tag,
                                         "other_value": _display(f["value"], "per_share" if unit != "USD" else "usd"),
                                         "source": f"{f['form']} {f['accn']}"})
        release = d.done.get(6) or {}
        peers = {}
        for pt, p in (d.done.get(7) or {}).items():
            om = p["operating_margin"]
            self.calc.add(_id(pt, "operating_margin"), om["value"], "percent", f"{pt} operating margin, step 7")
            peers[pt] = {"name": p["company"], "fiscal_year_end": p["facts"]["fy_end"],
                         "facts": self._fact_rows(pt, p["facts"]),
                         "operating_margin": {"id": _id(pt, "operating_margin"), "formula": om["formula"],
                                              "arithmetic": om["arithmetic"], "value": _display(om["value"], "percent")}}
        return {
            "company": {"name": d.company["title"], "ticker": t, "fiscal_year_end": d.facts["fy_end"],
                        "prior_fiscal_year_end": d.facts["prior_end"]},
            "share_price": d.args.price,
            "facts": self._fact_rows(t, d.facts),
            "alternative_definitions_in_xbrl": alternatives,
            "ratios": ratios,
            "quarter_from_earnings_release": {
                "accession": d.filings["8-K"]["accession"] if d.filings.get("8-K") else None,
                "revenue_lines": [{"line": f"Release L{n}", "text": x} for n, x in release.get("revenue", [])],
                "diluted_eps_lines": [{"line": f"Release L{n}", "text": x} for n, x in release.get("eps", [])]},
            "peers": peers,
            "calculator": "Call the calculator tool with expressions over the 'id' values above (and 'price').",
        }

    # -- checks run in code, shown to the Manager and printed in the report appendix
    def checks(self, ra, fa, ia):
        out = check_quotes([ra["overview"], ra["market_positioning"], ra["future_developments"]],
                           self.docs, self.spans)
        fa_texts = [fa["growth_and_peers"], fa["valuation_insights"]] + [
            json.dumps(x, ensure_ascii=False) for x in fa["definitions"]]
        sources = [x["text"] for v in (self.fa_context["quarter_from_earnings_release"],)
                   for k in ("revenue_lines", "diluted_eps_lines") for x in v[k]]
        sources += [r["arithmetic"] or "" for r in self.fa_context["ratios"]]
        sources += [p["operating_margin"]["arithmetic"] or "" for p in self.fa_context["peers"].values()]
        sources += [e["arithmetic"] for e in self.calc.log]
        allowed = allowed_numbers(list(self.calc.values.values()) + [e["value"] for e in self.calc.log], sources)
        out["financial_analyst_untraced_numbers"] = untraced(fa_texts, allowed)
        out["numbers_not_on_cited_line"] = check_line_numbers(
            [ra["overview"], ra["market_positioning"], ra["future_developments"], fa["growth_and_peers"],
             fa["valuation_insights"]], self.docs)
        inputs = allowed_numbers([], [json.dumps(ra, ensure_ascii=False), json.dumps(fa, ensure_ascii=False)])
        dn = ia["deciding_number"]["value"]
        out["deciding_number_found_in_inputs"] = bool(_numbers(dn)) and not untraced([dn], inputs)
        out["investment_advisor_untraced_numbers"] = untraced(
            [ia["thesis"], ia["strategic_recommendations"]], inputs)
        out["core_claims_missing"] = self.missing_core_claims(" ".join(fa_texts))
        return out

    def _quarter_value(self, key, label_re):
        """First number in the first release table row whose label matches, e.g. 'Total net revenue | $11,536 | ...'."""
        for line in self.fa_context["quarter_from_earnings_release"][key]:
            cells = _plain(line["text"]).split(" | ")
            if len(cells) > 1 and re.search(label_re, cells[0], re.I):
                nums = _numbers(cells[1])
                if nums:
                    return -nums[0][1] if "(" in cells[1] or "-" in cells[1] else nums[0][1]
        return None

    def missing_core_claims(self, text):
        """Which of the eleven core claims the Financial Analyst's text does not state."""
        t = self.desk.company["ticker"]
        v = self.calc.values
        found = {x for _, x, _ in _numbers(text)}

        def stated(value):
            return value is not None and _matches_any(found, value)

        claims = {
            "1 total revenue, latest fiscal year": [v.get(_id(t, "revenue", "fy"))],
            "2 revenue growth versus the prior year": [v.get(_id(t, "revenue_growth"))],
            "3 operating income and operating margin": [v.get(_id(t, "operating_income", "fy")),
                                                        v.get(_id(t, "operating_margin"))],
            "4 net income": [v.get(_id(t, "net_income", "fy"))],
            "5 GAAP diluted EPS": [v.get(_id(t, "eps_diluted", "fy"))],
            "6 return on average equity": [v.get(_id(t, "roae"))],
            "7 debt to equity (current portion included)": [v.get(_id(t, "debt_to_equity"))],
            "8 free cash flow": [v.get(_id(t, "fcf"))],
            "9 latest quarter's total revenue": [self._quarter_value("revenue_lines", r"revenue|sales")],
            "11 the quarter's GAAP diluted EPS": [self._quarter_value("diluted_eps_lines", r"^diluted")],
        }
        missing = [name for name, values in claims.items()
                   if any(x is not None for x in values) and not all(stated(x) for x in values if x is not None)]
        sentences = re.split(r"(?<=[.!?])\s+|\n", _plain(text))
        if not any(re.search(r"quarter", s, re.I) and re.search(r"year|yoy", s, re.I) and "%" in s for s in sentences):
            missing.append("10 the quarter's growth versus the same quarter last year")
        return sorted(missing, key=lambda s: int(s.split()[0]))

    def used_calculations(self, fa):
        """Calculator results that appear in the Financial Analyst's final text (drafts sent back by
        the Manager can leave calculations the final text no longer uses)."""
        text = _plain(json.dumps(fa, ensure_ascii=False))
        return [e for e in self.calc.log if _plain(e["result"]) in text]

    def sources_table(self, fa):
        """Code-compiled tables behind the Financial Analyst's numbers: each XBRL fact with its tag,
        period and filing, and each ratio with its arithmetic (step 5 and the calculator)."""
        ctx = self.fa_context
        rows = ["#### XBRL facts", "", "| Fact | Period | Value | Source |", "|---|---|---|---|"]
        rows += [f"| {f['label']} | {f['period']} | {f['value']} | {f['source']} |" for f in ctx["facts"]]
        rows += ["", "#### Ratios and their arithmetic", "", "| Ratio | Arithmetic | Source |", "|---|---|---|"]
        rows += [f"| {r['name']} | {r['arithmetic'] or r['note'] or 'n/a'} | step 5 calculator |" for r in ctx["ratios"]]
        for t, p in ctx["peers"].items():
            om = p["operating_margin"]
            rows.append(f"| {t} operating margin | {om['arithmetic']} | step 7 |")
        seen = set()
        for e in self.used_calculations(fa):
            if e["expression"] not in seen:
                seen.add(e["expression"])
                inputs = "; ".join(f"{k}: {v}" for k, v in e["inputs"].items())
                rows.append(f"| {e['label']} | {e['arithmetic']} | calculator tool; {inputs} |")
        return "\n".join(rows)

    def assemble(self, ra, fa, ia):
        ia = {**ia, "strategic_recommendations": re.sub(r"^\s*#+[^\n]*\n", "", ia["strategic_recommendations"])}
        definitions = "\n".join(f"- **{x['item']}**: uses {x['definition_used']}; alternative: "
                                f"{x['alternative']} [{x['source']}]" for x in fa["definitions"])
        dn = ia["deciding_number"]
        return {
            "Overview": ra["overview"],
            "Market Positioning and Growth Outlook": ra["market_positioning"] + "\n\n" + fa["growth_and_peers"],
            "Valuation Insights": fa["valuation_insights"]
                + (f"\n\n### Definitions used\n{definitions}" if definitions else "")
                + "\n\n### Sources and arithmetic (compiled in code from the data and the calculator)\n\n"
                + self.sources_table(fa),
            "Future Developments and Strategic Recommendations":
                ra["future_developments"] + "\n\n### Strategic recommendations\n" + ia["strategic_recommendations"],
            "Investment Recommendation":
                f"**Recommendation: {ia['recommendation']}**\n\n"
                f"**Deciding number:** {dn['label']}: {dn['value']} (from the {dn['appears_in']}'s output). "
                f"{dn['why']}\n\n{ia['thesis']}",
        }

    # -- the Strategy Manager's workflow (Figure 1)
    def run(self):
        print("  [Strategy Manager] dispatching the team", flush=True)
        ra_context = self.research_context()
        self.fa_context = self.financial_context()
        tasks = {
            RA: ("Write the qualitative research: business model, competitive position, catalysts and "
                 "recent developments, from the 10-K sections and the earnings release.",
                 "JSON with 'overview' (business model and what the company does), 'market_positioning' "
                 "(competitive position and the growth drivers the filings describe) and "
                 "'future_developments' (catalysts and recent developments). Every claim cited with its quoted line."),
            FA: ("Write the statement analysis: growth, profitability, returns, leverage, cash flow and "
                 "valuation from the facts and ratios, the latest quarter from the release lines, and "
                 "the peer comparison.",
                 "JSON with 'growth_and_peers' (growth, the quarter and the peer comparison), "
                 "'valuation_insights' (profitability, returns, leverage, free cash flow, P/E) and "
                 "'definitions'. Every number with its source; every ratio with its arithmetic."),
            IA: ("Write the investment thesis and recommendation from the two analysts' outputs.",
                 "JSON with 'thesis', 'recommendation' (Buy, Hold or Sell), 'deciding_number' and "
                 "'strategic_recommendations'."),
        }
        spec = {RA: (RA_SYSTEM, RA_SCHEMA, None), FA: (FA_SYSTEM, FA_SCHEMA, [CALCULATOR_TOOL]),
                IA: (IA_SYSTEM, IA_SCHEMA, None)}

        def send(role, context, previous=None, findings=None):
            task, expected = tasks[role]
            if previous is not None:
                task = ("Revise your previous output to fix the Strategy Manager's findings. Keep what "
                        "was not criticized. Original task: " + task)
                context = {**context, "your_previous_output": previous, "manager_findings": findings}
            system, schema, tools = spec[role]
            return self.call(role, system, {"task": task, "context": context, "expected_output": expected},
                             schema, tools)

        out = {RA: send(RA, ra_context), FA: send(FA, self.fa_context)}
        out[IA] = send(IA, {RA: out[RA], FA: out[FA]})

        checks = self.checks(out[RA], out[FA], out[IA])
        parts = self.assemble(out[RA], out[FA], out[IA])
        review = self.call("Strategy Manager", SM_SYSTEM, {
            "task": "Check that the five parts are present and consistent, and decide pass or fail for each.",
            "context": {"draft": {p: {"written_by": PARTS[p], "text": parts[p]} for p in PARTS},
                        "automated_checks": checks},
            "expected_output": "JSON with 'parts': one entry per part with 'part', 'status', 'roles' "
                               "(responsible roles if it fails, else empty) and 'problem' (empty if it passes)."},
            SM_SCHEMA)["parts"]

        failing = {}
        for p in review:
            if p["status"] == "fail":
                for role in p["roles"] or PARTS[p["part"]]:
                    failing.setdefault(role, []).append({"part": p["part"], "problem": p["problem"]})
        for p in review:
            print(f"  [Strategy Manager] {p['part']}: {p['status']}"
                  + (f" -> back to {', '.join(p['roles'] or PARTS[p['part']])}" if p["status"] == "fail" else ""))
        self.revision_errors = {}
        for role in (RA, FA, IA):  # each failing part goes back to its role once
            if role in failing:
                context = {RA: ra_context, FA: self.fa_context, IA: {RA: out[RA], FA: out[FA]}}[role]
                try:
                    out[role] = send(role, context, previous=out[role], findings=failing[role])
                except (openai.APIError, SystemExit) as e:  # keep the first draft rather than lose the report
                    self.revision_errors[role] = str(e)[:300]
                    print(f"  [{role}] revision failed ({str(e)[:120]}); keeping the first draft", flush=True)

        print("  [Strategy Manager] compiling the report", flush=True)
        out[RA] = {k: _plain(v) for k, v in out[RA].items()}
        final_checks = self.checks(out[RA], out[FA], out[IA])
        for old, new in final_checks["line_numbers_corrected"]:  # quotes verified on another line
            out[RA] = {k: v.replace(old, new) for k, v in out[RA].items()}
        self.final_calculations = self.used_calculations(out[FA])
        return self.compile(self.assemble(out[RA], out[FA], out[IA]), out[IA], review, failing, final_checks)

    def compile(self, parts, ia, review, failing, checks):
        d = self.desk
        read = []
        if "10-K" in self.docs:
            read.append(f"10-K {d.filings['10-K']['accession']} (Item 1 Business, Item 7 MD&A)")
        if "Release" in self.docs:
            read.append(f"8-K {d.filings['8-K']['accession']} (EX-99.1 earnings release)")
        xbrl = sorted({c[p]["accn"] for c in d.facts["concepts"].values() for p in ("fy", "prior") if c[p]})
        read.append(f"XBRL facts from {', '.join(xbrl)}")
        for pt, p in (d.done.get(7) or {}).items():
            accns = sorted({c["fy"]["accn"] for c in p["facts"]["concepts"].values() if c["fy"]})
            read.append(f"peer {pt} XBRL facts from {', '.join(accns)}")
        lines = [f"Filings read: {'; '.join(read)}",
                 f"Run time: {d.run_time:%Y-%m-%d %H:%M:%S}", "",
                 f"# {d.company['title']} ({d.company['ticker']}): equity research report", "",
                 f"**Recommendation: {ia['recommendation']}**"
                 + (f" · share price used: ${d.args.price:,.2f}" if d.args.price is not None else ""), ""]
        for i, p in enumerate(PARTS, 1):
            lines += [f"## {i}. {p}", "", parts[p].strip(), ""]
        lines += ["---", "", "## Appendix A: Strategy Manager review of the first draft", "",
                  "Each failing part was sent back to its role once; the report above is the revised version, "
                  "and the checks below the table were run on it.", "",
                  "| Part | Review | Problem | Sent back to |", "|---|---|---|---|"]
        for p in review:
            sent = ", ".join(p["roles"] or PARTS[p["part"]]) if p["status"] == "fail" else ""
            problem = p["problem"].replace("|", "/").replace("\n", " ")
            lines.append(f"| {p['part']} | {p['status']} | {problem} | {sent} |")
        lines += ["", "Checks run in code on the final text:", "",
                  f"- Research Analyst quotes verified against their lines: {checks['quotes_verified']} "
                  f"(of which {len(checks['line_numbers_corrected'])} cited the wrong line and were corrected "
                  f"to the line where the quote appears); not matching: {len(checks['quotes_failed'])}; "
                  f"paragraphs without a citation: {len(checks['paragraphs_without_citation'])}",
                  f"- Numbers not on the release or 10-K line they cite: "
                  f"{len(checks['numbers_not_on_cited_line'])}",
                  f"- Financial Analyst numbers not traced to the data or calculator: "
                  f"{', '.join(checks['financial_analyst_untraced_numbers']) or 'none'}",
                  f"- Deciding number found in the Advisor's inputs: "
                  f"{'yes' if checks['deciding_number_found_in_inputs'] else 'no'}",
                  f"- Core claims the Financial Analyst did not state: "
                  f"{'; '.join(checks['core_claims_missing']) or 'none'}",
                  f"- Investment Advisor numbers not found in its inputs: "
                  f"{', '.join(checks['investment_advisor_untraced_numbers']) or 'none'}"]
        for q in checks["quotes_failed"]:
            lines.append(f"  - quote not matching: {q}")
        for q in checks["numbers_not_on_cited_line"]:
            lines.append(f"  - number not on its cited line: {q}")
        for role, error in self.revision_errors.items():
            lines.append(f"- The {role}'s revision failed ({error}); its first draft is used above.")
        lines += ["", "## Appendix B: Calculator log", ""]
        if self.calc.log:
            used = {id(e) for e in self.final_calculations}
            lines += ["Every call, in order, including drafts the Strategy Manager sent back. Only rows marked "
                      "'yes' are used in the final report.", "",
                      "| Label | Arithmetic | In final report |", "|---|---|---|"]
            lines += [f"| {e['label']} | {e['arithmetic']} | {'yes' if id(e) in used else 'no'} |"
                      for e in self.calc.log]
        else:
            lines.append("The Financial Analyst made no calculator calls beyond the desk's step 5 ratios.")
        lines += ["", f"Model: {self.model} via Purdue GenAI Studio; tokens: {self.tokens['input']:,} in, "
                      f"{self.tokens['output']:,} out.", ""]
        path = d.folder() / "report.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def run_report(desk, model=DEFAULT_MODEL):
    """Step 8: the four roles. Returns the path of the saved Markdown report."""
    try:
        return Roles(desk, model).run()
    except openai.AuthenticationError:
        raise SystemExit("Purdue GenAI Studio rejected the API_KEY in .env. Check or rotate the key.")
    except openai.NotFoundError:
        raise SystemExit(f"Model {model!r} is not available on Purdue GenAI Studio.")
    except openai.RateLimitError:
        raise SystemExit("Rate limited by Purdue GenAI Studio even after retries. Try again in a minute.")
    except openai.APITimeoutError:
        raise SystemExit("Purdue GenAI Studio timed out. The server may be busy; try again.")
    except openai.APIStatusError as e:
        raise SystemExit(f"Purdue GenAI Studio error {e.status_code}: {e.message}")
    except openai.APIConnectionError:
        raise SystemExit("Could not reach Purdue GenAI Studio. Check the internet connection.")
