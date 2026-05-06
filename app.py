"""
app.py — Streamlit UI for Bank Reconciliation Tool

Regions: India 🇮🇳 · USA 🇺🇸 · Australia 🇦🇺
Banks  : HDFC/SBI/Axis/ICICI/Kotak | Chase/WellsFargo/BofA/Citi/CapitalOne/USBank | ANZ/NAB/Westpac/CBA/Macquarie
Books  : Tally | QuickBooks / IIF | Xero | MYOB | Wave | FreshBooks | OFX/QBO | QIF
"""

import io
import streamlit as st
import pandas as pd

from reconciler import (
    BANK_FORMATS, BOOKS_SOFTWARE,
    HAS_PIKEPDF, HAS_OCR,
    _REGION_CURRENCY, _REGION_CURRENCY_SYMBOL,
    extract_from_pdf, extract_bank_csv, extract_from_csv, extract_with_ocr,
    extract_from_ofx, extract_from_qif, extract_from_iif,
    extract_from_quickbooks_csv, extract_from_xero_csv, extract_from_myob_csv,
    extract_from_wave_csv, extract_from_freshbooks_csv,
    reconcile, to_excel,
    is_scanned_pdf, is_tally_pdf,
    peek_pdf_rows, detect_format, detect_books_software,
)

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Bank Reconciliation",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif; }

.recon-header {
  background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
  padding: 2rem 2.5rem; border-radius: 12px;
  margin-bottom: 1.5rem; border-left: 4px solid #3b82f6;
}
.recon-header h1 { color: #f1f5f9; font-size: 1.8rem; margin: 0; }
.recon-header p  { color: #94a3b8; margin: 0.3rem 0 0; font-size: 0.9rem; }

[data-testid="metric-container"] {
  background: #f8fafc; border: 1px solid #e2e8f0;
  border-radius: 10px; padding: 1rem;
}
.warn-box {
  background: #fff7ed; border: 1px solid #fed7aa;
  border-radius: 8px; padding: 0.75rem 1rem;
  font-size: 0.85rem; color: #92400e; margin-top: 0.5rem;
}
.info-box {
  background: #eff6ff; border: 1px solid #bfdbfe;
  border-radius: 8px; padding: 0.75rem 1rem;
  font-size: 0.85rem; color: #1e40af; margin-top: 0.5rem;
}
.ocr-box {
  background: #f0fdf4; border: 1px solid #bbf7d0;
  border-radius: 8px; padding: 0.75rem 1rem;
  font-size: 0.85rem; color: #166534; margin-top: 0.5rem;
}
.stTabs [data-baseweb="tab"] { font-family: 'IBM Plex Mono', monospace; font-size: 0.82rem; }
</style>
""", unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="recon-header">
  <h1>🏦 Bank Reconciliation Tool</h1>
  <p>India · USA · Australia — digital PDFs, scanned PDFs (OCR), CSV, OFX/QBO, QIF, IIF
     · split, consolidated &amp; reversal detection.</p>
</div>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════

with st.sidebar:

    # ── Region selector ───────────────────────────────────────────
    st.markdown("### 🌍 Region")
    region_label = st.radio(
        "Region", ["🇮🇳 India", "🇺🇸 USA", "🇦🇺 Australia"],
        horizontal=True, label_visibility="collapsed",
    )
    region_code = {"🇮🇳 India": "IN", "🇺🇸 USA": "US", "🇦🇺 Australia": "AU"}[region_label]

    region_bank_formats = BANK_FORMATS[region_code]
    region_books_sw     = BOOKS_SOFTWARE[region_code]
    base_currency       = _REGION_CURRENCY[region_code]
    currency_symbol     = _REGION_CURRENCY_SYMBOL[region_code]

    st.divider()

    # ── Bank format ───────────────────────────────────────────────
    st.markdown("### ⚙️ Matching Settings")
    bank_format = st.selectbox("Bank Format", options=list(region_bank_formats.keys()))

    # ── Books software ────────────────────────────────────────────
    books_software = st.selectbox("Books Software", options=region_books_sw)

    st.divider()

    amount_tol     = st.number_input(
        f"Amount Tolerance ({currency_symbol})", 0.0, 500.0, 0.01, 0.01
    )
    narr_threshold = st.slider("Narration Match % (min)", 0, 100, 60)
    date_tol       = st.number_input("Date Tolerance (days)", 0, 10, 2)

    st.divider()
    st.markdown("**Match Types**")
    st.markdown("🟢 **1:1** Direct · 🔵 **1:N** Split · 🟣 **N:1** Consolidated · 🔴 Reversal")

    st.divider()
    st.markdown("""
    <div class="warn-box">
    ⚠️ Files processed on Streamlit Cloud servers.<br>
    Run locally for sensitive client data.
    </div>
    """, unsafe_allow_html=True)

# ── Accepted file types by region ────────────────────────────────────────────
_BANK_TYPES: dict[str, list[str]] = {
    "IN": ["pdf", "csv"],
    "US": ["pdf", "csv", "ofx", "qbo", "qfx"],
    "AU": ["pdf", "csv", "ofx", "qif"],
}
_BOOKS_TYPES: dict[str, list[str]] = {
    "IN": ["pdf", "csv"],
    "US": ["pdf", "csv", "ofx", "qbo", "qfx", "qif", "iif"],
    "AU": ["pdf", "csv", "ofx", "qif"],
}

# ═══════════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════════

if "bank_col_map"  not in st.session_state: st.session_state.bank_col_map  = None
if "use_ocr_bank"  not in st.session_state: st.session_state.use_ocr_bank  = False
if "use_ocr_books" not in st.session_state: st.session_state.use_ocr_books = False


def _col_mapper_ui(preview_rows: list[list[str]], key_prefix: str) -> dict:
    if not preview_rows:
        st.warning("Could not extract any rows from this PDF for preview.")
        return {}
    max_cols   = max(len(r) for r in preview_rows)
    col_labels = [f"Col {i}" for i in range(max_cols)]
    padded     = [r + [""] * (max_cols - len(r)) for r in preview_rows]
    st.dataframe(pd.DataFrame(padded, columns=col_labels),
                 use_container_width=True, hide_index=True)
    st.caption("👆 Identify which column contains each field, then select below.")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        date_col = st.selectbox("📅 Date",      col_labels, key=f"{key_prefix}_date")
    with c2:
        narr_col = st.selectbox("📝 Narration", col_labels, key=f"{key_prefix}_narr",
                                index=min(1, max_cols - 1))
    with c3:
        deb_col  = st.selectbox("🔴 Debit",     col_labels, key=f"{key_prefix}_deb",
                                index=min(2, max_cols - 1))
    with c4:
        cred_col = st.selectbox("🟢 Credit",    col_labels, key=f"{key_prefix}_cred",
                                index=min(3, max_cols - 1))
    return {
        "date":      col_labels.index(date_col),
        "narration": col_labels.index(narr_col),
        "debit":     col_labels.index(deb_col),
        "credit":    col_labels.index(cred_col),
    }


def _software_tip(software: str, region: str) -> None:
    tips = {
        "Tally CSV": (
            "TallyPrime → Gateway of Tally → Display → Account Books → Ledger → "
            "Export → <strong>Excel / CSV</strong>. Columns: Date, Narration, Debit, Credit."
        ),
        "Tally PDF": (
            "⚠️ Tally PDFs use merged cells. CSV is strongly recommended for reliable results."
        ),
        "QuickBooks CSV": (
            "QuickBooks Online → Reports → Banking → Bank Register → Export to Excel/CSV.<br>"
            "Or: Banking → Download Transactions → CSV."
        ),
        "QuickBooks IIF": (
            "QuickBooks Desktop → File → Export → Lists to IIF Files.<br>"
            "IIF is tab-separated — do not open and save in Excel before uploading."
        ),
        "QuickBooks AU CSV": (
            "QuickBooks Online (AU) → Reports → Banking → Bank Register → Export to Excel/CSV."
        ),
        "Xero CSV": (
            "Xero → Accounting → Bank Accounts → select account → Export."
        ),
        "MYOB CSV": (
            "MYOB → Banking → Bank Transactions → Export → CSV.<br>"
            "Columns: Date, Description, Amount, GST Code."
        ),
        "Wave CSV": (
            "Wave → Accounting → Transactions → Export as CSV."
        ),
        "FreshBooks CSV": (
            "FreshBooks → Reports → General Ledger → Export CSV."
        ),
        "OFX / QBO": (
            "Most US banks: Online Banking → Download Transactions → "
            "select QBO / OFX / QFX format."
        ),
        "OFX": (
            "ANZ/Macquarie: Online Banking → Download Transactions → OFX format."
        ),
        "QIF": (
            "Macquarie Bank: Online Banking → Transactions → Export → QIF.<br>"
            "MYOB: File → Export → QIF."
        ),
    }
    msg = tips.get(software, "")
    if msg:
        st.markdown(f'<div class="info-box">💡 <strong>How to export {software}:</strong><br>{msg}</div>',
                    unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════
# UPLOAD SECTION
# ═══════════════════════════════════════════════════════════════════

col1, col2 = st.columns(2, gap="large")

bank_custom_map: dict = {}

# ── Bank Statement upload ─────────────────────────────────────────
with col1:
    st.markdown("#### 📄 Bank Statement")
    bank_file = st.file_uploader(
        "Bank file", type=_BANK_TYPES[region_code], key="bank",
        label_visibility="collapsed",
    )
    bank_pwd = st.text_input(
        "Password (if protected)", type="password", key="bank_pwd",
        placeholder="Leave blank if not protected",
        disabled=not HAS_PIKEPDF,
    )

    if bank_file:
        bank_bytes = io.BytesIO(bank_file.read())
        st.success(f"✅ {bank_file.name}  ({round(bank_file.size / 1024, 1)} KB)")
        fn_lower = bank_file.name.lower()
        is_bank_pdf = fn_lower.endswith(".pdf")
        is_bank_csv = fn_lower.endswith(".csv")
        is_bank_ofx = fn_lower.endswith((".ofx", ".qbo", ".qfx"))

        if is_bank_pdf:
            bank_bytes.seek(0)
            scanned = is_scanned_pdf(bank_bytes)

            if scanned:
                st.markdown("""
                <div class="ocr-box">
                📷 <strong>Scanned PDF detected</strong> — no machine-readable text found.<br>
                Enable OCR below to extract transactions via Tesseract.
                </div>""", unsafe_allow_html=True)
                if HAS_OCR:
                    st.session_state.use_ocr_bank = st.toggle(
                        "🔍 Enable OCR for Bank PDF", value=True, key="ocr_bank_toggle"
                    )
                    if st.session_state.use_ocr_bank:
                        st.caption("OCR is slow (≈5–15s per page at 300 DPI).")
                else:
                    st.error("OCR libraries not installed. See README.")
            else:
                st.session_state.use_ocr_bank = False

                # CBA warning
                if region_code == "AU" and bank_format == "CBA (OCR only)":
                    st.markdown("""
                    <div class="warn-box">
                    ⚠️ <strong>Commonwealth Bank</strong> does not offer CSV downloads.<br>
                    Enable OCR above to extract from the PDF statement.
                    </div>""", unsafe_allow_html=True)

                # Column mapper for Auto-Detect / Custom
                if bank_format in ("Auto-Detect", "Custom"):
                    bank_bytes.seek(0)
                    preview       = peek_pdf_rows(bank_bytes)
                    auto_detected = detect_format(preview)

                    if auto_detected and bank_format == "Auto-Detect":
                        st.markdown("""<div class="info-box">
                        ✅ <strong>Columns auto-detected.</strong></div>""",
                        unsafe_allow_html=True)
                        bank_custom_map = auto_detected
                    else:
                        label = ("Auto-detect failed — map columns manually:"
                                 if bank_format == "Auto-Detect" else
                                 "Map your bank's columns:")
                        st.markdown(f'<div class="warn-box">🗂️ <strong>{label}</strong></div>',
                                    unsafe_allow_html=True)
                        with st.expander("🗂️ Open Column Mapper", expanded=True):
                            bank_bytes.seek(0)
                            bank_custom_map = _col_mapper_ui(peek_pdf_rows(bank_bytes), "bank")
        elif is_bank_ofx:
            st.session_state.use_ocr_bank = False
            st.markdown("""<div class="info-box">
            ✅ <strong>OFX/QBO file detected</strong> — will parse directly.
            </div>""", unsafe_allow_html=True)
        else:
            st.session_state.use_ocr_bank = False


# ── Books upload ──────────────────────────────────────────────────
with col2:
    st.markdown("#### 📒 Books / Ledger")
    books_file = st.file_uploader(
        "Books file", type=_BOOKS_TYPES[region_code], key="books",
        label_visibility="collapsed",
    )
    books_pwd = st.text_input(
        "Password (if protected)", type="password", key="books_pwd",
        placeholder="Leave blank if not protected",
        disabled=not HAS_PIKEPDF,
    )

    if books_file:
        books_bytes = io.BytesIO(books_file.read())
        st.success(f"✅ {books_file.name}  ({round(books_file.size / 1024, 1)} KB)")
        fn_lower  = books_file.name.lower()
        is_pdf    = fn_lower.endswith(".pdf")
        is_csv    = fn_lower.endswith(".csv")
        is_ofx    = fn_lower.endswith((".ofx", ".qbo", ".qfx"))
        is_qif    = fn_lower.endswith(".qif")
        is_iif    = fn_lower.endswith(".iif")

        _software_tip(books_software, region_code)

        if is_pdf:
            books_bytes.seek(0)
            if is_tally_pdf(books_bytes):
                st.markdown("""
                <div class="warn-box">
                ⚠️ <strong>Tally PDF detected.</strong>
                Tally PDFs use merged cells and custom fonts that break table extraction.<br><br>
                <strong>Recommended:</strong> TallyPrime → Gateway of Tally →
                Display → Account Books → Ledger → Export → <strong>Excel / CSV</strong>.
                </div>""", unsafe_allow_html=True)

            books_bytes.seek(0)
            books_scanned = is_scanned_pdf(books_bytes)
            if books_scanned:
                st.markdown("""<div class="ocr-box">
                📷 <strong>Scanned books PDF detected.</strong></div>""",
                unsafe_allow_html=True)
                if HAS_OCR:
                    st.session_state.use_ocr_books = st.toggle(
                        "🔍 Enable OCR for Books PDF", value=True, key="ocr_books_toggle"
                    )
                else:
                    st.error("OCR libraries not installed.")
            else:
                st.session_state.use_ocr_books = False

        elif is_ofx:
            st.markdown("""<div class="info-box">
            ✅ <strong>OFX/QBO file detected</strong> — will parse directly.
            </div>""", unsafe_allow_html=True)
        elif is_qif:
            st.markdown("""<div class="info-box">
            ✅ <strong>QIF file detected.</strong></div>""", unsafe_allow_html=True)
        elif is_iif:
            st.markdown("""<div class="info-box">
            ✅ <strong>QuickBooks IIF file detected.</strong></div>""", unsafe_allow_html=True)

st.divider()

# ═══════════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════════

if not (bank_file and books_file):
    st.info("👆 Upload both files above to begin.")
    with st.expander("💡 Tips"):
        st.markdown(f"""
        - **Region selected:** {region_label} — date format and currency set automatically.
        - **India:** Best books format is Tally CSV. Columns must include Date, Narration, Debit, Credit.
        - **USA:** QuickBooks CSV (3-col or 4-col), QBO/OFX, or Xero CSV all supported.
        - **Australia:** Xero CSV, MYOB CSV, QIF (Macquarie/MYOB), or OFX all supported.
        - **CBA (Australia):** No CSV download — use OCR on the PDF statement.
        - **Wells Fargo:** No header row in CSV — select "Wells Fargo" from bank format.
        - **Scanned PDFs:** OCR is auto-detected. Requires Tesseract installed.
        - **Unknown bank format:** select **Custom** to map columns manually.
        - Increase Date Tolerance if your books record transactions a day or two later.
        - Lower Narration Match % if your bank uses short reference codes.
        """)
    st.stop()


if st.button("▶️ Run Reconciliation", type="primary", use_container_width=True):

    fmt_config = region_bank_formats.get(bank_format)
    if bank_format in ("Auto-Detect", "Custom") and bank_custom_map:
        fmt_config = bank_custom_map

    # ── Extract bank ───────────────────────────────────────────────
    with st.spinner("Extracting bank transactions…"):
        try:
            bank_bytes.seek(0)
            fn_lower   = bank_file.name.lower()
            is_bank_pdf = fn_lower.endswith(".pdf")
            is_bank_ofx = fn_lower.endswith((".ofx", ".qbo", ".qfx"))
            is_bank_csv = fn_lower.endswith(".csv")

            if is_bank_ofx:
                bank_txns = extract_from_ofx(bank_bytes, source="bank", region=region_code)
            elif is_bank_pdf:
                if st.session_state.use_ocr_bank:
                    bank_txns = extract_with_ocr(
                        bank_bytes, fmt_config, source="bank",
                        password=bank_pwd, base_currency=base_currency,
                        region=region_code,
                    )
                else:
                    bank_txns = extract_from_pdf(
                        bank_bytes, fmt_config, source="bank",
                        password=bank_pwd, base_currency=base_currency,
                        region=region_code,
                    )
            elif is_bank_csv:
                bank_txns = extract_bank_csv(
                    bank_bytes, fmt_config, source="bank",
                    base_currency=base_currency, region=region_code,
                )
            else:
                st.error("Unsupported bank file type.")
                st.stop()

        except ValueError as e:
            st.error(f"❌ Bank file: {e}")
            st.stop()
        except Exception as e:
            st.error(f"❌ Failed to read bank file: {e}")
            st.stop()

    # ── Extract books ──────────────────────────────────────────────
    with st.spinner("Extracting book transactions…"):
        try:
            books_bytes.seek(0)
            fn_lower  = books_file.name.lower()
            is_pdf    = fn_lower.endswith(".pdf")
            is_csv    = fn_lower.endswith(".csv")
            is_ofx    = fn_lower.endswith((".ofx", ".qbo", ".qfx"))
            is_qif    = fn_lower.endswith(".qif")
            is_iif    = fn_lower.endswith(".iif")

            if is_ofx or books_software in ("OFX / QBO", "OFX"):
                books_txns = extract_from_ofx(books_bytes, source="books", region=region_code)
            elif is_qif or books_software == "QIF":
                books_txns = extract_from_qif(books_bytes, source="books", region=region_code)
            elif is_iif or books_software == "QuickBooks IIF":
                books_txns = extract_from_iif(books_bytes, source="books", region=region_code)
            elif is_csv:
                if books_software in ("QuickBooks CSV", "QuickBooks AU CSV"):
                    books_txns = extract_from_quickbooks_csv(
                        books_bytes, source="books", region=region_code
                    )
                elif books_software == "Xero CSV":
                    books_txns = extract_from_xero_csv(
                        books_bytes, source="books", region=region_code
                    )
                elif books_software == "MYOB CSV":
                    books_txns = extract_from_myob_csv(books_bytes, source="books")
                elif books_software == "Wave CSV":
                    books_txns = extract_from_wave_csv(
                        books_bytes, source="books", region=region_code
                    )
                elif books_software == "FreshBooks CSV":
                    books_txns = extract_from_freshbooks_csv(
                        books_bytes, source="books", region=region_code
                    )
                else:
                    books_txns = extract_from_csv(
                        books_bytes, source="books",
                        base_currency=base_currency, region=region_code,
                    )
            elif is_pdf:
                if st.session_state.use_ocr_books:
                    books_txns = extract_with_ocr(
                        books_bytes, None, source="books",
                        password=books_pwd, base_currency=base_currency,
                        region=region_code,
                    )
                else:
                    books_txns = extract_from_pdf(
                        books_bytes, None, source="books",
                        password=books_pwd, base_currency=base_currency,
                        region=region_code,
                    )
            else:
                st.error("Unsupported books file type.")
                st.stop()

        except ValueError as e:
            st.error(f"❌ Books file: {e}")
            st.stop()
        except Exception as e:
            st.error(f"❌ Failed to read books file: {e}")
            st.stop()

    # ── Validate ───────────────────────────────────────────────────
    if not bank_txns:
        st.error(
            "No transactions extracted from bank file. "
            + ("OCR may need higher DPI or better scan quality."
               if st.session_state.use_ocr_bank else
               "Try switching Bank Format, or use Custom column mapper.")
        )
        st.stop()

    if not books_txns:
        msg = {
            "Tally CSV":    "Ensure columns include Date, Narration, Debit/Credit.",
            "Tally PDF":    "Try exporting as CSV from TallyPrime instead.",
            "QuickBooks CSV": "Ensure file has Date, Description, Amount columns.",
            "Xero CSV":     "Ensure Date and Amount columns are present.",
            "MYOB CSV":     "Ensure Date, Description, Amount columns are present.",
        }.get(books_software, "Check file format and column names.")
        st.error(f"No transactions extracted from books file. {msg}")
        st.stop()

    # ── Reconcile ──────────────────────────────────────────────────
    with st.spinner(f"Matching {len(bank_txns)} bank × {len(books_txns)} book entries…"):
        result = reconcile(
            bank_txns, books_txns,
            amount_tolerance=amount_tol,
            narration_threshold=narr_threshold,
            date_tolerance_days=date_tol,
        )

    matched         = result["matched"]
    unmatched_bank  = result["unmatched_bank"]
    unmatched_books = result["unmatched_books"]
    counts = {k: sum(1 for m in matched if m.match_type == k)
              for k in ("1:1", "1:N", "N:1", "reversal")}
    match_rate = round(len(matched) / max(len(matched) + len(unmatched_bank), 1) * 100, 1)

    # ── Summary metrics ────────────────────────────────────────────
    st.subheader("📊 Summary")
    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("Bank Entries",  len(bank_txns))
    c2.metric("Book Entries",  len(books_txns))
    c3.metric("🟢 1:1",        counts["1:1"])
    c4.metric("🔵 1:N",        counts["1:N"])
    c5.metric("🟣 N:1",        counts["N:1"])
    c6.metric("🔴 Reversals",  counts["reversal"])
    c7.metric("⚠️ Unmatched",  len(unmatched_bank) + len(unmatched_books))

    st.markdown(f"**Match Rate: {match_rate}%**")
    st.progress(match_rate / 100)
    st.divider()

    # ── Tabs ───────────────────────────────────────────────────────
    PAGE_SIZE = 200

    def _show_df(df: pd.DataFrame, key: str) -> None:
        if df.empty:
            return
        total_pages = max(1, (len(df) - 1) // PAGE_SIZE + 1)
        if total_pages > 1:
            page = st.number_input(f"Page (1–{total_pages})", 1, total_pages, 1, key=key)
            df   = df.iloc[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        st.dataframe(df, use_container_width=True, hide_index=True)

    sym = currency_symbol

    tab1, tab2, tab3, tab4 = st.tabs([
        f"✅ Matched ({len(matched)})",
        f"🔴 Unmatched Bank ({len(unmatched_bank)})",
        f"🟡 Unmatched Books ({len(unmatched_books)})",
        f"🔄 Reversals ({counts['reversal']})",
    ])

    with tab1:
        if matched:
            rows = [{
                "Type":              m.match_type,
                "Date (Bank)":       ", ".join(t.date      for t in m.bank),
                "Narration (Bank)":  ", ".join(t.narration for t in m.bank),
                f"Amt (Bank {sym})":  round(sum(t.amount   for t in m.bank), 2),
                "Date (Books)":      ", ".join(t.date      for t in m.books) if m.books else "—",
                "Narration (Books)": ", ".join(t.narration for t in m.books) if m.books else "—",
                f"Amt (Books {sym})": round(sum(t.amount   for t in m.books), 2) if m.books else 0,
                "Score":             f"{m.score}%",
                "Notes":             m.notes,
            } for m in matched]
            _show_df(pd.DataFrame(rows), "page_matched")
        else:
            st.info("No matched entries. Try lowering Narration Match % or increasing Date Tolerance.")

    with tab2:
        if unmatched_bank:
            st.caption("In Bank Statement — no matching Books entry found.")
            _show_df(pd.DataFrame([{
                "Date": t.date, "Narration": t.narration,
                f"Amount ({sym})": t.amount,
                "Type": t.txn_type, "Currency": t.currency,
            } for t in unmatched_bank]), "page_ub")
        else:
            st.success("🎉 All bank entries accounted for in Books!")

    with tab3:
        if unmatched_books:
            st.caption("In Books — no matching Bank transaction found.")
            _show_df(pd.DataFrame([{
                "Date": t.date, "Narration": t.narration,
                f"Amount ({sym})": t.amount,
                "Type": t.txn_type, "Currency": t.currency,
            } for t in unmatched_books]), "page_ubk")
        else:
            st.success("🎉 All book entries have a matching bank transaction!")

    with tab4:
        reversals = [m for m in matched if m.match_type == "reversal"]
        if reversals:
            st.caption("Debit + Credit of the same amount — likely returned/reversed. Verify with client.")
            _show_df(pd.DataFrame([{
                "Date 1": m.bank[0].date, "Narration 1": m.bank[0].narration,
                f"Amount ({sym})": m.bank[0].amount,
                "Type 1": m.bank[0].txn_type,
                "Date 2": m.bank[1].date, "Narration 2": m.bank[1].narration,
                "Type 2": m.bank[1].txn_type, "Notes": m.notes,
            } for m in reversals]), "page_rev")
        else:
            st.info("No reversal pairs detected.")

    # ── Download ───────────────────────────────────────────────────
    st.divider()
    st.download_button(
        label="📥 Download Full Report (.xlsx)",
        data=to_excel(result, currency_symbol=sym),
        file_name="reconciliation_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        type="primary",
    )
    st.caption("4 sheets: Summary · Matched · Unmatched Bank · Unmatched Books")
