"""
API endpoints for quotations / estimates.

  GET    /api/quotes                      -- list (filter by status, customer_id)
  POST   /api/quotes                      -- create a quote from line items
  GET    /api/quotes/<id>                 -- get one
  PUT    /api/quotes/<id>                 -- update a draft quote
  DELETE /api/quotes/<id>                 -- delete a draft quote (admin only)
  GET    /api/quotes/<id>/pdf             -- render the quote PDF
  POST   /api/quotes/<id>/mark-sent       -- quote sent to the customer
  POST   /api/quotes/<id>/accept          -- customer accepted
  POST   /api/quotes/<id>/decline         -- customer declined
  POST   /api/quotes/<id>/convert-to-invoice -- turn an accepted quote into an invoice

Design notes
------------
Amounts are NEVER accepted raw. The caller supplies line items; ex-GST, GST
and total are derived here (see `Quote.normalise_lines` and
`Quote.calculate_gst`). If the arithmetic could drift from the lines the
customer sees, the quote is not a quote -- it is a number.

Tenant scoping: every query is filtered by `user_id`, so a quote belonging to
another tenant is a 404, not a 403 (matching the rest of the API).

The lifecycle transitions are separate endpoints rather than a free-text
status update, so an illegal transition (accepted -> draft, say) is
impossible to express.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from flask import Blueprint, request, jsonify, send_file

logger = logging.getLogger(__name__)

from app.models import db, get_utc_now
from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.quote import Quote, QUOTE_STATUSES
from app.shared.decorators import api_key_required, log_activity
from app.shared.roles import role_required
from app.shared.validators import validate_date_string, validate_uuid

quotes_bp = Blueprint('quotes', __name__, url_prefix='/api/quotes')

# Default validity of a quote, in days, when the caller does not say.
DEFAULT_VALID_DAYS = 30


def _get_quote(quote_id):
    """Fetch a quote belonging to the caller, or None."""
    if not validate_uuid(quote_id):
        return None, 400
    quote = Quote.query.filter_by(id=quote_id, user_id=request.current_user.id).first()
    if not quote:
        return None, 404
    return quote, 200


def _parse_lines(data):
    """Return (lines, error)."""
    raw = data.get('lines')
    if raw is None:
        return None, 'Line items are required'
    lines, error = Quote.normalise_lines(raw)
    if error:
        return None, error
    return lines, None


def _derive_amounts(lines, gst_type):
    ex_gst = sum((Decimal(line['amount']) for line in lines), Decimal('0')).quantize(Decimal('0.01'))
    gst = Quote.calculate_gst(ex_gst, gst_type)
    total = Quote.calculate_total(ex_gst, gst)
    return ex_gst, gst, total


def _lines_to_description(lines):
    """Encode quote lines into the invoice description format.

    The invoice PDF understands "text — $amount" lines separated by <br/>
    (see `_parse_line_items` in app/shared/pdf.py). Encoding here means a
    converted quote renders on the invoice PDF with the same line items,
    without the invoice side needing to know anything about quotes.
    """
    return '<br/>'.join(f"{line['description']} — ${Decimal(line['amount']):,.2f}" for line in lines)


@quotes_bp.route('', methods=['GET'])
@api_key_required
def list_quotes():
    user = request.current_user
    query = Quote.query.filter_by(user_id=user.id)

    status = request.args.get('status')
    if status:
        if status not in QUOTE_STATUSES:
            return jsonify({'error': f'Invalid status. Must be one of: {", ".join(QUOTE_STATUSES)}'}), 400
        query = query.filter(Quote.status == status)

    customer_id = request.args.get('customer_id')
    if customer_id:
        if not validate_uuid(customer_id):
            return jsonify({'error': 'Invalid customer ID'}), 400
        query = query.filter(Quote.customer_id == customer_id)

    quotes = query.order_by(Quote.quote_date.desc(), Quote.created_at.desc()).all()
    return jsonify({'quotes': [q.to_dict() for q in quotes]}), 200


@quotes_bp.route('', methods=['POST'])
@api_key_required
def create_quote():
    user = request.current_user
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    customer_id = str(data.get('customer_id', '')).strip()
    if not customer_id:
        return jsonify({'error': 'Customer ID is required'}), 400
    if not validate_uuid(customer_id):
        return jsonify({'error': 'Invalid customer ID'}), 400
    customer = Customer.query.filter_by(id=customer_id, user_id=user.id).first()
    if not customer:
        return jsonify({'error': 'Customer not found'}), 404

    lines, error = _parse_lines(data)
    if error:
        return jsonify({'error': error}), 400

    try:
        from app.shared.validators import validate_gst_type
        gst_type = validate_gst_type(data.get('gst_type', 0))
        quote_date = validate_date_string(data.get('quote_date'), required=True)
        valid_until = validate_date_string(data.get('valid_until'))
    except ValueError:
        return jsonify({'error': 'Invalid input'}), 400

    if valid_until is None:
        valid_until = quote_date + timedelta(days=DEFAULT_VALID_DAYS)
    if valid_until < quote_date:
        return jsonify({'error': 'valid_until cannot be before quote_date'}), 400

    client_name = str(data.get('client_name') or customer.name).strip()
    if not client_name:
        return jsonify({'error': 'Client name is required'}), 400

    ex_gst, gst, total = _derive_amounts(lines, gst_type)

    quote = Quote(
        user_id=user.id,
        customer_id=customer_id,
        quote_number=Quote.next_quote_number(user.id, quote_date),
        client_name=client_name,
        lines=lines,
        ex_gst_amount=ex_gst,
        gst_amount=gst,
        gst_type=gst_type,
        total_amount=total,
        quote_date=quote_date,
        valid_until=valid_until,
        status='draft',
        notes=data.get('notes'),
    )

    db.session.add(quote)
    db.session.flush()

    log_activity(
        user_id=user.id,
        action='CREATE',
        table_name='quotes',
        record_id=quote.id,
        new_values=quote.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()

    return jsonify({'message': 'Quote created', 'quote': quote.to_dict()}), 201


@quotes_bp.route('/<quote_id>', methods=['GET'])
@api_key_required
def get_quote(quote_id):
    quote, status = _get_quote(quote_id)
    if quote is None:
        return jsonify({'error': 'Invalid quote ID' if status == 400 else 'Quote not found'}), status
    return jsonify({'quote': quote.to_dict()}), 200


@quotes_bp.route('/<quote_id>', methods=['PUT'])
@api_key_required
def update_quote(quote_id):
    quote, status = _get_quote(quote_id)
    if quote is None:
        return jsonify({'error': 'Invalid quote ID' if status == 400 else 'Quote not found'}), status

    if quote.status != 'draft':
        return jsonify({
            'error': f'Only a draft quote can be edited (this one is {quote.status})',
            'status': quote.status,
        }), 409

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    old_values = quote.to_dict()

    if 'lines' in data:
        lines, error = _parse_lines(data)
        if error:
            return jsonify({'error': error}), 400
        quote.lines = lines

    try:
        from app.shared.validators import validate_gst_type
        if 'gst_type' in data:
            quote.gst_type = validate_gst_type(data.get('gst_type'))
        if 'quote_date' in data:
            quote.quote_date = validate_date_string(data.get('quote_date'), required=True)
        if 'valid_until' in data:
            quote.valid_until = validate_date_string(data.get('valid_until'))
    except ValueError:
        return jsonify({'error': 'Invalid input'}), 400

    if 'notes' in data:
        quote.notes = data.get('notes')
    if 'client_name' in data:
        client_name = str(data.get('client_name') or '').strip()
        if not client_name:
            return jsonify({'error': 'Client name is required'}), 400
        quote.client_name = client_name

    if quote.valid_until and quote.quote_date and quote.valid_until < quote.quote_date:
        return jsonify({'error': 'valid_until cannot be before quote_date'}), 400

    ex_gst, gst, total = _derive_amounts(quote.lines or [], quote.gst_type)
    quote.ex_gst_amount = ex_gst
    quote.gst_amount = gst
    quote.total_amount = total

    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='quotes',
        record_id=quote.id,
        old_values=old_values,
        new_values=quote.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()

    return jsonify({'message': 'Quote updated', 'quote': quote.to_dict()}), 200


@quotes_bp.route('/<quote_id>', methods=['DELETE'])
@api_key_required
@role_required('admin')
def delete_quote(quote_id):
    quote, status = _get_quote(quote_id)
    if quote is None:
        return jsonify({'error': 'Invalid quote ID' if status == 400 else 'Quote not found'}), status

    if quote.status != 'draft':
        return jsonify({
            'error': f'Only a draft quote can be deleted (this one is {quote.status})',
            'status': quote.status,
        }), 409

    old_values = quote.to_dict()
    quote_id_value = quote.id
    db.session.delete(quote)

    log_activity(
        user_id=request.current_user.id,
        action='DELETE',
        table_name='quotes',
        record_id=quote_id_value,
        old_values=old_values,
        ip_address=request.remote_addr,
    )
    db.session.commit()

    return jsonify({'message': 'Quote deleted'}), 200


@quotes_bp.route('/<quote_id>/pdf', methods=['GET'])
@api_key_required
def generate_quote_pdf(quote_id):
    quote, status = _get_quote(quote_id)
    if quote is None:
        return jsonify({'error': 'Invalid quote ID' if status == 400 else 'Quote not found'}), status

    from app.shared.pdf import generate_quote_pdf as build_pdf
    buffer = build_pdf(request.current_user, quote)

    return send_file(
        buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'quote_{quote.quote_number}_{quote.quote_date}.pdf',
    )


@quotes_bp.route('/<quote_id>/mark-sent', methods=['POST'])
@api_key_required
def mark_quote_sent(quote_id):
    quote, status = _get_quote(quote_id)
    if quote is None:
        return jsonify({'error': 'Invalid quote ID' if status == 400 else 'Quote not found'}), status

    if quote.status not in ('draft', 'sent'):
        return jsonify({
            'error': f'Cannot mark a {quote.status} quote as sent',
            'status': quote.status,
        }), 409

    old_values = quote.to_dict()
    quote.status = 'sent'
    quote.sent_at = get_utc_now()

    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='quotes',
        record_id=quote.id,
        old_values=old_values,
        new_values=quote.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()

    return jsonify({'message': 'Quote marked as sent', 'quote': quote.to_dict()}), 200


@quotes_bp.route('/<quote_id>/accept', methods=['POST'])
@api_key_required
def accept_quote(quote_id):
    quote, status = _get_quote(quote_id)
    if quote is None:
        return jsonify({'error': 'Invalid quote ID' if status == 400 else 'Quote not found'}), status

    if quote.status not in ('sent', 'draft', 'accepted'):
        return jsonify({
            'error': f'Cannot accept a {quote.status} quote',
            'status': quote.status,
        }), 409

    old_values = quote.to_dict()
    quote.status = 'accepted'
    quote.accepted_at = get_utc_now()

    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='quotes',
        record_id=quote.id,
        old_values=old_values,
        new_values=quote.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()

    return jsonify({'message': 'Quote accepted', 'quote': quote.to_dict()}), 200


@quotes_bp.route('/<quote_id>/decline', methods=['POST'])
@api_key_required
def decline_quote(quote_id):
    quote, status = _get_quote(quote_id)
    if quote is None:
        return jsonify({'error': 'Invalid quote ID' if status == 400 else 'Quote not found'}), status

    if quote.status not in ('sent', 'draft', 'declined'):
        return jsonify({
            'error': f'Cannot decline a {quote.status} quote',
            'status': quote.status,
        }), 409

    old_values = quote.to_dict()
    quote.status = 'declined'
    quote.declined_at = get_utc_now()

    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='quotes',
        record_id=quote.id,
        old_values=old_values,
        new_values=quote.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()

    return jsonify({'message': 'Quote declined', 'quote': quote.to_dict()}), 200


@quotes_bp.route('/<quote_id>/convert-to-invoice', methods=['POST'])
@api_key_required
def convert_quote_to_invoice(quote_id):
    """Turn an accepted quote into a draft invoice.

    The invoice is created in `draft` status and is NOT sent -- conversion is
    a records step, not a money step. The caller marks it sent separately,
    exactly as for any other invoice.

    A quote can only be converted once: `converted_invoice_id` records the
    result, so a double call is a 409 rather than a duplicate invoice.
    """
    quote, status = _get_quote(quote_id)
    if quote is None:
        return jsonify({'error': 'Invalid quote ID' if status == 400 else 'Quote not found'}), status

    if quote.converted_invoice_id:
        return jsonify({
            'error': 'This quote has already been converted to an invoice',
            'invoice_id': quote.converted_invoice_id,
        }), 409

    if quote.status not in ('accepted', 'sent'):
        return jsonify({
            'error': f'Only an accepted or sent quote can be converted (this one is {quote.status})',
            'status': quote.status,
        }), 409

    user = request.current_user
    data = request.get_json(silent=True) or {}

    try:
        invoice_date = validate_date_string(data.get('invoice_date'), required=False) or date.today()
        due_date = validate_date_string(data.get('due_date'), required=False)
    except ValueError:
        return jsonify({'error': 'Invalid input'}), 400

    if due_date is None:
        terms = getattr(user, 'payment_terms', None) or 14
        due_date = invoice_date + timedelta(days=int(terms))

    invoice = Invoice(
        user_id=user.id,
        customer_id=quote.customer_id,
        client_name=quote.client_name,
        description=_lines_to_description(quote.lines or []),
        ex_gst_amount=quote.ex_gst_amount,
        gst_amount=quote.gst_amount,
        gst_type=quote.gst_type,
        total_amount=quote.total_amount,
        invoice_date=invoice_date,
        due_date=due_date,
        status='draft',
        notes=f'Quote {quote.quote_number}',
    )
    db.session.add(invoice)
    db.session.flush()

    quote.converted_invoice_id = invoice.id
    if quote.status != 'accepted':
        quote.status = 'accepted'
        quote.accepted_at = quote.accepted_at or get_utc_now()

    log_activity(
        user_id=user.id,
        action='CREATE',
        table_name='invoices',
        record_id=invoice.id,
        new_values=invoice.to_dict(),
        ip_address=request.remote_addr,
    )
    log_activity(
        user_id=user.id,
        action='UPDATE',
        table_name='quotes',
        record_id=quote.id,
        old_values={'converted_invoice_id': None, 'status': 'sent'},
        new_values=quote.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()

    return jsonify({
        'message': 'Quote converted to invoice',
        'quote': quote.to_dict(),
        'invoice': invoice.to_dict(),
    }), 201
