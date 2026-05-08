# 🏦 Bank Reconciliation Tool

> Free, open-source tool to reconcile your bank statement with your books in minutes. Upload files, click a button, download an Excel report.

---

## 👤 For Users (No Coding Needed)

### What does this tool do?
It compares your **bank statement** with your **Tally/books ledger** and tells you:
- Which entries match ✅
- Which are in the bank but missing from books ❌
- Which are in books but missing from bank ❌
- Splits, consolidations, and reversals are detected automatically

### How to use it (3 steps)

**Step 1 — Open the app**
👉 Open the hosted app URL in your browser (works on mobile too)

**Step 2 — Upload your files**
- Upload your **bank statement** (PDF or CSV)
- Upload your **books/ledger** (PDF or CSV exported from Tally)
- Select your bank name (HDFC, SBI, Axis, ICICI, Kotak — or Auto-Detect)

**Step 3 — Run & Download**
- Click **Run Reconciliation**
- Download the Excel report — it has 4 sheets:
  - **Summary** — overview of matches and gaps
  - **Matched** — all entries that matched
  - **Unmatched Bank** — entries in bank but not in books
  - **Unmatched Books** — entries in books but not in bank

---

### Supported File Formats

| What to upload | Accepted formats |
|---|---|
| Bank Statement | PDF (digital or scanned), CSV |
| Books / Ledger | PDF, CSV (Tally export recommended) |

> **Scanned or handwritten PDFs?** The tool uses OCR to read them automatically.
> **Password-protected PDF?** Enter the password when prompted — it's unlocked in your browser only, never stored.

---

### How to export from Tally

> Gateway of Tally → Display → Account Books → Ledger → Export → **Excel / CSV**

Your CSV should have these columns (names are flexible, case-insensitive):

| Column | Accepted column names |
|---|---|
| Date | `date` |
| Narration | `narration`, `description`, `particulars`, `remarks` |
| Debit | `debit`, `dr`, `withdrawal` |
| Credit | `credit`, `cr`, `deposit` |

---

### My bank isn't in the list — what do I do?

1. Select **Custom** from the Bank Format dropdown
2. A preview of your PDF table will appear
3. Use the 4 dropdowns to tell the tool which column is Date / Narration / Debit / Credit
4. Run reconciliation

---

### Settings you can adjust

| Setting | Default | What it means |
|---|---|---|
| Amount Tolerance | ₹0.01 | Allows tiny rounding differences to still match |
| Narration Match % | 60% | How closely descriptions must match — lower this for UPI/NEFT codes |
| Date Tolerance | 2 days | Allows entries recorded a day or two apart to still match |

---

### 🔒 Is my data safe?

Yes. Your files are processed **in your browser session only** and are **never saved to any server or disk**.

However, if you use the hosted (cloud) version, data does pass through Streamlit's servers in the US. For sensitive client financial data (CA/audit use), we recommend **running it locally** (see developer section below).

---

## 💻 For Developers

### Run locally

```bash
git clone https://github.com/vish-pro/reconciliation.com.git
cd reconciliation.com
pip install -r requirements.txt
streamlit run app.py
```

Open `http://localhost:8501` in your browser.

### Scanned PDF support (OCR)

Install Tesseract on your system:

```bash
# Ubuntu / Debian
sudo apt install tesseract-ocr poppler-utils

# macOS
brew install tesseract poppler

# Windows
# Download installer: https://github.com/UB-Mannheim/tesseract/wiki
```

### Deploy to Streamlit Cloud (free hosting)

1. Fork this repo
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**
3. Select your fork → set entry point to `app.py` → **Deploy**

`packages.txt` is already included — Streamlit Cloud reads it to auto-install `tesseract-ocr` and `poppler-utils`.

### Use reconciler.py standalone (headless / no UI)

```python
from reconciler import extract_from_pdf, extract_from_csv, reconcile, to_excel

bank  = extract_from_pdf(open("bank.pdf", "rb"), None, "bank")
books = extract_from_csv(open("tally.csv", "rb"), "books")
result = reconcile(bank, books)

with open("report.xlsx", "wb") as f:
    f.write(to_excel(result))
```

### File Structure

```
reconciliation.com/
├── app.py              # Streamlit UI
├── reconciler.py       # Extraction + matching engine (no UI dependency)
├── requirements.txt    # Python dependencies
├── packages.txt        # System dependencies (Streamlit Cloud)
└── README.md
```

### Matching Logic

Runs in 4 sequential passes on remaining unmatched entries:

```
Pass 1 — Bipartite 1:1    Hungarian algorithm, globally optimal assignment
Pass 2 — Split 1:N        One bank entry = sum of N book entries (max 3-way)
Pass 3 — Consolidated N:1 N bank entries = one book entry
Pass 4 — Reversals        Debit + Credit of same amount within date window
```

### Tech Stack

`streamlit` · `pdfplumber` · `rapidfuzz` · `scipy` · `pandas` · `pikepdf` · `pytesseract` · `pdf2image` · `python-dateutil`

---

## 🤝 Contributing

Pull requests are welcome. For major changes, open an issue first to discuss what you'd like to change.

---

## ⭐ If this helped you

Give the repo a star — it helps others find this tool.
