# Equity-research desk: data side

## Purpose
Write a Python program for the data side of an equity-research desk.
Use Section 3.1 of Francis & Zhang (2026) as the model.
Do not write the model roles now. You will write them later.

## Rules
1. Read the SEC User-Agent from the `.env` file in this folder. The name of the value is `SEC_USER_AGENT`.
2. Send the User-Agent with each request.
3. Do not write a company name in the code.
4. Print each step when the program starts it.

## Input
The program accepts these items:

| Item | Status | Function |
|---|---|---|
| Ticker | Mandatory | The company to research. |
| `--peers` | Optional | A list of peer tickers. |
| `--price` | Optional | The share price, for P/E. |
| `--step` | Optional | The number of one step. The program then does only that step. |

## Steps

### Step 1: Find the CIK
1. Use the SEC file `company_tickers.json`.
2. Find the CIK for the ticker.

### Step 2: Find the filings
1. Get the list of company filings from the EDGAR submissions API.
2. Find the latest 10-K.
3. Find the latest 10-Q.
4. Find the latest 8-K that reports Item 2.02 in its document.

   NOTE: An 8-K with Item 2.02 contains the earnings release.

5. For each of the three filings, print the accession number.
6. For each of the three filings, print the "Accepted" time from its EDGAR index page.

### Step 3: Download the documents
1. Download the 10-K document.
2. Download the 10-Q document.
3. For the 8-K, download Exhibit 99.1.

   NOTE: Exhibit 99.1 is the earnings release.

4. Change each document to plain text.
5. Save the text files in one folder.
6. Use the company name, the date and the time of the run as the folder name.

### Step 4: Get the XBRL facts
1. Get the XBRL company facts.
2. Find the values for the latest fiscal year:
   - Total revenue
   - Operating income
   - Net income
   - Diluted EPS
   - Stockholders' equity
   - Long-term debt
   - The current portion of long-term debt
   - Cash from operations
   - Additions to property and equipment
3. For each value, print the value, its tag, its period end and its filing.
4. Print the value for the prior year next to each value.

   NOTE: A concept can have more than one candidate tag.

5. For each concept, use the candidate tag whose latest year is the newest.
6. Use the fiscal year of revenue for all concepts.

### Step 5: Calculate the ratios
1. Write a calculator in the code.
2. Calculate these items:
   - Revenue growth
   - Operating margin
   - Return on average equity
   - Debt to equity. Include the current portion of long-term debt in the debt.
   - Free cash flow. Free cash flow = cash from operations − additions to property and equipment.
   - P/E. Use the `--price` value.
3. For each item, print the result and its arithmetic.

### Step 6: Read the earnings release
1. Find the lines in the release text that give the total revenue for the quarter.
2. Find the lines in the release text that give the diluted EPS for the quarter.
3. Print these lines.

### Step 7: Compare the peers
1. For each peer, print the same facts as in Step 4.
2. For each peer, calculate the operating margin with the definition in Step 5.
3. Print the operating margin for each peer.

## Output
Save the facts in the same folder as the text files.

## Report
When you complete the program, give me:
1. The exact command that installs the necessary packages.
2. The exact command that runs the program with a ticker, peers and a price.
