"""
API endpoints for pending payment reconciliations.

Remittance advice (e.g. Xero "Payment has been made" email) is the PAYER's
claim. Until the bank statement confirms the money actually landed, the
invoice must stay unpaid. This module records every remittance advice as
a row in `pending` status and provides /confirm and /reject endpoints
that the operator uses once he's checked the bank.

POST   /api/pending-payment-reconciliations
GET    /api/pending-payment-reconciliations
GET    /api/pending-payment-reconciliations/<id>
PATCH  /api/pending-payment-reconciliations/<id>      — notes only
POST   /api/pending-payment-reconciliations/<id>/confirm
POST   /api/pending-payment-reconciliations/<id>/reject
"""
from datetime import date, datetime, timezone
from decimal import Decimal

from flask import Blueprint, request, jsonify
from sqlalchemy import or_

from app.models import (
    db,
    get_utc_now,
    PendingPaymentReconciliation,
    PENDING_RECONCILIATION_STATUSES,
)
from app.models.invoice import Invoice
from app.models.customer import Customer
from app.shared.decorators import api_key_required, log_activity


payment_recon_bp = Blueprint('payment_reconciliations', __name__, url_prefix='/api')


def _parse_date(value, field_name, required=False):
    if not value:
        if required:
            from flask import abort
            abort(400, description=f'{field_name} is required')
        return None
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        from flask import abort
        abort(400, description=f'{field_name} must be YYYY-MM-DD')


def _parse_decimal(value, field_name):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except Exception:
        from flask import abort
        abort(400, description=f'{field_name} is not a valid number')


# ----------------------------------------------------------------------------
# POST /api/pending-payment-reconciliations  (manual create; the script uses this)
# ----------------------------------------------------------------------------

@payment_recon_bp.route('/pending-payment-reconciliations', methods=['POST'])
@api_key_required
def create_reconciliation():
    """
    Record a new pending reconciliation. Typically called by the Xero-email
    scanner script after it extracts the remittance advice PDF.

    Body:
      customer_id           str  required
      invoice_id            str  optional — set if invoice_ref_token matched
      invoice_ref_token     str  the 8-char prefix the script extracted
      reference_text        str  the raw "5C1A4C6F - SOFTWARE" line
      amount                num  in amount_currency
      amount_currency       str  default 'AUD'
      payment_date          ISO date
      sent_date             ISO date  (when the remittance was generated)
      payer_name            str
      payer_abn             str
      source_email_id       str  mail server message id
      source_sender         str
      source_subject        str
      pdf_attachment_path   str
      notes                 str
    """
    data = request.get_json() or {}

    customer_id = data.get('customer_id')
    if not customer_id:
        return jsonify({'error': 'customer_id is required'}), 400
    if not Customer.query.filter_by(id=customer_id, user_id=request.current_user.id).first():
        return jsonify({'error': 'Customer not found'}), 404

    invoice_id = data.get('invoice_id')
    if invoice_id:
        inv = Invoice.query.filter_by(id=invoice_id, user_id=request.current_user.id).first()
        if not inv:
            return jsonify({'error': 'Invoice not found'}), 404

    row = PendingPaymentReconciliation(
        user_id=request.current_user.id,
        customer_id=customer_id,
        invoice_id=invoice_id,
        invoice_ref_token=(data.get('invoice_ref_token') or '').upper()[:8] or None,
        reference_text=data.get('reference_text'),
        amount=_parse_decimal(data.get('amount'), 'amount'),
        amount_currency=data.get('amount_currency') or 'AUD',
        payment_date=_parse_date(data.get('payment_date'), 'payment_date'),
        sent_date=_parse_date(data.get('sent_date'), 'sent_date'),
        payer_name=data.get('payer_name'),
        payer_abn=data.get('payer_abn'),
        source_email_id=data.get('source_email_id'),
        source_sender=data.get('source_sender'),
        source_subject=data.get('source_subject'),
        pdf_attachment_path=data.get('pdf_attachment_path'),
        notes=data.get('notes'),
        status='pending',
    )
    db.session.add(row)
    db.session.flush()

    log_activity(
        user_id=request.current_user.id,
        action='CREATE',
        table_name='pending_payment_reconciliations',
        record_id=row.id,
        new_values={
            'invoice_ref_token': row.invoice_ref_token,
            'amount':            str(row.amount) if row.amount else None,
            'payment_date':      row.payment_date.isoformat() if row.payment_date else None,
            'invoice_id':        row.invoice_id,
        },
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return jsonify({'message': 'Reconciliation recorded', 'reconciliation': row.to_dict()}), 201


# ----------------------------------------------------------------------------
# GET /api/pending-payment-reconciliations
# ----------------------------------------------------------------------------

@payment_recon_bp.route('/pending-payment-reconciliations', methods=['GET'])
@api_key_required
def list_reconciliations():
    q = PendingPaymentReconciliation.query.filter_by(user_id=request.current_user.id)

    status = request.args.get('status')
    if status:
        statuses = request.args.getlist('status')
        if len(statuses) == 1:
            q = q.filter(PendingPaymentReconciliation.status == statuses[0])
        else:
            q = q.filter(PendingPaymentReconciliation.status.in_(statuses))
    else:
        # Default: only show actionable rows
        pass

    if request.args.get('customer_id'):
        q = q.filter(PendingPaymentReconciliation.customer_id == request.args['customer_id'])
    if request.args.get('invoice_id'):
        q = q.filter(PendingPaymentReconciliation.invoice_id == request.args['invoice_id'])
    if request.args.get('invoice_ref_token'):
        q = q.filter(PendingPaymentReconciliation.invoice_ref_token == request.args['invoice_ref_token'].upper()[:8])

    try:
        limit = max(1, min(int(request.args.get('limit', 100)), 500))
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        return jsonify({'error': 'limit/offset must be integers'}), 400

    total = q.count()
    rows  = q.order_by(PendingPaymentReconciliation.created_at.desc()).limit(limit).offset(offset).all()
    return jsonify({
        'reconciliations': [r.to_dict() for r in rows],
        'total':           total,
        'limit':           limit,
        'offset':          offset,
    }), 200


# ----------------------------------------------------------------------------
# GET /api/pending-payment-reconciliations/<id>
# ----------------------------------------------------------------------------

@payment_recon_bp.route('/pending-payment-reconciliations/<recon_id>', methods=['GET'])
@api_key_required
def get_reconciliation(recon_id):
    row = PendingPaymentReconciliation.query.filter_by(id=recon_id, user_id=request.current_user.id).first()
    if not row:
        return jsonify({'error': 'Reconciliation not found'}), 404
    return jsonify({'reconciliation': row.to_dict()}), 200


# ----------------------------------------------------------------------------
# PATCH /api/pending-payment-reconciliations/<id>  (notes only)
# ----------------------------------------------------------------------------

@payment_recon_bp.route('/pending-payment-reconciliations/<recon_id>', methods=['PATCH'])
@api_key_required
def update_reconciliation(recon_id):
    row = PendingPaymentReconciliation.query.filter_by(id=recon_id, user_id=request.current_user.id).first()
    if not row:
        return jsonify({'error': 'Reconciliation not found'}), 404
    if row.status not in ('pending', 'stale'):
        return jsonify({'error': f'Cannot edit notes on a {row.status} reconciliation. Use /confirm or /reject.'}), 400
    data = request.get_json() or {}
    if 'notes' in data:
        row.notes = data['notes']
    db.session.commit()
    return jsonify({'message': 'Updated', 'reconciliation': row.to_dict()}), 200


# ----------------------------------------------------------------------------
# POST /api/pending-payment-reconciliations/<id>/confirm
# ----------------------------------------------------------------------------

@payment_recon_bp.route('/pending-payment-reconciliations/<recon_id>/confirm', methods=['POST'])
@api_key_required
def confirm_reconciliation(recon_id):
    """
    Confirm the payment landed in the bank account. Side effects:
      - Sets status='confirmed', reconciled_at=now, reconciled_by=request body 'by'
      - If invoice_id is set and the invoice is unpaid, marks it paid with
        amount + payment_date from this reconciliation.
      - Idempotent: if the invoice is already paid, just records confirmation.
    """
    row = PendingPaymentReconciliation.query.filter_by(id=recon_id, user_id=request.current_user.id).first()
    if not row:
        return jsonify({'error': 'Reconciliation not found'}), 404
    if row.status not in ('pending', 'stale'):
        return jsonify({'error': f'Cannot confirm a {row.status} reconciliation.'}), 400

    data = request.get_json() or {}
    old = row.to_dict()

    row.status        = 'confirmed'
    row.reconciled_at = get_utc_now()
    row.reconciled_by = data.get('by') or 'agent'
    if data.get('bank_reference'):
        row.bank_reference = data['bank_reference']
    if data.get('bank_screenshot_path'):
        row.bank_screenshot_path = data['bank_screenshot_path']
    if data.get('notes'):
        row.notes = (old.get('notes') or '') + ('\n' if old.get('notes') else '') + data['notes']

    invoice_after = None
    if row.invoice_id:
        inv = Invoice.query.filter_by(id=row.invoice_id, user_id=request.current_user.id).first()
        if inv:
            old_inv = inv.to_dict()
            if inv.status != 'paid':
                # Mark the invoice paid with the remittance-advice amount + date
                inv.status        = 'paid'
                inv.amount_paid   = row.amount if row.amount is not None else inv.total_amount
                inv.paid_at       = get_utc_now()
                inv.payment_date  = row.payment_date or date.today()
                # Log to activity
                log_activity(
                    user_id=request.current_user.id,
                    action='UPDATE',
                    table_name='invoices',
                    record_id=inv.id,
                    old_values=old_inv,
                    new_values=inv.to_dict(),
                    ip_address=request.remote_addr,
                )
            invoice_after = inv.to_dict()

    log_activity(
        user_id=request.current_user.id,
        action='CONFIRM',
        table_name='pending_payment_reconciliations',
        record_id=recon_id,
        old_values={k: old.get(k) for k in ('status', 'reconciled_at', 'reconciled_by', 'bank_reference', 'notes')},
        new_values={
            'status':         row.status,
            'reconciled_at':  row.reconciled_at.isoformat(),
            'reconciled_by':  row.reconciled_by,
            'bank_reference': row.bank_reference,
        },
        ip_address=request.remote_addr,
    )
    db.session.commit()

    return jsonify({
        'message':         'Reconciliation confirmed',
        'reconciliation':  row.to_dict(),
        'invoice':         invoice_after,
    }), 200


# ----------------------------------------------------------------------------
# POST /api/pending-payment-reconciliations/<id>/reject
# ----------------------------------------------------------------------------

@payment_recon_bp.route('/pending-payment-reconciliations/<recon_id>/reject', methods=['POST'])
@api_key_required
def reject_reconciliation(recon_id):
    """
    Reject the reconciliation: bank shows no matching payment, or the
    reference doesn't match any invoice, or the amount differs. Records
    the reason. Does NOT mark any invoice paid.
    """
    row = PendingPaymentReconciliation.query.filter_by(id=recon_id, user_id=request.current_user.id).first()
    if not row:
        return jsonify({'error': 'Reconciliation not found'}), 404
    if row.status not in ('pending', 'stale'):
        return jsonify({'error': f'Cannot reject a {row.status} reconciliation.'}), 400

    data = request.get_json() or {}
    old = row.to_dict()

    row.status        = 'rejected'
    row.reconciled_at = get_utc_now()
    row.reconciled_by = data.get('by') or 'agent'
    if data.get('bank_reference'):
        row.bank_reference = data['bank_reference']
    if data.get('notes'):
        row.notes = (old.get('notes') or '') + ('\n' if old.get('notes') else '') + data['notes']

    log_activity(
        user_id=request.current_user.id,
        action='REJECT',
        table_name='pending_payment_reconciliations',
        record_id=recon_id,
        old_values={k: old.get(k) for k in ('status', 'reconciled_at', 'reconciled_by', 'bank_reference', 'notes')},
        new_values={
            'status':         row.status,
            'reconciled_at':  row.reconciled_at.isoformat(),
            'reconciled_by':  row.reconciled_by,
            'bank_reference': row.bank_reference,
        },
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return jsonify({'message': 'Reconciliation rejected', 'reconciliation': row.to_dict()}), 200


# ----------------------------------------------------------------------------
# GET /api/pending-payment-reconciliations/options  (UI form helper)
# ----------------------------------------------------------------------------

@payment_recon_bp.route('/pending-payment-reconciliations/options', methods=['GET'])
@api_key_required
def reconciliation_options():
    return jsonify({
        'statuses': list(PENDING_RECONCILIATION_STATUSES),
    }), 200
