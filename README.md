# Estimate Review Prototype
Custom missed-operations analyzer for Ben's Body Works (and other shops).

## What it does
- Accepts CCC ONE estimate PDFs
- Extracts RO#, VIN, vehicle, labor rates, and line items
- Applies domain rules to suggest common missed operations (R&I, scans, ADAS, corrosion protection, one-time-use parts, etc.)
- Calculates potential recovered dollars using the shop's rates
- Simple web UI for review

## Run the web app
```bash
cd /workspace/artifacts/estimate_review
python3 app.py
```
Then open http://localhost:5000 in a browser and upload a PDF.

## Test the analyzer directly
```bash
python3 estimate_analyzer.py /path/to/estimate.pdf
```

## Current status (MVP)
- Works on the sample PDFs provided (3720, 3722, 3700, 3735)
- Rule-based suggestions (strong starting point)
- Ready to expand with better PDF parsing, more rules, and LLM analysis

## Next steps
1. Improve line-item parsing accuracy
2. Add more specific rules from your estimators' experience
3. Generate supplement PDF
4. Multi-estimate dashboard
5. LLM-enhanced justifications

Labor rates default to the rates found in the sample estimates ($65 body/paint, $85 mechanical, $40 paint supplies).
