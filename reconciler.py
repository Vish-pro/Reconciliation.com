"""
reconciler.py — Bank Reconciliation Engine

Features:
  Extraction   : digital PDF, scanned PDF (OCR), CSV
  PDF safety   : password unlock (pikepdf), junk-row filter, page crop,
                 two-line narration merge, per-page column re-detection
  Parsing      : 15 date formats + dateutil fallback, multi-currency,
                 negatives / CR-suffix, Indian lakh format, unicode NFKC
  Matching     : bipartite 1:1 (Hungarian), 1:N splits, N:1 consolidations,
                 reversal detection, amount-bucket index for O(1) candidates
  Helpers      : peek_pdf_rows(), detect_format(), is_scanned_pdf(),
                 is_tally_pdf() — used by the Streamlit UI
"""

from __future__ import annotations

import io
import re
import unicodedata
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from typing import Optional

import numpy as np
import pandas as pd
import pdfplumber
from dateutil import parser as dateparser
from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment

# ── Optional deps — graceful fallback ────────────────────────────────────────
try:
    import pikepdf
    HAS_PIKEPDF = True
except ImportError:
    HAS_PIKEPDF = False

try:
    import pytesseract
    from pytesseract import Output as TessOutput
    from pdf2image import convert_from_bytes
    from PIL import Image          # noqa: F401 — confirms Pillow present
    HAS_OCR = True
except ImportError:
    HAS_OCR = False

warnings.filterwarnings("ignore")


# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

BANK_FORMATS: dict[str, Optional[dict]] = {
    "Auto-Detect": None,
    "HDFC":   {"date": 0, "narration": 1, "debit": 4, "credit": 5},
    "SBI":    {"date": 0, "narration": 2, "debit": 4, "credit": 5},
    "Axis":   {"date": 0, "narration": 2, "debit": 3, "credit": 4},
    "ICICI":  {"date": 0, "narration": 1, "debit": 3, "credit": 4},
    "Kotak":  {"date": 0, "narration": 1, "debit": 3, "credit": 4},
    "Custom": None,   # filled by UI column mapper
}

_JUNK_PATTERNS = [
    r"opening\s*balance", r"closing\s*balance", r"balance\s*b/?f",
    r"balance\s*c/?f", r"brought\s*forward", r"carried\s*forward",
    r"\btotal\b", r"page\s+\d+\s+of\s+\d+", r"statement\s+of\s+account",
    r"^\s*date\s*$", r"account\s+number", r"ifsc", r"branch\s+name",
    r"terms\s+and\s+conditions", r"this\s+is\s+(a\s+)?computer",
]

_CURRENCY_SYMBOLS = {
    "₹": "INR", "Rs.": "INR", "Rs": "INR", "INR": "INR",
    "USD": "USD", "$": "USD",
    "EUR": "EUR", "€": "EUR",
    "GBP": "GBP", "£": "GBP",
}

_DATE_FORMATS = [
    "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
    "%d %b %Y", "%d-%b-%Y", "%d/%b/%Y", "%d %b %y",
    "%d %B %Y", "%d-%B-%Y",
    "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y",
    "%d.%m.%Y", "%d.%m.%y",
]

_HEADER_KEYWORDS: dict[str, set[str]] = {
    "date":      {"date", "dt", "txn date", "tran date", "trans date",
                  "value date", "posting date"},
    "narration": {"narration", "description", "particulars", "remarks",
                  "details", "desc", "transaction details"},
    "debit":     {"debit", "dr", "withdrawal", "withdraw",
                  "paid out", "debit amt", "withdrawal amt"},
    "credit":    {"credit", "cr", "deposit", "received",
                  "credit amt", "deposit amt"},
}

_TALLY_KEYWORDS = {"tally", "tallyprime", "tally.erp", "tdl", "tally solutions"}


# ═══════════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Transaction:
    date: str
    amount: float
    narration: str
    txn_type: str          # "debit" | "credit"
    source: str            # "bank" | "books"
    currency: str = "INR"
    is_reversal: bool = False
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class MatchResult:
    match_type: str        # "1:1" | "1:N" | "N:1" | "reversal"
    bank: list[Transaction]
    books: list[Transaction]
    score: float
    notes: str = ""


# ═══════════════════════════════════════════════════════════════════
# TEXT UTILITIES
# ═══════════════════════════════════════════════════════════════════

def _clean_text(val: str) -> str:
    if not val:
        return ""
    try:
        val = unicodedata.normalize("NFKC", str(val))
    except Exception:
        val = str(val)
    val = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", val)
    return re.sub(r"\s+", " ", val).strip()


def _is_junk_row(row: list) -> bool:
    text = " ".join(_clean_text(str(c)) for c in row if c)
    if not text.strip():
        return True
    for p in _JUNK_PATTERNS:
        if re.search(p, text, re.IGNORECASE):
            return True
    return False


# ═══════════════════════════════════════════════════════════════════
# AMOUNT PARSING
# ═══════════════════════════════════════════════════════════════════

def _parse_amount(val: str) -> tuple[Optional[float], str, bool]:
    """
    Returns (amount, currency, is_reversal).
    Handles: ₹1,000.00 | 1000 CR | -500 | (500.00) | USD 100 | 1,00,000
    """
    if not val:
        return None, "INR", False
    s = _clean_text(str(val))

    currency = "INR"
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in s:
            currency = code
            s = s.replace(symbol, "").strip()
            break

    is_reversal = bool(re.search(
        r"\bCR\b|\bReversal\b|\bRev\b|\bReturn\b", s, re.IGNORECASE
    ))
    s = re.sub(r"\b(CR|DR|C|D|Reversal|Rev|Return)\b", "", s, flags=re.IGNORECASE)
    is_negative = bool(re.match(r"^\s*-", s)) or ("(" in s and ")" in s)
    s_clean = re.sub(r"[^\d.]", "", s)
    if not s_clean:
        return None, currency, is_reversal
    try:
        amount = float(s_clean)
        if amount == 0:
            return None, currency, is_reversal
        if is_negative:
            amount = -amount
        return amount, currency, is_reversal
    except ValueError:
        return None, currency, is_reversal


# ═══════════════════════════════════════════════════════════════════
# DATE PARSING
# ═══════════════════════════════════════════════════════════════════

def _parse_date(val: str) -> Optional[str]:
    val = _clean_text(str(val or "")).strip()
    if not val or len(val) < 4:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(val, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        return dateparser.parse(val, dayfirst=True).strftime("%Y-%m-%d")
    except Exception:
        return None


def _date_obj(d: str) -> Optional[datetime]:
    try:
        return datetime.strptime(d, "%Y-%m-%d")
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
# PDF UNLOCK
# ═══════════════════════════════════════════════════════════════════

def unlock_pdf(file, password: str = "") -> io.BytesIO:
    raw = file.read() if hasattr(file, "read") else file
    buf = io.BytesIO(raw)
    if not HAS_PIKEPDF:
        buf.seek(0)
        return buf
    buf.seek(0)
    try:
        with pikepdf.open(buf, password=password) as pdf:
            out = io.BytesIO()
            pdf.save(out)
            out.seek(0)
            return out
    except pikepdf.PasswordError:
        raise ValueError("Incorrect PDF password.")
    except Exception:
        buf.seek(0)
        return buf


# ═══════════════════════════════════════════════════════════════════
# COLUMN AUTO-DETECTION
# ═══════════════════════════════════════════════════════════════════

def _detect_columns(header_row: list) -> Optional[dict]:
    mapping: dict[str, int] = {}
    for i, cell in enumerate(header_row):
        cell_norm = _clean_text(str(cell or "")).lower()
        for col_type, keywords in _HEADER_KEYWORDS.items():
            if col_type not in mapping and any(kw in cell_norm for kw in keywords):
                mapping[col_type] = i
    if "date" in mapping and ("debit" in mapping or "credit" in mapping):
        mapping.setdefault("narration", 1)
        return mapping
    return None


def _find_header(rows: list[list]) -> tuple[int, Optional[dict]]:
    for i, row in enumerate(rows[:10]):
        col_map = _detect_columns(row)
        if col_map:
            return i, col_map
    return 0, None


def detect_format(rows: list[list]) -> Optional[dict]:
    """Public wrapper — used by the UI column mapper to check auto-detect."""
    _, col_map = _find_header(rows)
    return col_map


# ═══════════════════════════════════════════════════════════════════
# NARRATION MERGE (two-line wrap fix)
# ═══════════════════════════════════════════════════════════════════

def _merge_wrapped_rows(rows: list[list], date_col: int) -> list[list]:
    merged: list[list] = []
    for row in rows:
        date_val = row[date_col] if date_col < len(row) else None
        if not _parse_date(str(date_val or "")) and merged:
            prev = merged[-1]
            for i, cell in enumerate(row):
                if not cell:
                    continue
                if i < len(prev) and prev[i]:
                    prev[i] = f"{_clean_text(str(prev[i]))} {_clean_text(str(cell))}".strip()
                elif i < len(prev):
                    prev[i] = _clean_text(str(cell))
                else:
                    prev.extend([""] * (i - len(prev)))
                    prev.append(_clean_text(str(cell)))
        else:
            merged.append(list(row))
    return merged


# ═══════════════════════════════════════════════════════════════════
# SCANNED PDF DETECTION
# ═══════════════════════════════════════════════════════════════════

def is_scanned_pdf(file) -> bool:
    """
    Returns True if PDF has no extractable text — i.e., it is image-based / scanned.
    Checks first 3 pages; if any yields >50 chars of text, it's digital.
    Leaves file pointer at position 0 after the check.
    """
    raw = file.read() if hasattr(file, "read") else file
    buf = io.BytesIO(raw)
    try:
        with pdfplumber.open(buf) as pdf:
            for page in pdf.pages[:3]:
                text = page.extract_text() or ""
                if len(text.strip()) > 50:
                    if hasattr(file, "seek"):
                        file.seek(0)
                    return False
    except Exception:
        pass
    if hasattr(file, "seek"):
        file.seek(0)
    return True


# ═══════════════════════════════════════════════════════════════════
# OCR PIPELINE  (pytesseract + pdf2image)
# ═══════════════════════════════════════════════════════════════════

def _ocr_image_to_rows(img, gap_frac: float = 0.025) -> list[list[str]]:
    """
    Convert one PIL image to a list of cell-rows using pytesseract bounding boxes.

    Strategy:
      1. Get word-level bounding boxes (image_to_data).
      2. Cluster words into rows by vertical proximity (y_tol = 1 % of page height).
      3. Within each row, split words into cells by horizontal gap
         (gap_frac × page_width).
    This handles multi-column bank statement layouts without needing OpenCV.
    """
    data = pytesseract.image_to_data(
        img, output_type=TessOutput.DATAFRAME,
        config="--psm 6 --oem 3",   # uniform block, LSTM engine
    )
    data = data[(data["conf"] > 30) & (data["text"].str.strip() != "")]
    if data.empty:
        return []

    y_tol     = img.height * 0.012
    x_gap_min = img.width  * gap_frac
    data = data.sort_values(["top", "left"]).reset_index(drop=True)

    # ── Cluster into rows ────────────────────────────────────────
    row_groups: list[list[dict]] = []
    current: list[dict] = [data.iloc[0].to_dict()]
    current_y: float = data.iloc[0]["top"]

    for _, word in data.iloc[1:].iterrows():
        if abs(word["top"] - current_y) <= y_tol:
            current.append(word.to_dict())
        else:
            row_groups.append(sorted(current, key=lambda w: w["left"]))
            current   = [word.to_dict()]
            current_y = word["top"]
    if current:
        row_groups.append(sorted(current, key=lambda w: w["left"]))

    # ── Merge words into cells by x-gap ─────────────────────────
    result: list[list[str]] = []
    for group in row_groups:
        if not group:
            continue
        cells: list[str] = []
        cell_words: list[str] = [group[0]["text"]]
        prev_right: float = group[0]["left"] + group[0]["width"]

        for word in group[1:]:
            gap = word["left"] - prev_right
            if gap > x_gap_min:
                cells.append(" ".join(cell_words).strip())
                cell_words = [word["text"]]
            else:
                cell_words.append(word["text"])
            prev_right = word["left"] + word["width"]

        cells.append(" ".join(cell_words).strip())
        result.append([c for c in cells if c])

    return result


def extract_with_ocr(
    file,
    fmt_config: Optional[dict],
    source: str,
    password: str = "",
    base_currency: str = "INR",
    dpi: int = 300,
) -> list[Transaction]:
    """
    OCR extraction pipeline for scanned / image-based PDFs.

    Flow: PDF → unlock → pdf2image (PIL images) → tesseract bounding boxes
          → cell rows → same parsing pipeline as digital PDFs.

    Requires: pytesseract, pdf2image, Pillow, tesseract binary.
    On Ubuntu/Streamlit Cloud: add tesseract-ocr + poppler-utils to packages.txt.
    """
    if not HAS_OCR:
        raise RuntimeError(
            "OCR not available. Install: pip install pytesseract pdf2image pillow\n"
            "Also install Tesseract binary (see README)."
        )

    unlocked = unlock_pdf(file, password)
    images   = convert_from_bytes(unlocked.read(), dpi=dpi)

    all_rows: list[list[str]] = []
    for img in images:
        all_rows.extend(_ocr_image_to_rows(img))

    if not all_rows:
        return []

    # Re-use the same downstream parsing pipeline
    header_idx, auto_map = _find_header(all_rows)
    col_map    = fmt_config or auto_map or {"date": 0, "narration": 1, "debit": 2, "credit": 3}
    date_col   = col_map.get("date", 0)
    narr_col   = col_map.get("narration", 1)
    debit_col  = col_map.get("debit", 2)
    credit_col = col_map.get("credit", 3)

    data_rows = _merge_wrapped_rows(all_rows[header_idx + 1:], date_col)
    transactions: list[Transaction] = []

    for row in data_rows:
        if _is_junk_row(row):
            continue
        try:
            parsed_date = _parse_date(str(row[date_col] if date_col < len(row) else ""))
            if not parsed_date:
                continue
            narration = _clean_text(str(row[narr_col] if narr_col < len(row) else ""))

            for raw_val, base_type in (
                (row[debit_col]  if debit_col  < len(row) else "", "debit"),
                (row[credit_col] if credit_col < len(row) else "", "credit"),
            ):
                amount, currency, is_reversal = _parse_amount(str(raw_val or ""))
                if amount is None:
                    continue
                txn_type = base_type
                if amount < 0:
                    txn_type    = "credit" if base_type == "debit" else "debit"
                    amount      = abs(amount)
                    is_reversal = True
                note = f"[{currency}] {narration}" if currency != base_currency else narration
                transactions.append(Transaction(
                    date=parsed_date, amount=round(amount, 2),
                    narration=note, txn_type=txn_type,
                    source=source, currency=currency, is_reversal=is_reversal,
                ))
        except (IndexError, AttributeError):
            continue

    return transactions


# ═══════════════════════════════════════════════════════════════════
# COLUMN MAPPER HELPERS  (used by Streamlit UI)
# ═══════════════════════════════════════════════════════════════════

def peek_pdf_rows(file, max_rows: int = 15) -> list[list[str]]:
    """
    Extract raw table rows from the first 3 pages of a PDF — no parsing.
    Used by the column mapper UI to display a preview so the user can
    visually identify which column index = Date / Narration / Debit / Credit.
    """
    raw = file.read() if hasattr(file, "read") else file
    buf = io.BytesIO(raw)
    rows: list[list[str]] = []

    try:
        with pdfplumber.open(buf) as pdf:
            for page in pdf.pages[:3]:
                for table in (page.extract_tables() or []):
                    for row in table:
                        if row and any(c for c in row):
                            rows.append([_clean_text(str(c or "")) for c in row])
                        if len(rows) >= max_rows:
                            return rows
    except Exception:
        pass

    return rows


# ═══════════════════════════════════════════════════════════════════
# TALLY PDF DETECTION
# ═══════════════════════════════════════════════════════════════════

def is_tally_pdf(file) -> bool:
    """
    Returns True if the PDF appears to be a Tally export.
    Tally PDFs are notoriously bad for machine parsing — merged cells,
    custom fonts, no consistent column structure.
    The UI uses this to recommend CSV export instead.
    """
    raw = file.read() if hasattr(file, "read") else file
    buf = io.BytesIO(raw)
    try:
        with pdfplumber.open(buf) as pdf:
            for page in pdf.pages[:2]:
                text = (page.extract_text() or "").lower()
                if any(kw in text for kw in _TALLY_KEYWORDS):
                    if hasattr(file, "seek"):
                        file.seek(0)
                    return True
    except Exception:
        pass
    if hasattr(file, "seek"):
        file.seek(0)
    return False


# ═══════════════════════════════════════════════════════════════════
# DIGITAL PDF EXTRACTION
# ═══════════════════════════════════════════════════════════════════

def extract_from_pdf(
    file,
    fmt_config: Optional[dict],
    source: str,
    password: str = "",
    base_currency: str = "INR",
) -> list[Transaction]:
    transactions: list[Transaction] = []
    file = unlock_pdf(file, password)

    with pdfplumber.open(file) as pdf:
        all_rows: list[list] = []
        for page in pdf.pages:
            h = page.height
            try:
                cropped = page.crop((0, h * 0.08, page.width, h * 0.92))
                tables  = cropped.extract_tables() or []
            except Exception:
                tables = []
            if not tables:
                tables = page.extract_tables() or []
            for table in tables:
                all_rows.extend(r for r in table if r)

    if not all_rows:
        return transactions

    header_idx, auto_map = _find_header(all_rows)
    col_map    = fmt_config or auto_map or {"date": 0, "narration": 1, "debit": 2, "credit": 3}
    date_col   = col_map.get("date", 0)
    narr_col   = col_map.get("narration", 1)
    debit_col  = col_map.get("debit", 2)
    credit_col = col_map.get("credit", 3)

    data_rows = _merge_wrapped_rows(all_rows[header_idx + 1:], date_col)

    for row in data_rows:
        if _is_junk_row(row):
            continue
        try:
            parsed_date = _parse_date(str(row[date_col] if date_col < len(row) else ""))
            if not parsed_date:
                continue
            narration = _clean_text(str(row[narr_col] if narr_col < len(row) else ""))

            for raw_val, base_type in (
                (row[debit_col]  if debit_col  < len(row) else "", "debit"),
                (row[credit_col] if credit_col < len(row) else "", "credit"),
            ):
                amount, currency, is_reversal = _parse_amount(str(raw_val or ""))
                if amount is None:
                    continue
                txn_type = base_type
                if amount < 0:
                    txn_type    = "credit" if base_type == "debit" else "debit"
                    amount      = abs(amount)
                    is_reversal = True
                note = f"[{currency}] {narration}" if currency != base_currency else narration
                transactions.append(Transaction(
                    date=parsed_date, amount=round(amount, 2),
                    narration=note, txn_type=txn_type,
                    source=source, currency=currency, is_reversal=is_reversal,
                    raw={"row": row},
                ))
        except (IndexError, AttributeError):
            continue

    return transactions


# ═══════════════════════════════════════════════════════════════════
# CSV EXTRACTION
# ═══════════════════════════════════════════════════════════════════

def extract_from_csv(file, source: str, base_currency: str = "INR") -> list[Transaction]:
    df = pd.read_csv(file, dtype=str)
    df.columns = [_clean_text(c).lower() for c in df.columns]

    def _find_col(patterns: list[str]) -> Optional[str]:
        for pat in patterns:
            match = next((c for c in df.columns if pat in c), None)
            if match:
                return match
        return None

    date_col  = _find_col(["date"])
    narr_col  = _find_col(["narr", "desc", "particular", "remark", "detail"])
    debit_col = _find_col(["debit", "dr", "withdraw", "paid"])
    cred_col  = _find_col(["credit", "cr", "deposit", "received"])

    if not date_col:
        raise ValueError("Cannot detect date column. Rename it to 'Date' and re-upload.")

    transactions: list[Transaction] = []
    for _, row in df.iterrows():
        narration   = _clean_text(str(row.get(narr_col, "") or "")) if narr_col else ""
        parsed_date = _parse_date(str(row.get(date_col, "") or ""))
        if not parsed_date:
            continue
        for col, base_type in ((debit_col, "debit"), (cred_col, "credit")):
            if not col:
                continue
            amount, currency, is_reversal = _parse_amount(str(row.get(col, "") or ""))
            if amount is None:
                continue
            txn_type = base_type
            if amount < 0:
                txn_type    = "credit" if base_type == "debit" else "debit"
                amount      = abs(amount)
                is_reversal = True
            note = f"[{currency}] {narration}" if currency != base_currency else narration
            transactions.append(Transaction(
                date=parsed_date, amount=round(amount, 2),
                narration=note, txn_type=txn_type,
                source=source, currency=currency, is_reversal=is_reversal,
            ))
    return transactions


# ═══════════════════════════════════════════════════════════════════
# MATCH SCORING + BUCKET INDEX
# ═══════════════════════════════════════════════════════════════════

def _score(b: Transaction, e: Transaction, date_tol: int) -> float:
    if b.txn_type != e.txn_type:
        return 0.0
    b_date, e_date = _date_obj(b.date), _date_obj(e.date)
    if b_date and e_date and abs((b_date - e_date).days) > date_tol:
        return 0.0
    return float(fuzz.partial_ratio(b.narration.lower(), e.narration.lower()))


def _amounts_match(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def _build_index(txns: list[Transaction]) -> dict[int, list[int]]:
    idx: dict[int, list[int]] = {}
    for i, t in enumerate(txns):
        idx.setdefault(round(t.amount), []).append(i)
    return idx


def _candidates(amount: float, tol: float, idx: dict[int, list[int]]) -> list[int]:
    result = []
    for bucket in range(round(amount - tol), round(amount + tol) + 1):
        result.extend(idx.get(bucket, []))
    return result


# ═══════════════════════════════════════════════════════════════════
# MATCHING PASSES
# ═══════════════════════════════════════════════════════════════════

def _bipartite_match(
    bank: list[Transaction], books: list[Transaction],
    amount_tol: float, narration_threshold: int, date_tol: int,
) -> tuple[list[MatchResult], list[Transaction], list[Transaction]]:
    if not bank or not books:
        return [], bank, books
    books_idx = _build_index(books)
    n, m      = len(bank), len(books)
    score_mat = np.zeros((n, m), dtype=float)
    for i, b in enumerate(bank):
        for j in _candidates(b.amount, amount_tol, books_idx):
            if j < m:
                score_mat[i, j] = _score(b, books[j], date_tol)
    row_ind, col_ind = linear_sum_assignment(-score_mat)
    matched: list[MatchResult] = []
    used_bank: set[int]  = set()
    used_books: set[int] = set()
    for r, c in zip(row_ind, col_ind):
        s = score_mat[r, c]
        if s >= narration_threshold and _amounts_match(bank[r].amount, books[c].amount, amount_tol):
            matched.append(MatchResult("1:1", [bank[r]], [books[c]], round(s, 1)))
            used_bank.add(r)
            used_books.add(c)
    return (
        matched,
        [b for i, b in enumerate(bank)  if i not in used_bank],
        [b for i, b in enumerate(books) if i not in used_books],
    )


def _subset_indices(
    target: float, pool: list[Transaction], tol: float, max_parts: int = 3
) -> Optional[list[int]]:
    for r in range(2, max_parts + 1):
        for combo in combinations(range(len(pool)), r):
            if _amounts_match(sum(pool[k].amount for k in combo), target, tol):
                return list(combo)
    return None


def _match_splits(
    bank: list[Transaction], books: list[Transaction],
    amount_tol: float, date_tol: int,
) -> tuple[list[MatchResult], list[Transaction], list[Transaction]]:
    matched: list[MatchResult] = []
    used_bank: set[int]  = set()
    used_books: set[int] = set()
    for i, b in enumerate(bank):
        eligible = [
            (j, bk) for j, bk in enumerate(books)
            if j not in used_books and bk.txn_type == b.txn_type and bk.amount < b.amount
            and (not _date_obj(b.date) or not _date_obj(bk.date)
                 or abs((_date_obj(b.date) - _date_obj(bk.date)).days) <= date_tol)  # type: ignore[operator]
        ]
        if len(eligible) < 2:
            continue
        pool, pool_idx = [e[1] for e in eligible], [e[0] for e in eligible]
        combo = _subset_indices(b.amount, pool, amount_tol)
        if combo:
            matched.append(MatchResult("1:N", [b], [pool[k] for k in combo], 100.0,
                                       notes=f"Bank ₹{b.amount} = {len(combo)} book entries"))
            used_bank.add(i)
            for k in combo:
                used_books.add(pool_idx[k])
    return (
        matched,
        [b for i, b in enumerate(bank)  if i not in used_bank],
        [b for i, b in enumerate(books) if i not in used_books],
    )


def _match_consolidated(
    bank: list[Transaction], books: list[Transaction],
    amount_tol: float, date_tol: int,
) -> tuple[list[MatchResult], list[Transaction], list[Transaction]]:
    matched: list[MatchResult] = []
    used_bank: set[int]  = set()
    used_books: set[int] = set()
    for j, bk in enumerate(books):
        eligible = [
            (i, b) for i, b in enumerate(bank)
            if i not in used_bank and b.txn_type == bk.txn_type and b.amount < bk.amount
            and (not _date_obj(bk.date) or not _date_obj(b.date)
                 or abs((_date_obj(bk.date) - _date_obj(b.date)).days) <= date_tol)  # type: ignore[operator]
        ]
        if len(eligible) < 2:
            continue
        pool, pool_idx = [e[1] for e in eligible], [e[0] for e in eligible]
        combo = _subset_indices(bk.amount, pool, amount_tol)
        if combo:
            matched.append(MatchResult("N:1", [pool[k] for k in combo], [bk], 100.0,
                                       notes=f"{len(combo)} bank entries = Books ₹{bk.amount}"))
            used_books.add(j)
            for k in combo:
                used_bank.add(pool_idx[k])
    return (
        matched,
        [b for i, b in enumerate(bank)  if i not in used_bank],
        [b for i, b in enumerate(books) if i not in used_books],
    )


def _detect_reversals(
    bank: list[Transaction], books: list[Transaction],
    amount_tol: float, date_tol: int,
) -> tuple[list[MatchResult], list[Transaction], list[Transaction]]:
    matched: list[MatchResult] = []
    used_bank: set[int] = set()
    for i, b1 in enumerate(bank):
        if i in used_bank:
            continue
        for j, b2 in enumerate(bank):
            if j <= i or j in used_bank:
                continue
            if b1.txn_type != b2.txn_type and _amounts_match(b1.amount, b2.amount, amount_tol):
                d1, d2 = _date_obj(b1.date), _date_obj(b2.date)
                if d1 and d2 and abs((d1 - d2).days) <= date_tol:
                    matched.append(MatchResult(
                        "reversal", [b1, b2], [], 100.0,
                        notes="Debit + Credit of same amount — likely reversal/return",
                    ))
                    used_bank.update({i, j})
                    break
    return (
        matched,
        [b for i, b in enumerate(bank) if i not in used_bank],
        books,
    )


# ═══════════════════════════════════════════════════════════════════
# ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════

def reconcile(
    bank: list[Transaction],
    books: list[Transaction],
    amount_tolerance: float = 0.01,
    narration_threshold: int = 60,
    date_tolerance_days: int = 2,
) -> dict:
    m1, rem_bank, rem_books = _bipartite_match(
        bank, books, amount_tolerance, narration_threshold, date_tolerance_days)
    m2, rem_bank, rem_books = _match_splits(rem_bank, rem_books, amount_tolerance, date_tolerance_days)
    m3, rem_bank, rem_books = _match_consolidated(rem_bank, rem_books, amount_tolerance, date_tolerance_days)
    m4, rem_bank, rem_books = _detect_reversals(rem_bank, rem_books, amount_tolerance, date_tolerance_days)
    return {
        "matched":         m1 + m2 + m3 + m4,
        "unmatched_bank":  rem_bank,
        "unmatched_books": rem_books,
    }


# ═══════════════════════════════════════════════════════════════════
# EXCEL EXPORT
# ═══════════════════════════════════════════════════════════════════

def to_excel(result: dict) -> bytes:
    output = io.BytesIO()

    def _txn_dict(t: Transaction) -> dict:
        return {"Date": t.date, "Narration": t.narration,
                "Amount (₹)": t.amount, "Type": t.txn_type, "Currency": t.currency}

    matched_rows = [{
        "Match Type":        m.match_type,
        "Date (Bank)":       ", ".join(t.date      for t in m.bank),
        "Narration (Bank)":  ", ".join(t.narration for t in m.bank),
        "Amount (Bank ₹)":   round(sum(t.amount    for t in m.bank),  2),
        "Date (Books)":      ", ".join(t.date      for t in m.books) if m.books else "—",
        "Narration (Books)": ", ".join(t.narration for t in m.books) if m.books else "—",
        "Amount (Books ₹)":  round(sum(t.amount    for t in m.books), 2) if m.books else 0,
        "Score":             f"{m.score}%",
        "Notes":             m.notes,
    } for m in result["matched"]]

    counts  = {k: sum(1 for m in result["matched"] if m.match_type == k)
               for k in ("1:1", "1:N", "N:1", "reversal")}
    total   = len(result["matched"]) + len(result["unmatched_bank"])
    rate    = round(len(result["matched"]) / max(total, 1) * 100, 1)

    summary_df = pd.DataFrame([
        {"Metric": "Matched — 1:1",           "Count": counts["1:1"]},
        {"Metric": "Matched — 1:N (split)",   "Count": counts["1:N"]},
        {"Metric": "Matched — N:1 (consol.)", "Count": counts["N:1"]},
        {"Metric": "Reversals flagged",        "Count": counts["reversal"]},
        {"Metric": "Unmatched (Bank only)",    "Count": len(result["unmatched_bank"])},
        {"Metric": "Unmatched (Books only)",   "Count": len(result["unmatched_books"])},
        {"Metric": "Overall Match Rate (%)",   "Count": rate},
    ])

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary",          index=False)
        pd.DataFrame(matched_rows).to_excel(writer, sheet_name="Matched", index=False)
        pd.DataFrame([_txn_dict(t) | {"Remark": "In Bank — Not in Books"}
                      for t in result["unmatched_bank"]]).to_excel(
            writer, sheet_name="Unmatched (Bank)", index=False)
        pd.DataFrame([_txn_dict(t) | {"Remark": "In Books — Not in Bank"}
                      for t in result["unmatched_books"]]).to_excel(
            writer, sheet_name="Unmatched (Books)", index=False)

    return output.getvalue()
