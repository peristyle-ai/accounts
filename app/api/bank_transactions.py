"""
API endpoints for bank transactions (received payments).

  POST   /api/bank-transactions                 — record a new bank txn (optional invoice_id)
  GET    /api/bank-transactions                 — list (filter by invoice_id, date range, method)
  GET    /api/bank-transactions/<id>            — get one
  PUT    /api/bank-transactions/<id>            — update notes/metadata (immutable once paid)
  DELETE /api/bank-transactions/<id>            — delete (admin only)

When invoice_id is supplied, the bank transaction is recorded AS WELL AS
marking the linked invoice 'paid' (atomic — both succeed or both fail).
This replaces the older /api/invoices/<id>/mark-paid workflow with proper
provenance. The old endpoint stays for backwards compat.

The model is defined in app.models.bank_transaction.BankTransaction.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import logging

from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import and_

logger = logging.getLogger(__name__)

from app.models import db, get_utc_now
from app.models.bank_transaction import BankTransaction, BANK_TXN_METHODS, BANK_TXN_SOURCES
from app.models.invoice import Invoice
from app.models.user import User
from app.shared.decorators import api_key_required, log_activity
from app.shared.roles import role_required

bank_txn_bp = Blueprint('bank_transactions', __name__, url_prefix='/api/bank-transactions')


def _parse_decimal(value, field_name):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'{field_name} must be a decimal number, got {value!r}')


def _parse_date(value, field_name, required=True):
    if value is None or value == '':
        if required:
            raise ValueError(f'{field_name} is required')
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise ValueError(f'{field_name} must be YYYY-MM-DD, got {value!r}')


def _parse_datetime(value, field_name):
    if value is None or value == '':
        return None
    try:
        # Accept ISO 8601 with or without tz; assume UTC if naive
        if isinstance(value, str) and value.endswith('Z'):
            value = value[:-1] + '+00:00'
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        raise ValueError(f'{field_name} must be ISO-8601 datetime, got {value!r}')


def _validate_method(value):
    if value is None or value == '':
        return None
    if value not in BANK_TXN_METHODS:
        raise ValueError(f'method must be one of {BANK_TXN_METHODS}, got {value!r}')
    return value


def _validate_source(value):
    if value is None or value == '':
        return 'manual'
    if value not in BANK_TXN_SOURCES:
        raise ValueError(f'raw_source must be one of {BANK_TXN_SOURCES}, got {value!r}')
    return value


@bank_txn_bp.route('', methods=['POST'])
@api_key_required
def create_bank_transaction():
    """Record a new received bank transaction.

    Body (JSON):
      invoice_id         optional — if supplied, the linked invoice is marked paid
      amount             required
      transaction_date   required — YYYY-MM-DD
      currency           optional — default 'AUD'
      transaction_id     optional — bank-side txn ID
      reference          optional — payer-supplied reference string
      method             optional — one of BANK_TXN_METHODS
      payer_name         optional
      payer_account      optional
      settled_at         optional — ISO-8601 datetime
      notes              optional
      raw_source         optional — default 'manual'
    """
    data = request.get_json() or {}

    try:
        amount = _parse_decimal(data.get('amount'), 'amount')
        if amount is None:
            raise ValueError('amount is required')
        if amount <= 0:
            raise ValueError('amount must be positive')
        transaction_date = _parse_date(data.get('transaction_date'), 'transaction_date')
        method = _validate_method(data.get('method'))
        raw_source = _validate_source(data.get('raw_source'))
        settled_at = _parse_datetime(data.get('settled_at'), 'settled_at')
    except ValueError as e:
        logger.warning('Validation error in create_bank_transaction: %s', e)
        return jsonify({'error': 'Invalid input'}), 400

    invoice_id = data.get('invoice_id')
    invoice = None
    auto_mark_paid = False
    if invoice_id:
        if not invoice_id.replace('-', '').replace('_', '').isalnum() or len(invoice_id) != 36:
            return jsonify({'error': 'invoice_id must be a UUID'}), 400
        invoice = Invoice.query.filter_by(id=invoice_id, user_id=request.current_user.id).first()
        if not invoice:
            return jsonify({'error': 'Invoice not found'}), 404
        if invoice.status == 'cancelled':
            return jsonify({'error': 'Cannot record payment on a cancelled invoice'}), 409
        # Auto-mark invoice paid ONLY when invoice is still open (sent/draft).
        # If already paid, we just link the bank txn to it (backfill / late
        # reconciliation) without touching the invoice's paid record.
        if invoice.status in ('sent', 'draft'):
            auto_mark_paid = True

    txn = BankTransaction(
        user_id=request.current_user.id,
        invoice_id=invoice.id if invoice else None,
        transaction_id=data.get('transaction_id') or None,
        reference=data.get('reference') or None,
        method=method,
        payer_name=data.get('payer_name') or None,
        payer_account=data.get('payer_account') or None,
        amount=amount,
        currency=data.get('currency') or 'AUD',
        transaction_date=transaction_date,
        settled_at=settled_at,
        notes=data.get('notes') or None,
        raw_source=raw_source,
    )
    db.session.add(txn)

    old_invoice_values = None
    if auto_mark_paid:
        old_invoice_values = invoice.to_dict()
        invoice.payment_date = transaction_date
        invoice.amount_paid = amount
        invoice.status = 'paid'
        invoice.paid_at = get_utc_now()
    elif invoice:
        # Already paid — just link the bank txn to the existing invoice.
        txn.invoice_id = invoice.id

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error('Database error in create_bank_transaction: %s', e, exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

    log_activity(
        user_id=request.current_user.id,
        action='CREATE',
        table_name='bank_transactions',
        record_id=txn.id,
        ip_address=request.remote_addr,
    )
    if invoice:
        log_activity(
            user_id=request.current_user.id,
            action='UPDATE',
            table_name='invoices',
            record_id=invoice.id,
            old_values=old_invoice_values,
            new_values=invoice.to_dict(),
            ip_address=request.remote_addr,
        )
    # Second commit for the activity log entries (the first commit above
    # only flushed the bank_transaction + any invoice update; the logs were
    # added to the session AFTER that).
    try:
        db.session.commit()
    except Exception as e:
        # The bank_transaction + invoice changes already succeeded; a failure
        # here means the activity log didn't get written. Log loudly but don't
        # fail the request - the caller has the IDs they need.
        current_app.logger.error(f'activity_log commit failed: {e}', exc_info=True)

    return jsonify({
        'message': 'Bank transaction recorded' + (' and invoice marked paid' if auto_mark_paid else ' (linked to already-paid invoice)' if invoice else ''),
        'bank_transaction': txn.to_dict(),
        'invoice': invoice.to_dict() if invoice else None,
    }), 201


@bank_txn_bp.route('', methods=['GET'])
@api_key_required
def list_bank_transactions():
    """List bank transactions for the current user.

    Query params (all optional):
      invoice_id         filter to one invoice
      method             filter to one method (osko/bpay/etc)
      date_from          YYYY-MM-DD inclusive
      date_to            YYYY-MM-DD inclusive
      limit              default 100, max 500
    """
    q = BankTransaction.query.filter_by(user_id=request.current_user.id)

    invoice_id = request.args.get('invoice_id')
    if invoice_id:
        q = q.filter_by(invoice_id=invoice_id)

    method = request.args.get('method')
    if method:
        q = q.filter_by(method=method)

    try:
        date_from = _parse_date(request.args.get('date_from'), 'date_from', required=False)
        date_to = _parse_date(request.args.get('date_to'), 'date_to', required=False)
    except ValueError as e:
        logger.warning('Validation error in list_bank_transactions: %s', e)
        return jsonify({'error': 'Invalid date format'}), 400
    if date_from:
        q = q.filter(BankTransaction.transaction_date >= date_from)
    if date_to:
        q = q.filter(BankTransaction.transaction_date <= date_to)

    try:
        limit = min(int(request.args.get('limit', 100)), 500)
    except ValueError:
        limit = 100

    txns = q.order_by(BankTransaction.transaction_date.desc(), BankTransaction.created_at.desc()).limit(limit).all()
    return jsonify({
        'bank_transactions': [t.to_dict() for t in txns],
        'count': len(txns),
        'limit': limit,
    }), 200


@bank_txn_bp.route('/<txn_id>', methods=['GET'])
@api_key_required
def get_bank_transaction(txn_id):
    txn = BankTransaction.query.filter_by(id=txn_id, user_id=request.current_user.id).first()
    if not txn:
        return jsonify({'error': 'Bank transaction not found'}), 404
    return jsonify({'bank_transaction': txn.to_dict()}), 200


@bank_txn_bp.route('/<txn_id>', methods=['PUT'])
@api_key_required
def update_bank_transaction(txn_id):
    """Update mutable fields on a recorded bank transaction.

    Immutable: amount, transaction_date, invoice_id, transaction_id (these
    are provenance of the bank record itself — if it was wrong, delete and
    re-record). Mutable: notes, method, payer_name, payer_account.

    Does NOT touch the linked invoice (that would be double-bookkeeping).
    """
    txn = BankTransaction.query.filter_by(id=txn_id, user_id=request.current_user.id).first()
    if not txn:
        return jsonify({'error': 'Bank transaction not found'}), 404

    data = request.get_json() or {}
    old_values = txn.to_dict()

    try:
        if 'method' in data:
            txn.method = _validate_method(data['method'])
        if 'notes' in data:
            txn.notes = data['notes'] or None
        if 'payer_name' in data:
            txn.payer_name = data['payer_name'] or None
        if 'payer_account' in data:
            txn.payer_account = data['payer_account'] or None
    except ValueError as e:
        logger.warning('Validation error in update_bank_transaction: %s', e)
        return jsonify({'error': 'Invalid input'}), 400

    db.session.commit()

    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='bank_transactions',
        record_id=txn.id,
        old_values=old_values,
        new_values=txn.to_dict(),
        ip_address=request.remote_addr,
    )

    return jsonify({'message': 'Bank transaction updated', 'bank_transaction': txn.to_dict()}), 200


@bank_txn_bp.route('/<txn_id>', methods=['DELETE'])
@api_key_required
@role_required('admin')
def delete_bank_transaction(txn_id):
    """Delete a bank transaction record.

    WARNING: does NOT unmark the linked invoice as paid. The invoice payment
    record is independent (amount_paid / payment_date / paid_at on the
    Invoice row). If you delete this and want to undo the invoice payment
    too, do it explicitly via the invoice endpoints. We split the lifecycle
    so we don't lose the bank record when an invoice gets re-opened.
    """
    txn = BankTransaction.query.filter_by(id=txn_id, user_id=request.current_user.id).first()
    if not txn:
        return jsonify({'error': 'Bank transaction not found'}), 404

    old_values = txn.to_dict()
    db.session.delete(txn)
    db.session.commit()

    log_activity(
        user_id=request.current_user.id,
        action='DELETE',
        table_name='bank_transactions',
        record_id=txn.id,
        old_values=old_values,
        ip_address=request.remote_addr,
    )

    return jsonify({'message': 'Bank transaction deleted', 'id': txn_id}), 200
