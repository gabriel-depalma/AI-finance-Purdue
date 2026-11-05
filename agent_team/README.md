# Agent team (Labs 04–05)

The desk rebuilt as an agent team on **CrewAI Flows**. The graph is fixed in code and is the same on every run:

Step 1 → … → Step 7 → Research Analyst → Financial Analyst → Investment Advisor → Strategy Manager → Verifier

- **Roles** (`roles/`): each role's instructions are in its own file. The three analyst personas end with an `Insight:` line. Each role node is one model call that gets only `{task, context, expected_output}` as JSON.
- **Strategy Manager**: checks the five parts, the eleven core claims and each Insight line (missing, or only repeating the release's opening paragraph). It sends a failing part back to its role once. No role runs more than twice.
- **Verifier** (`verifier.py`): runs on every run, and no option turns it off. It reads only `report.md`, the saved filing texts and `facts.json`, and skips the Investment Advisor's thesis. Code checks XBRL numbers against the facts file, quoted lines against the filing texts, and ratios by recomputing them. One model call checks that each cited line states the number for that period and definition. Each number is marked correct, wrong or unsupported, with corrections from the filings.
- **Runs** (`runs/`): one folder per run, named by company, date and time. Each holds the filing texts, `facts.json`, each node's input and output (`nodes/`), `report.md` and `verified_report*.md`, with the table of marks under the first line.

The model is Purdue GenAI Studio (`gpt-oss:120b`). The key is read as `API_KEY` from `../.env`.

## Commands (from this folder)

```
uv venv --python 3.13 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt

.venv\Scripts\python.exe team.py COST --peers WMT,TGT,BJ --price 992.13
.venv\Scripts\python.exe team.py --verify-only runs\<run folder>
.venv\Scripts\python.exe ui.py          (or double-click start_ui.bat)
```

## Lab 04 Verifier tests

- `runs/..._test_seeded_eps`: diluted EPS raised by $1 to $21.76. Marked **wrong** and corrected to $20.76 (XBRL EarningsPerShareDiluted; 10-K L1134).
- `runs/..._test_invented_growth`: revenue growth stated as 14.14%. Marked **wrong**; recomputed (303,154.0 − 275,235.0) / 275,235.0 = 10.14%.
- `runs/LAB03_..._test_clean`: the Lab 03 Costco report with the audit's one fix (quarter GAAP EPS $6.75). All eleven core claims are marked correct.
- Skip test: the only option is `--verify-only`, which runs the Verifier alone. No option skips it.
