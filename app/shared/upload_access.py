"""Tenant scoping for files served out of the upload folder.

The 2026-10-25 security sweep found that `/uploads/<path>` required a
logged-in user but applied **no tenant check** (S8): any authenticated
account could fetch any other account's payslip PDF, receipt or logo.

Ownership is resolved from the data, not from the request path alone:

  * ``payslips/<user_id>/<file>`` -- the path segment IS the owner.
  * everything else -- the stored path column of the record that owns
    the file is consulted (expenses, invoices, pay events, logos, OCR
    queue, BAS lodgements, payment reconciliations, invoice reminders).

If ownership cannot be established, the file is NOT served. Denying by
default is the only safe answer to "who does this belong to?".
"""

import json


def _normalise_stored(value):
    """Normalise a stored path to the form used under the upload folder.

    Stored values appear in several shapes:
      'uploads/receipts/x.jpg'
      'receipts/x.jpg'
      '/home/somewhere/uploads/receipts/x.jpg'
    All three normalise to 'receipts/x.jpg'.
    """
    if not value:
        return None
    text = str(value).replace('\\', '/').strip()
    if not text:
        return None
    marker = 'uploads/'
    idx = text.rfind(marker)
    if idx != -1:
        text = text[idx + len(marker):]
    return text.lstrip('/')


def _owner_from_database(rel_path):
    """Look up the owning user_id for `rel_path` in the record tables."""
    from app.models.expense import Expense
    from app.models.invoice import Invoice
    from app.models.pay_event import PayEvent
    from app.models.user_profile import UserProfile

    candidates = (rel_path, f'uploads/{rel_path}')

    def _match(model, column, owner_attr='user_id'):
        for value in candidates:
            try:
                row = model.query.filter(getattr(model, column) == value).first()
            except Exception:
                row = None
            if row is not None:
                owner = getattr(row, owner_attr, None)
                if owner is not None:
                    return int(owner)
        # Absolute paths (older records) end with the relative path.
        try:
            rows = model.query.filter(getattr(model, column).like(f'%/{rel_path}')).all()
        except Exception:
            rows = []
        for row in rows:
            owner = getattr(row, owner_attr, None)
            if owner is not None:
                return int(owner)
        return None

    checks = (
        (Expense, 'attachment_path'),
        (Invoice, 'attachment_path'),
        (PayEvent, 'payslip_pdf_path'),
        (UserProfile, 'logo_path'),
    )
    for model, column in checks:
        owner = _match(model, column)
        if owner is not None:
            return owner

    # --- records that reach the owner indirectly -------------------------
    from app.models.ocr_queue import OcrQueue
    from app.models.bas_lodgement import BasLodgement
    from app.models.pending_payment_reconciliation import PendingPaymentReconciliation
    from app.models.invoice_reminder import InvoiceReminder

    owner = _match(BasLodgement, 'screenshot_path')
    if owner is not None:
        return owner

    owner = _match(PendingPaymentReconciliation, 'bank_screenshot_path')
    if owner is not None:
        return owner

    owner = _match(PendingPaymentReconciliation, 'pdf_attachment_path')
    if owner is not None:
        return owner

    owner = _match(InvoiceReminder, 'pdf_attachment_path')
    if owner is not None:
        return owner

    # OCR queue rows point at a receipt image; the owner is the expense's.
    for value in candidates:
        try:
            row = OcrQueue.query.filter(OcrQueue.image_path == value).first()
        except Exception:
            row = None
        if row is not None and getattr(row, 'expense', None) is not None:
            owner = getattr(row.expense, 'user_id', None)
            if owner is not None:
                return int(owner)
    return None


def resolve_upload_owner(rel_path):
    """Return the user_id that owns `rel_path`, or None if unknown.

    `rel_path` is the path as requested under /uploads/, with or without
    a leading 'uploads/' segment.
    """
    if not rel_path:
        return None
    rel = str(rel_path).replace('\\', '/').strip().lstrip('/')
    if rel.startswith('uploads/'):
        rel = rel[len('uploads/'):]
    parts = [p for p in rel.split('/') if p]
    if len(parts) < 2:
        # A bare filename at the root of the upload folder belongs to
        # nobody we can prove. Refuse.
        return None

    if parts[0] == 'payslips':
        if len(parts) < 3:
            return None
        try:
            return int(parts[1])
        except (TypeError, ValueError):
            return None

    normalised = _normalise_stored(rel)
    if not normalised:
        return None
    return _owner_from_database(normalised)
