# AI Analyst Project 1: equity-research desk, data side

The data layer of a multi-agent equity-research desk, modeled on Section 3.1 of
Francis & Zhang (2026), "Can AI match professional analysts? Evidence from a multi-agent system,"
*Finance Research Letters* 103, 110127. The model roles come later.

## Setup

Python 3.9+, standard library only:

```
python -m pip install -r requirements.txt
```

Create a `.env` file in this folder with your SEC contact details (it is git-ignored):

```
SEC_USER_AGENT=Your Name your.email@example.com
```

## Run

```
python research_desk.py COST --peers WMT,TGT,BJ --price 1005.16
```

Put the ticker first. `--step N` runs one step only (1-7).

| Step | What it does |
|---|---|
| 1 | Ticker -> CIK from SEC `company_tickers.json` |
| 2 | Latest 10-K, 10-Q and Item 2.02 8-K, with accession numbers and EDGAR "Accepted" times |
| 3 | Downloads the 10-K, 10-Q and the 8-K's Exhibit 99.1 as plain text into `runs/<Company>_<date>_<time>/` |
| 4 | XBRL facts for the latest fiscal year and the prior year, with tag, period end and filing |
| 5 | Calculator: revenue growth, operating margin, ROAE, debt/equity, free cash flow, P/E |
| 6 | Release lines stating the quarter's total revenue and diluted EPS |
| 7 | The same facts and operating margin for each peer |

Facts, calculations, release lines and peers are saved as `facts.json` beside the texts.

## Files

- `research_desk.py`: command line and the seven steps
- `edgar_docs.py`: EDGAR filings, index pages, HTML-to-text, release lines
- `xbrl_facts.py`: XBRL tag selection and the calculator
- `sec_client.py`: `.env` reader and rate-limited SEC client
- `library/`: the build prompt, rewritten in ASD-STE100 Simplified Technical English
- `runs/`: example output
