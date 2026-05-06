# 🏦 Bank Reconciliation Tool

> Streamlit app to reconcile bank statements vs books. Supports digital/scanned PDFs, CSV, OCR, password-protected files & custom column mapping. Detects splits, consolidations & reversals. Excel report export.

---

## Features

| Feature | Detail |
|---|---|
| **Input formats** | Bank PDF · Books PDF · Books CSV (Tally export recommended) |
| **Bank formats** | HDFC · SBI · Axis · ICICI · Kotak · Auto-Detect · Custom mapper |
| **Scanned PDFs** | Auto-detected · OCR via Tesseract (bounding-box layout reconstruction) |
| **Password PDFs** | Unlocked in-memory via pikepdf |
| **Match types** | 1:1 direct · 1:N split · N:1 consolidated · Reversal pairs |
| **Matching engine** | Hungarian algorithm (globally optimal, no duplicate matches) |
| **Data quality** | Junk row filter · narration merge · unicode cleanup · 15 date formats |
| **Multi-currency** | ₹ / $ / € / £ — foreign entries flagged, still matched |
| **Export** | Excel report — Summary · Matched · Unmatched Bank · Unmatched Books |
| **Scale** | Amount-bucket index · 200-row UI pagination |

---

## Quick Start

### Hosted (no install)
Open the app URL → upload files → click **Run Reconciliation** → download report.

### Run locally
```bash
git clone https://github.com/YOUR_USERNAME/reconcile-app.git
cd reconcile-app
pip install -r requirements.txt
streamlit run app.py
```

For scanned PDF support, also install Tesseract:
```bash
# Ubuntu / Debian
sudo apt install tesseract-ocr poppler-utils

# macOS
brew install tesseract poppler

# Windows
# Download installer: https://github.com/UB-Mannheim/tesseract/wiki
```

---

## Deploy to Streamlit Cloud

1. Fork this repo
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**
3. Select your fork → set entry point to `app.py` → **Deploy**

`packages.txt` is already in the repo — Streamlit Cloud reads it automatically to install `tesseract-ocr` and `poppler-utils`.

---

## File Structure

```
reconcile-app/
├── app.py              # Streamlit UI
├── reconciler.py       # Extraction + matching engine (no UI dependency)
├── requirements.txt    # Python dependencies
├── packages.txt        # System dependencies (Streamlit Cloud)
└── README.md
```

`reconciler.py` has zero UI dependency — import it standalone for headless use:

```python
from reconciler import extract_from_pdf, extract_from_csv, reconcile, to_excel

bank  = extract_from_pdf(open("bank.pdf", "rb"), None, "bank")
books = extract_from_csv(open("tally.csv", "rb"), "books")
result = reconcile(bank, books)

with open("report.xlsx", "wb") as f:
    f.write(to_excel(result))
```

---

## Books CSV Format (Tally)

Export from TallyPrime:
> Gateway of Tally → Display → Account Books → Ledger → Export → **Excel / CSV**

Required columns (case-insensitive):

| Column | Accepted names |
|---|---|
| Date | `date` |
| Narration | `narration`, `description`, `particulars`, `remarks` |
| Debit | `debit`, `dr`, `withdrawal` |
| Credit | `credit`, `cr`, `deposit` |

---

## Custom Column Mapper

If your bank isn't in the supported list or Auto-Detect fails:

1. Select **Custom** from the Bank Format dropdown
2. A raw table preview of your PDF appears
3. Use the 4 dropdowns to map **Date / Narration / Debit / Credit** to the correct column
4. Run reconciliation

---

## Matching Logic

Runs in 4 sequential passes on remaining unmatched entries:

```
Pass 1 — Bipartite 1:1    Hungarian algorithm, globally optimal assignment
Pass 2 — Split 1:N        One bank entry = sum of N book entries (max 3-way)
Pass 3 — Consolidated N:1 N bank entries = one book entry
Pass 4 — Reversals        Debit + Credit of same amount within date window
```

---

## Settings

| Setting | Default | Notes |
|---|---|---|
| Amount Tolerance | ₹0.01 | Allows minor rounding differences |
| Narration Match % | 60 | Fuzzy match threshold — lower for UPI/NEFT codes |
| Date Tolerance | 2 days | Allows entries recorded on different days to match |

---

## ⚠️ Privacy

Files are processed **in-memory** and never stored to disk. However, when hosted on Streamlit Community Cloud, data passes through Streamlit's US servers. For sensitive client data (CA/tax use), **run locally**.

---

## Tech Stack

`streamlit` · `pdfplumber` · `rapidfuzz` · `scipy` · `pandas` · `pikepdf` · `pytesseract` · `pdf2image` · `python-dateutil`
