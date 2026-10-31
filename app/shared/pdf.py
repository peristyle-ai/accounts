"""
PDF generation utilities.

Provides reusable PDF generation for invoices.
"""

import io
import logging
import os
import pathlib
import re

from app.shared.address import render_address
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image


# Matches a trailing " — $X.XX" (em-dash + dollar amount) at the end of a
# description line. Used by _parse_line_items to split a multi-line description
# into separate line items.
_LINE_ITEM_AMOUNT_RE = re.compile(r"\s*—\s*\$([\d,]+\.\d{2})\s*$")

# --- Business logo resolution --------------------------------------------
# `user.logo_path` is stored RELATIVE to the app root by the upload route
# (e.g. "uploads/logos/<uuid>.png"). Resolving it against the process CWD is
# fragile: under systemd the CWD happens to be the repo root, but any script
# or test run from elsewhere finds nothing and the invoice silently ships
# with no logo. That is exactly what happened once on 2026-10-22 -- an
# invoice went out with no logo because the uploads/ directory had been
# left behind by the repo rename and the stored path no longer resolved.
#
# We resolve against the app root instead, and fall back to a git-tracked
# seed asset (config: app.logo_fallback_path) so a lost upload can never
# again silently drop the logo from customer-facing PDFs.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_log = logging.getLogger(__name__)
_FALLBACK_CACHE = None



def _logo_fallback_from_config():
    """Read app.logo_fallback_path from config.yaml (no app context needed)."""
    global _FALLBACK_CACHE
    if _FALLBACK_CACHE is not None:
        return _FALLBACK_CACHE
    _FALLBACK_CACHE = ''
    try:
        import yaml
        cfg_path = _REPO_ROOT / 'config.yaml'
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
            _FALLBACK_CACHE = (cfg.get('app') or {}).get('logo_fallback_path') or ''
    except Exception:
        _log.warning("Could not read logo_fallback_path from config.yaml",
                     exc_info=True)
    return _FALLBACK_CACHE


def _resolve_logo_path(user):
    """Return an absolute Path to the business logo, or None if there is none.

    Falls back to the configured seed asset ONLY when the user is known to
    have a logo (logo_path is set) but the file cannot be found. A business
    that never uploaded a logo never gets the fallback.
    """
    stored = getattr(user, 'logo_path', None)
    if not stored:
        return None

    candidates = []
    p = pathlib.Path(stored)
    candidates.append(p if p.is_absolute() else _REPO_ROOT / p)

    fallback = None
    try:
        from flask import current_app
        fallback = current_app.config.get('LOGO_FALLBACK_PATH')
    except Exception:
        fallback = None
    if not fallback:
        # No Flask app context (scripts, tests, CLI). Read config.yaml
        # directly so the fallback works the same everywhere.
        fallback = _logo_fallback_from_config()
    if fallback:
        fp = pathlib.Path(fallback)
        candidates.append(fp if fp.is_absolute() else _REPO_ROOT / fp)

    for c in candidates:
        if c.exists():
            if c != candidates[0]:
                _log.warning(
                    "Logo file for user %s not found at %s; using fallback %s",
                    getattr(user, 'id', '?'), candidates[0], c,
                )
            return c
    _log.warning(
        "Logo file for user %s not found (tried: %s); invoice will render "
        "without a logo.", getattr(user, 'id', '?'),
        ', '.join(str(c) for c in candidates),
    )
    return None


def _logo_image(user, width=4*cm, height=2*cm):
    """Return a ReportLab Image for the business logo, or None."""
    path = _resolve_logo_path(user)
    if not path:
        return None
    try:
        return Image(str(path), width=width, height=height, kind='proportional')
    except Exception:
        _log.warning("Could not load logo image %s", path, exc_info=True)
        return None




def _parse_line_items(description):
    """Parse a multi-line description into a list of (text, amount) tuples.

    Encoded form (chosen 2026-10-01 for the a customer invoice): callers
    put one line item per line, separated by `<br/>`, with the amount at the
    end of each line as " — $X.XX" (em-dash + dollar amount). Example:

        A.I Inference and System Hosting — $100.00<br/>
        A.I Agent and Software System Development — $200.00

    Returns the parsed list if every line has a parseable amount; otherwise
    returns None (caller falls back to single-row behaviour). Single-line
    descriptions without `<br/>` always fall back — that preserves the
    pre-2026-10-01 behaviour for invoices like Example Co's "Hosting +
    inference - <Month Year>".
    """
    if not description or '<br' not in description.lower():
        return None
    raw_lines = re.split(r'<br\s*/?>', description, flags=re.IGNORECASE)
    items = []
    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue
        m = _LINE_ITEM_AMOUNT_RE.search(line)
        if not m:
            return None  # One of the lines lacks an amount — not line items.
        amount_str = m.group(1).replace(',', '')
        try:
            amount = float(amount_str)
        except ValueError:
            return None
        text = _LINE_ITEM_AMOUNT_RE.sub('', line).strip()
        items.append((text, amount))
    return items or None


def generate_invoice_pdf(user, invoice):
    """
    Generate a PDF for an invoice and return as bytes buffer.

    Args:
        user: User object (with business details and logo)
        invoice: Invoice object (with customer)

    Returns:
        BytesIO buffer positioned at the start of the PDF data
    """
    from reportlab.lib.enums import TA_RIGHT

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
    elements = []
    styles = getSampleStyleSheet()

    logo_cell = []
    _logo = _logo_image(user)
    if _logo is not None:
        logo_cell.append(_logo)

    business_paragraphs = []
    if user.business_name:
        business_paragraphs.append(f'<b>{user.business_name}</b>')
    if user.abn:
        business_paragraphs.append(f'ABN: {user.abn}')
    if render_address(user):
        business_paragraphs.append(render_address(user).replace(chr(10), '<br/>'))
    if user.contact_email:
        business_paragraphs.append(f'Email: {user.contact_email}')
    if user.contact_number:
        business_paragraphs.append(f'Phone: {user.contact_number}')

    business_style = ParagraphStyle('Business', parent=styles['Normal'], fontSize=9, alignment=TA_RIGHT, leading=12)
    business_cell = [Paragraph('<br/>'.join(business_paragraphs), business_style)] if business_paragraphs else ['']

    business_name_style = ParagraphStyle(
        'BusinessName', parent=styles['Heading2'],
        fontSize=20, alignment=0, leading=24,
        textColor=colors.HexColor('#2c3e50'),
        spaceAfter=0, spaceBefore=0
    )
    business_name_cell = [Paragraph(user.business_name, business_name_style)] if user.business_name else ['']

    header_data = [
        [logo_cell, business_cell],
        [business_name_cell, '']
    ]
    header_table = Table(header_data, colWidths=[8*cm, 8*cm])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 4),
        ('TOPPADDING', (0, 1), (0, 1), 6),
        ('BOTTOMPADDING', (0, 1), (0, 1), 0),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 0.3*cm))

    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=28, spaceAfter=0, alignment=0, textColor=colors.HexColor('#2c3e50'))
    title_cell = Paragraph('INVOICE', title_style)

    invoice_title_row = Table([[title_cell, '']], colWidths=[12*cm, 4*cm])
    invoice_title_row.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LINEBELOW', (0, 0), (-1, -1), 1.5, colors.HexColor('#2c3e50')),
    ]))
    elements.append(invoice_title_row)
    elements.append(Spacer(1, 0.5*cm))

    info_data = [
        ['Invoice Date:', str(invoice.invoice_date), 'Invoice #:', invoice.id[:8].upper()],
        ['Due Date:', str(invoice.due_date) if invoice.due_date else 'N/A', '', '']
    ]
    info_table = Table(info_data, colWidths=[3*cm, 5*cm, 3*cm, 5*cm])
    info_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.grey),
        ('TEXTCOLOR', (2, 0), (2, -1), colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 0.5*cm))

    elements.append(Paragraph('<b>Bill To:</b>', styles['Heading3']))
    customer_info = f"<b>{invoice.customer.name}</b><br/>"
    if invoice.customer.contact_name:
        customer_info += f"Attn: {invoice.customer.contact_name}<br/>"
    if render_address(invoice.customer):
        customer_info += f"{render_address(invoice.customer).replace(chr(10), '<br/>')}<br/>"
    if invoice.customer.contact_email:
        customer_info += f"Email: {invoice.customer.contact_email}<br/>"
    if invoice.customer.contact_number:
        customer_info += f"Phone: {invoice.customer.contact_number}<br/>"
    if invoice.customer.abn:
        customer_info += f"ABN: {invoice.customer.abn}<br/>"
    elements.append(Paragraph(customer_info, styles['Normal']))
    elements.append(Spacer(1, 0.5*cm))

    # NOTE: the description is rendered inside the items table below — there is
    # no separate "Description:" paragraph above it. Pre-2026-10-01 this block
    # emitted a redundant Description paragraph AND a single-row items table
    # that both contained the description; the operator flagged it as visually
    # duplicated on 2026-10-01. The items table now owns all line-item
    # rendering.

    # Build the items table. New behaviour (2026-10-01): if the description is
    # encoded as multi-line `<br/>`-separated lines each carrying a trailing
    # dollar amount, render one row per line item with its individual amount.
    # Otherwise fall back to the pre-2026-10-01 single-row behaviour (one
    # description cell + the ex_gst total), which keeps Example Co and any
    # other legacy single-line invoices rendering unchanged.
    line_items = _parse_line_items(invoice.description)
    if line_items:
        items_data = [['Description', 'Amount']]
        for text, amount in line_items:
            items_data.append([
                Paragraph(text, styles['Normal']),
                f"${amount:,.2f}",
            ])
    else:
        # Wrap the description cell in a Paragraph so multi-line descriptions
        # (encoded with <br/> by the caller) render correctly. Strings would
        # show the literal `<br/>` text.
        items_data = [
            ['Description', 'Amount'],
            [Paragraph(invoice.description or 'Services', styles['Normal']),
             f"${invoice.ex_gst_amount:,.2f}"]
        ]
    items_table = Table(items_data, colWidths=[12*cm, 4*cm])
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#34495e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ALIGN', (-1, 0), (-1, -1), 'RIGHT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 0.3*cm))

    totals_data = [
        ['Subtotal (ex GST):', f"${invoice.ex_gst_amount:.2f}"],
        [f'GST ({float(invoice.gst_type)*100:.0f}%):', f"${invoice.gst_amount:.2f}"],
        ['TOTAL:', f"${invoice.total_amount:.2f}"]
    ]
    totals_table = Table(totals_data, colWidths=[12*cm, 4*cm])
    totals_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ALIGN', (-1, 0), (-1, -1), 'RIGHT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LINEABOVE', (0, -1), (-1, -1), 1, colors.black),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, -1), (-1, -1), 12),
    ]))
    elements.append(totals_table)

    if invoice.customer.gst and user.abn:
        elements.append(Spacer(1, 1*cm))
        payment_terms = user.payment_terms if user.payment_terms else 14
        elements.append(Paragraph(
            f'<i>Payment terms: Net {payment_terms} days. Please include invoice number {invoice.id[:8].upper()} on payment.</i>',
            styles['Normal']
        ))

    if user.bank_name or user.account_name or user.account_number_plain or user.bsb_plain:
        elements.append(Spacer(1, 0.7*cm))
        elements.append(Paragraph('<b>Bank Details:</b>', styles['Heading3']))
        bank_details = []
        if user.bank_name:
            bank_details.append(f'Bank: {user.bank_name}')
        if user.account_name:
            bank_details.append(f'Account Name: {user.account_name}')
        if user.bsb_plain:
            bank_details.append(f'BSB: {user.bsb_plain}')
        if user.account_number_plain:
            bank_details.append(f'Account Number: {user.account_number_plain}')
        elements.append(Paragraph('<br/>'.join(bank_details), styles['Normal']))

    doc.build(elements)
    buffer.seek(0)
    return buffer


def generate_statement_of_account_pdf(user, customer, invoices, as_of_date):
    """
    Generate a Statement of Account PDF for a customer.

    Lists every invoice (paid + outstanding) in chronological order with a
    running balance, plus a summary block with totals and a 30/60/90/90+ days
    aging breakdown of the outstanding amount.

    Args:
        user:         User row (drives the business-header / bank-details block).
        customer:     Customer row (drives the bill-to block).
        invoices:     Iterable of Invoice rows belonging to this customer.
                      Should be ordered by invoice_date ascending. The function
                      will sort defensively if not.
        as_of_date:   date — the "statement as at" date. Typically today.

    Returns:
        BytesIO buffer at position 0.
    """
    from datetime import date as _date
    from reportlab.lib.enums import TA_RIGHT, TA_LEFT, TA_CENTER

    # ---- normalise inputs -------------------------------------------------
    invoices = list(invoices)
    invoices.sort(key=lambda inv: (inv.invoice_date, inv.created_at or _date.min))

    # Decimals
    from decimal import Decimal
    def D(x):
        if x is None or x == '':
            return Decimal('0.00')
        return Decimal(str(x))

    # ---- summary aggregates ----------------------------------------------
    total_invoiced = Decimal('0.00')    # sum of total_amount across all invoices
    total_paid = Decimal('0.00')        # sum of amount_paid where paid
    outstanding = Decimal('0.00')       # sum of (total - paid) where status != 'paid'
    aging = {'current': Decimal('0.00'),
             '1_30':    Decimal('0.00'),
             '31_60':   Decimal('0.00'),
             '61_90':   Decimal('0.00'),
             '90_plus': Decimal('0.00')}

    for inv in invoices:
        total = D(inv.total_amount)
        paid = D(inv.amount_paid) if inv.status == 'paid' else Decimal('0.00')
        total_invoiced += total
        total_paid += paid
        if inv.status != 'paid':
            bal = total - paid
            outstanding += bal
            # days overdue = as_of_date - due_date (only count if past due)
            if inv.due_date and as_of_date > inv.due_date:
                days = (as_of_date - inv.due_date).days
                if days <= 30:
                    aging['1_30'] += bal
                elif days <= 60:
                    aging['31_60'] += bal
                elif days <= 90:
                    aging['61_90'] += bal
                else:
                    aging['90_plus'] += bal
            else:
                aging['current'] += bal

    # ---- doc setup --------------------------------------------------------
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=1.5*cm, leftMargin=1.5*cm,
        topMargin=1.2*cm, bottomMargin=1.2*cm,
    )
    elements = []
    styles = getSampleStyleSheet()

    # ---- header: logo + business block (mirrors invoice layout) ----------
    logo_cell = []
    _logo = _logo_image(user)
    if _logo is not None:
        logo_cell.append(_logo)

    business_paragraphs = []
    if user.business_name:
        business_paragraphs.append(f'<b>{user.business_name}</b>')
    if user.abn:
        business_paragraphs.append(f'ABN: {user.abn}')
    if render_address(user):
        business_paragraphs.append(render_address(user).replace(chr(10), '<br/>'))
    if user.contact_email:
        business_paragraphs.append(f'Email: {user.contact_email}')
    if user.contact_number:
        business_paragraphs.append(f'Phone: {user.contact_number}')

    business_style = ParagraphStyle(
        'StmtBusiness', parent=styles['Normal'],
        fontSize=9, alignment=TA_RIGHT, leading=12,
    )
    business_cell = [Paragraph('<br/>'.join(business_paragraphs), business_style)] if business_paragraphs else ['']

    business_name_style = ParagraphStyle(
        'StmtBusinessName', parent=styles['Heading2'],
        fontSize=20, alignment=0, leading=24,
        textColor=colors.HexColor('#2c3e50'),
        spaceAfter=0, spaceBefore=0,
    )
    business_name_cell = [Paragraph(user.business_name or '', business_name_style)] if user.business_name else ['']

    header_data = [
        [logo_cell, business_cell],
        [business_name_cell, ''],
    ]
    header_table = Table(header_data, colWidths=[8*cm, 8.5*cm])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),    # logo: left
        ('ALIGN', (0, 1), (0, 1), 'CENTER'),  # business name: centered under logo
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (0, 0), 2),
        ('TOPPADDING', (0, 1), (0, 1), 4),
        ('BOTTOMPADDING', (0, 1), (0, 1), 0),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 0.2*cm))

    # ---- title bar --------------------------------------------------------
    title_style = ParagraphStyle(
        'StmtTitle', parent=styles['Heading1'], fontSize=20,
        spaceAfter=0, alignment=0,
        textColor=colors.HexColor('#2c3e50'),
    )
    title_cell = Paragraph('STATEMENT OF ACCOUNT', title_style)
    title_right_style = ParagraphStyle(
        'StmtTitleRight', parent=styles['Normal'], fontSize=10,
        alignment=TA_RIGHT, textColor=colors.HexColor('#7f8c8d'),
    )
    title_right = Paragraph(f'Statement date: <b>{as_of_date.isoformat() if hasattr(as_of_date, "isoformat") else as_of_date}</b>',
                            title_right_style)
    title_row = Table([[title_cell, title_right]], colWidths=[10.5*cm, 6*cm])
    title_row.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LINEBELOW', (0, 0), (-1, -1), 1.5, colors.HexColor('#2c3e50')),
    ]))
    elements.append(title_row)
    elements.append(Spacer(1, 0.3*cm))

    # ---- bill-to block ----------------------------------------------------
    elements.append(Paragraph('<b>Statement to:</b>', styles['Heading3']))
    customer_info = f"<b>{customer.name}</b><br/>"
    if customer.contact_name:
        customer_info += f"Attn: {customer.contact_name}<br/>"
    if render_address(customer):
        customer_info += f"{render_address(customer).replace(chr(10), '<br/>')}<br/>"
    if customer.contact_email:
        customer_info += f"Email: {customer.contact_email}<br/>"
    if customer.contact_number:
        customer_info += f"Phone: {customer.contact_number}<br/>"
    if customer.abn:
        customer_info += f"ABN: {customer.abn}<br/>"
    elements.append(Paragraph(customer_info, styles['Normal']))
    elements.append(Spacer(1, 0.3*cm))

    # ---- summary block ---------------------------------------------------
    summary_label_style = ParagraphStyle(
        'StmtSummaryLbl', parent=styles['Normal'], fontSize=10,
        textColor=colors.HexColor('#7f8c8d'),
    )
    summary_val_style = ParagraphStyle(
        'StmtSummaryVal', parent=styles['Normal'], fontSize=11,
        fontName='Helvetica-Bold',
    )

    def sum_cell(label, value_str, highlight=False):
        bg = colors.HexColor('#ecf0f1') if not highlight else colors.HexColor('#34495e')
        fg_lbl = colors.HexColor('#7f8c8d') if not highlight else colors.white
        fg_val = colors.black if not highlight else colors.white
        lbl_style = ParagraphStyle('s', parent=summary_label_style, textColor=fg_lbl)
        val_style = ParagraphStyle('v', parent=summary_val_style, textColor=fg_val)
        return [Paragraph(label, lbl_style), Paragraph(value_str, val_style)]

    summary_data = [
        sum_cell('Total invoiced (inc GST)', f"${total_invoiced:,.2f}"),
        sum_cell('Total paid', f"${total_paid:,.2f}"),
        sum_cell('Outstanding balance', f"${outstanding:,.2f}", highlight=True),
    ]
    summary_table = Table(summary_data, colWidths=[5.5*cm, 4.5*cm, 5.5*cm, 1*cm])
    # We want three label/value pairs side by side, not stacked. Re-layout:
    summary_data2 = [[
        Paragraph('Total invoiced (inc GST)', summary_label_style),
        Paragraph('Total paid', summary_label_style),
        Paragraph('Outstanding balance', ParagraphStyle('h', parent=summary_label_style, textColor=colors.white)),
    ], [
        Paragraph(f"${total_invoiced:,.2f}", summary_val_style),
        Paragraph(f"${total_paid:,.2f}", summary_val_style),
        Paragraph(f"${outstanding:,.2f}", ParagraphStyle('hv', parent=summary_val_style, textColor=colors.white)),
    ]]
    summary_table = Table(summary_data2, colWidths=[5.5*cm, 4.5*cm, 5.5*cm])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#ecf0f1')),
        ('BACKGROUND', (2, 0), (2, -1), colors.HexColor('#34495e')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#bdc3c7')),
        ('LINEAFTER', (0, 0), (1, -1), 0.5, colors.HexColor('#bdc3c7')),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 0.25*cm))

    # ---- aging breakdown (only if there's anything outstanding) ----------
    if outstanding > 0:
        elements.append(Paragraph('<b>Outstanding aging</b>', styles['Heading3']))
        aging_data = [
            ['Current', '1\u201330 days', '31\u201360 days', '61\u201390 days', '90+ days'],
            [f"${aging['current']:,.2f}",
             f"${aging['1_30']:,.2f}",
             f"${aging['31_60']:,.2f}",
             f"${aging['61_90']:,.2f}",
             f"${aging['90_plus']:,.2f}"],
        ]
        aging_table = Table(aging_data, colWidths=[3.3*cm]*5)
        aging_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#34495e')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#bdc3c7')),
            ('LINEAFTER', (0, 0), (3, -1), 0.5, colors.HexColor('#bdc3c7')),
        ]))
        elements.append(aging_table)
        elements.append(Spacer(1, 0.3*cm))

    # ---- invoice line items ----------------------------------------------
    elements.append(Paragraph('<b>Invoices</b>', styles['Heading3']))

    header_row = ['Date', 'Invoice #', 'Description', 'Due', 'Amount', 'Paid', 'Balance', 'Status', 'Rem.']
    rows = [header_row]
    running_balance = Decimal('0.00')

    status_color_map = {
        'paid':    colors.HexColor('#27ae60'),
        'sent':    colors.HexColor('#e67e22'),
        'overdue': colors.HexColor('#c0392b'),
        'draft':   colors.HexColor('#95a5a6'),
    }

    for inv in invoices:
        total = D(inv.total_amount)
        paid = D(inv.amount_paid) if inv.status == 'paid' else Decimal('0.00')
        bal = total - paid
        running_balance += bal

        inv_due = inv.due_date.isoformat() if inv.due_date else '—'
        status_disp = inv.status
        if status_disp == 'sent' and inv.due_date and as_of_date > inv.due_date:
            status_disp = 'overdue'

        # truncate long descriptions
        desc = inv.description or ''
        if len(desc) > 38:
            desc = desc[:35] + '…'

        # reminder count (0 if relation isn't loaded)
        try:
            rcount = inv.reminders.count() if hasattr(inv, 'reminders') else 0
        except Exception:
            rcount = 0

        rows.append([
            inv.invoice_date.isoformat(),
            inv.id[:8].upper(),
            Paragraph(desc, ParagraphStyle('d', parent=styles['Normal'], fontSize=8.5)),
            inv_due,
            f"${total:,.2f}",
            f"${paid:,.2f}" if inv.status == 'paid' else '—',
            f"${bal:,.2f}" if bal > 0 else '—',
            status_disp.upper(),
            str(rcount) if rcount else '—',
        ])

    col_widths = [1.7*cm, 1.8*cm, 5.1*cm, 1.7*cm, 1.8*cm, 1.6*cm, 1.8*cm, 1.5*cm, 0.8*cm]
    items_table = Table(rows, colWidths=col_widths, repeatRows=1)
    ts = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#34495e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('FONTSIZE', (0, 1), (-1, -1), 8.5),
        ('ALIGN', (4, 0), (6, -1), 'RIGHT'),
        ('ALIGN', (7, 0), (7, -1), 'CENTER'),
        ('ALIGN', (0, 0), (3, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#bdc3c7')),
        ('LINEBELOW', (0, 0), (-1, 0), 1, colors.HexColor('#2c3e50')),
        ('LINEAFTER', (0, 0), (6, -1), 0.25, colors.HexColor('#bdc3c7')),
    ])
    # alternating row backgrounds, and status color highlight
    for i, inv in enumerate(invoices, start=1):
        if i % 2 == 0:
            ts.add('BACKGROUND', (0, i), (-1, i), colors.HexColor('#f8f9fa'))
        status_disp = inv.status
        if status_disp == 'sent' and inv.due_date and as_of_date > inv.due_date:
            status_disp = 'overdue'
        c = status_color_map.get(status_disp, colors.black)
        ts.add('TEXTCOLOR', (7, i), (7, i), c)
        ts.add('FONTNAME', (7, i), (7, i), 'Helvetica-Bold')
    items_table.setStyle(ts)
    elements.append(items_table)
    elements.append(Spacer(1, 0.2*cm))

    # ---- totals row ------------------------------------------------------
    totals_data = [
        ['', '', '', '', 'TOTAL INVOICED', f"${total_invoiced:,.2f}"],
        ['', '', '', '', 'TOTAL PAID', f"${total_paid:,.2f}"],
        ['', '', '', '', 'OUTSTANDING', f"${outstanding:,.2f}"],
    ]
    totals_table = Table(totals_data, colWidths=col_widths)
    totals_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (4, 0), (5, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ('LINEABOVE', (4, 0), (5, 0), 1, colors.black),
        ('LINEABOVE', (4, 2), (5, 2), 1, colors.black),
        ('FONTNAME', (4, 2), (5, 2), 'Helvetica-Bold'),
        ('TEXTCOLOR', (4, 2), (5, 2), colors.HexColor('#c0392b')),
    ]))
    elements.append(totals_table)
    elements.append(Spacer(1, 0.35*cm))

    # ---- bank details + payment terms ------------------------------------
    if outstanding > 0:
        if user.bank_name or user.account_name or user.account_number_plain or user.bsb_plain:
            # Render inline so it flows naturally across page breaks if needed.
            elements.append(Paragraph(
                '<font size="10" color="#2c3e50"><b>Payment details</b></font>',
                styles['Normal'],
            ))
            bank_bits = []
            if user.bank_name:
                bank_bits.append(f"<b>Bank:</b> {user.bank_name}")
            if user.account_name:
                bank_bits.append(f"<b>Account name:</b> {user.account_name}")
            if user.bsb_plain:
                bank_bits.append(f"<b>BSB:</b> {user.bsb_plain}")
            if user.account_number_plain:
                bank_bits.append(f"<b>Account number:</b> {user.account_number_plain}")
            bank_style = ParagraphStyle(
                'BankLine', parent=styles['Normal'], fontSize=9, leading=11,
            )
            elements.append(Paragraph(' &nbsp;|&nbsp; '.join(bank_bits), bank_style))

        payment_terms = user.payment_terms if user.payment_terms else 14
        elements.append(Spacer(1, 0.2*cm))
        elements.append(Paragraph(
            f'<i>Standard payment terms: Net {payment_terms} days. Please quote the invoice number on payment. '
            f'Thank you for your business.</i>',
            ParagraphStyle('Terms', parent=styles['Normal'], fontSize=8.5, textColor=colors.HexColor('#7f8c8d')),
        ))
    else:
        elements.append(Paragraph(
            '<i>This account is paid in full. Thank you for your prompt payment.</i>',
            styles['Normal'],
        ))

    doc.build(elements)
    buffer.seek(0)
    return buffer


def generate_payslip_pdf(user, employee, pay_event, lines=None, business_name=None, abn=None, address=None):
    """
    Generate a payslip PDF using ReportLab (already a dep and already
    loaded in the app process, so no extra process spawn).

    Layout mirrors the original payslip template:
      - PAY SLIP title
      - employer header (business name / ABN / address / phone / email)
      - employee/pay block (name, position, TFN, pay period, payment date, pay frequency)
      - Earnings & deductions table (Gross / PAYG / Net)
      - Superannuation (paid separately)
      - Payment details (bank ref, payment method)

    Args:
        user:        User row (for context — currently unused beyond defaults)
        employee:    Employee row. The TFN is decrypted here (server-side).
        pay_event:   PayEvent row.
        lines:       Optional list of PayEventLine rows. If None, builds a
                     3-row breakdown from the headline gross/payg/net.
        business_name/abn/address: optional overrides (default to user.business_name etc.)

    Returns:
        BytesIO buffer at position 0.
    """
    from reportlab.lib.enums import TA_RIGHT

    biz = business_name or (user.business_name if user else None) or 'Employer'
    biz_abn = abn or (user.abn if user else None) or ''
    biz_addr = address or (render_address(user) if user else '') or ''
    biz_email = (user.contact_email if user else None) or ''
    biz_phone = (user.contact_number if user else None) or ''

    tfn_plain = employee.tfn_plain if employee is not None else None
    position = pay_event.position_snapshot or (employee.position if employee else None) or ''
    legal_name = employee.legal_name if employee else ''
    preferred_name = employee.preferred_name if employee else ''
    name_for_payslip = preferred_name or legal_name

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=2*cm, leftMargin=2*cm,
        topMargin=1.5*cm, bottomMargin=1.5*cm,
    )
    elements = []
    styles = getSampleStyleSheet()

    # ----- Header -----
    title_style = ParagraphStyle(
        'PayslipTitle', parent=styles['Heading1'],
        fontSize=24, alignment=0, leading=28,
        textColor=colors.HexColor('#2c3e50'),
        spaceAfter=4,
    )
    elements.append(Paragraph('PAY SLIP', title_style))

    header_style = ParagraphStyle(
        'HeaderMeta', parent=styles['Normal'],
        fontSize=9, textColor=colors.HexColor('#7f8c8d'), leading=12,
    )
    header_bits = [biz]
    if biz_abn:
        header_bits.append(f'ABN {biz_abn}')
    if biz_addr:
        header_bits.append(biz_addr.replace(chr(10), ', '))
    header_line = ' \u00b7 '.join([b for b in header_bits if b])
    elements.append(Paragraph(header_line, header_style))
    if biz_phone or biz_email:
        contact_bits = []
        if biz_phone:
            contact_bits.append(f'Phone {biz_phone}')
        if biz_email:
            contact_bits.append(biz_email)
        elements.append(Paragraph(' \u00b7 '.join(contact_bits), header_style))
    elements.append(Spacer(1, 0.4*cm))

    # ----- Employee / pay block -----
    label_w = 4*cm
    val_w = 5*cm
    def row(left_label, left_val, right_label, right_val):
        cell_style_l = ParagraphStyle('LblL', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#7f8c8d'))
        cell_style_v = ParagraphStyle('ValV', parent=styles['Normal'], fontSize=10, fontName='Helvetica-Bold')
        return [Paragraph(left_label, cell_style_l), Paragraph(str(left_val), cell_style_v),
                Paragraph(right_label, cell_style_l), Paragraph(str(right_val), cell_style_v)]

    info_rows = [
        row('Employee',    name_for_payslip, 'Pay period',   f"{pay_event.pay_period_start} \u2013 {pay_event.pay_period_end}"),
        row('Position',    position,        'Payment date', str(pay_event.payment_date)),
        row('TFN',         tfn_plain or '', 'Pay frequency', pay_event.pay_frequency),
    ]
    info_table = Table(info_rows, colWidths=[label_w, val_w, label_w, val_w])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 0.3*cm))

    # ----- Earnings & deductions -----
    section_style = ParagraphStyle(
        'Section', parent=styles['Heading3'], fontSize=11,
        textColor=colors.HexColor('#2c3e50'), spaceBefore=4, spaceAfter=4,
    )
    elements.append(Paragraph('Earnings &amp; deductions', section_style))

    # Use provided lines if any, else build a 3-row breakdown.
    if lines:
        rendered_lines = []
        for ln in lines:
            rendered_lines.append([ln.description or ln.line_type, f"${float(ln.amount):,.2f}"])
    else:
        rendered_lines = [
            ['Gross wages', f"${float(pay_event.gross_amount):,.2f}"],
            ['PAYG tax withheld', f"-${float(pay_event.payg_tax_amount):,.2f}"],
            ['Net pay deposited', f"${float(pay_event.net_amount):,.2f}"],
        ]

    items_data = [['Description', 'Amount (AUD)']] + rendered_lines
    items_table = Table(items_data, colWidths=[12*cm, 4*cm])
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#34495e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ALIGN', (-1, 0), (-1, -1), 'RIGHT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('LINEBELOW', (0, 0), (-1, 0), 1, colors.HexColor('#34495e')),
        ('LINEBELOW', (0, -1), (-1, -1), 1, colors.HexColor('#2c3e50')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 0.3*cm))

    # ----- Superannuation (paid separately) -----
    super_pct = float(employee.super_rate_pct) if employee and employee.super_rate_pct is not None else 12.0
    elements.append(Paragraph('Superannuation (paid separately)', section_style))
    super_data = [
        ['Description', 'Amount (AUD)'],
        [f"Superannuation contribution @ {super_pct:.2f}% OTE", f"${float(pay_event.super_payable_amount):,.2f}"],
    ]
    super_table = Table(super_data, colWidths=[12*cm, 4*cm])
    super_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#34495e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ALIGN', (-1, 0), (-1, -1), 'RIGHT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(super_table)

    fund_name = employee.super_fund_name if employee and employee.super_fund_name else "Employee's nominated super fund"
    paid_to_style = ParagraphStyle('PaidTo', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#7f8c8d'))
    elements.append(Spacer(1, 0.15*cm))
    elements.append(Paragraph(f'Paid to: {fund_name}', paid_to_style))
    elements.append(Spacer(1, 0.4*cm))

    # ----- Payment details -----
    elements.append(Paragraph('Payment details', section_style))
    pay_rows = [
        ['Bank reference', pay_event.bank_reference or '—'],
        ['Payment method', 'Direct deposit'],
    ]
    pay_table = Table(pay_rows, colWidths=[4*cm, 12*cm])
    pay_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(pay_table)
    elements.append(Spacer(1, 0.6*cm))

    # ----- Footer -----
    footer_style = ParagraphStyle(
        'Footer', parent=styles['Normal'], fontSize=8,
        textColor=colors.HexColor('#7f8c8d'), leading=10,
    )
    elements.append(Paragraph(
        'This pay slip has been issued in accordance with the Fair Work Act 2009 '
        'and complies with the requirements of section 536. Payslip details are '
        'kept private and stored for 7 years as required by the Fair Work Regulations 2009.',
        footer_style,
    ))

    doc.build(elements)
    buffer.seek(0)
    return buffer


def generate_quote_pdf(user, quote):
    """
    Generate a PDF for a quotation and return it as a bytes buffer.

    Args:
        user: User object (with business details and logo)
        quote: Quote object (with customer)

    Returns:
        BytesIO buffer positioned at the start of the PDF data

    Deliberately mirrors the invoice layout (same logo resolution, same
    header block, same table styles) so a customer who has seen an invoice
    recognises the quote as the same document family. What it does NOT
    carry is bank details: a quote is a proposal, not a request for money.
    """
    from reportlab.lib.enums import TA_RIGHT
    from decimal import Decimal

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
    elements = []
    styles = getSampleStyleSheet()

    logo_cell = []
    _logo = _logo_image(user)
    if _logo is not None:
        logo_cell.append(_logo)

    business_paragraphs = []
    if user.business_name:
        business_paragraphs.append(f'<b>{user.business_name}</b>')
    if user.abn:
        business_paragraphs.append(f'ABN: {user.abn}')
    if render_address(user):
        business_paragraphs.append(render_address(user).replace(chr(10), '<br/>'))
    if user.contact_email:
        business_paragraphs.append(f'Email: {user.contact_email}')
    if user.contact_number:
        business_paragraphs.append(f'Phone: {user.contact_number}')

    business_style = ParagraphStyle('Business', parent=styles['Normal'], fontSize=9, alignment=TA_RIGHT, leading=12)
    business_cell = [Paragraph('<br/>'.join(business_paragraphs), business_style)] if business_paragraphs else ['']

    business_name_style = ParagraphStyle(
        'BusinessName', parent=styles['Heading2'],
        fontSize=20, alignment=0, leading=24,
        textColor=colors.HexColor('#2c3e50'),
        spaceAfter=0, spaceBefore=0
    )
    business_name_cell = [Paragraph(user.business_name, business_name_style)] if user.business_name else ['']

    header_table = Table(
        [[logo_cell, business_cell], [business_name_cell, '']],
        colWidths=[8*cm, 8*cm]
    )
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 4),
        ('TOPPADDING', (0, 1), (0, 1), 6),
        ('BOTTOMPADDING', (0, 1), (-1, 1), 0),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 0.3*cm))

    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=28, spaceAfter=0, alignment=0, textColor=colors.HexColor('#2c3e50'))
    title_cell = Paragraph('QUOTATION', title_style)
    title_row = Table([[title_cell, '']], colWidths=[12*cm, 4*cm])
    title_row.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LINEBELOW', (0, 0), (-1, -1), 1.5, colors.HexColor('#2c3e50')),
    ]))
    elements.append(title_row)
    elements.append(Spacer(1, 0.5*cm))

    info_data = [
        ['Quote Date:', str(quote.quote_date), 'Quote #:', quote.quote_number],
        ['Valid Until:', str(quote.valid_until) if quote.valid_until else 'N/A', 'Status:', quote.status.title()],
    ]
    info_table = Table(info_data, colWidths=[3*cm, 5*cm, 3*cm, 5*cm])
    info_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.grey),
        ('TEXTCOLOR', (2, 0), (2, -1), colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 0.5*cm))

    elements.append(Paragraph('<b>Quote To:</b>', styles['Heading3']))
    customer = quote.customer
    customer_info = f"<b>{customer.name}</b><br/>"
    if customer.contact_name:
        customer_info += f"Attn: {customer.contact_name}<br/>"
    if render_address(customer):
        customer_info += f"{render_address(customer).replace(chr(10), '<br/>')}<br/>"
    if customer.contact_email:
        customer_info += f"Email: {customer.contact_email}<br/>"
    if customer.contact_number:
        customer_info += f"Phone: {customer.contact_number}<br/>"
    if customer.abn:
        customer_info += f"ABN: {customer.abn}<br/>"
    elements.append(Paragraph(customer_info, styles['Normal']))
    elements.append(Spacer(1, 0.5*cm))

    items_data = [['Description', 'Amount']]
    for line in (quote.lines or []):
        items_data.append([
            Paragraph(str(line.get('description', '')), styles['Normal']),
            f"${Decimal(str(line.get('amount', 0))):,.2f}",
        ])
    items_table = Table(items_data, colWidths=[12*cm, 4*cm])
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#34495e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ALIGN', (-1, 0), (-1, -1), 'RIGHT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 0.3*cm))

    totals_data = [
        ['Subtotal (ex GST):', f"${quote.ex_gst_amount:.2f}"],
        [f'GST ({float(quote.gst_type)*100:.0f}%):', f"${quote.gst_amount:.2f}"],
        ['TOTAL:', f"${quote.total_amount:.2f}"],
    ]
    totals_table = Table(totals_data, colWidths=[12*cm, 4*cm])
    totals_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ALIGN', (-1, 0), (-1, -1), 'RIGHT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LINEABOVE', (0, -1), (-1, -1), 1, colors.black),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, -1), (-1, -1), 12),
    ]))
    elements.append(totals_table)

    # A zero-GST quote to a customer with no ABN is usually an overseas
    # customer, so say so on the document rather than leaving a bare $0.00
    # line that reads like an error.
    if Decimal(str(quote.gst_type)) == 0 and not (customer.abn or '').strip():
        elements.append(Spacer(1, 0.4*cm))
        elements.append(Paragraph(
            '<i>No GST has been applied: the customer is not Australian GST registered.</i>',
            styles['Normal']
        ))

    if quote.notes:
        elements.append(Spacer(1, 0.7*cm))
        elements.append(Paragraph('<b>Notes:</b>', styles['Heading3']))
        elements.append(Paragraph(str(quote.notes).replace(chr(10), '<br/>'), styles['Normal']))

    elements.append(Spacer(1, 1*cm))
    validity = f'This quotation is valid until {quote.valid_until}.' if quote.valid_until else 'This quotation is subject to confirmation.'
    elements.append(Paragraph(f'<i>{validity}</i>', styles['Normal']))

    doc.build(elements)
    buffer.seek(0)
    return buffer
