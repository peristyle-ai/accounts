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

GST is DERIVED from the customer and the business unless the caller says
otherwise (see `app/shared/tax.py`):

  * omitted  -> derive: GST applies only when the business is registered,
                the customer is a GST registrant, and the customer is in
                Australia. A customer in the USA therefore gets a quote
                with NO GST, without anyone having to remember to ask.
  * explicit -> honoured, but a NON-zero rate is refused unless the
                customer is actually registered for GST. Charging Australian
                GST to an overseas or unregistered customer is not a
                rounding error, it is a wrong document.

A quote line may be RECURRING (a monthly retainer). The totals then cover
the first period, and `one_off_amount` / `recurring_amount` / the PDF show
the split so nobody has to guess which is which.

Tenant scoping: every query is filtered by `user_id`, so a quote belonging to
another tenant is a 404, not a 403 (matching the rest of the API).

The lifecycle transitions are separate endpoints rather than a free-text
status update, so an illegal transition (accepted -> draft, say) is
impossible to express.
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from flask import Blueprint, request, jsonify, send_file

logger = logging.getLogger(__name__)

from app.models import db, get_utc_now
from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.quote import Quote, QUOTE_SENT_VIAS, QUOTE_STATUSES
from app.shared.decorators import api_key_required, log_activity
from app.shared.roles import role_required
from app.shared.tax import check_gst_type, derive_gst_type
from app.shared.tax import gst_basis as gst_basis_for
from app.shared.validators import validate_date_string, validate_uuid

quotes_bp = Blueprint('quotes', __name__, url_prefix='/api/quotes')

# Default validity of a quote, in days, when the caller does not say.
DEFAULT_VALID_DAYS = 30

# The hash of a transmitted document: 64 hex characters. A sha256 digest
# is written by `sha256sum`, so this is the shape a person pasting the
# output of that command actually produces.
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')


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
        if data.get('gst_type') is None:
            # Not stated: derive it from the customer and the business.
            gst_type = derive_gst_type(customer, user)
        else:
            gst_type = validate_gst_type(data.get('gst_type'))
        quote_date = validate_date_string(data.get('quote_date'), required=True)
        valid_until = validate_date_string(data.get('valid_until'))
    except ValueError:
        return jsonify({'error': 'Invalid input'}), 400

    gst_error = check_gst_type(customer, user, gst_type)
    if gst_error:
        return jsonify({'error': gst_error}), 400

    # Capture WHY this quote carries the GST treatment it carries, so the
    # reason on the page is fixed at the moment the quote is raised.
    gst_basis = gst_basis_for(customer, user, gst_type)

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
        gst_basis=gst_basis,
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

    # VALIDATE FIRST, THEN ASSIGN.
    #
    # Every value the caller sent is checked into a local variable before
    # anything is written to the ORM object. The earlier version assigned
    # as it went, which meant a request that was REJECTED (a bad
    # gst_type, a bad quote_date) had already mutated the row. The
    # mutation is flushed by the next query in the session, so the
    # rejected request held a row lock on the quote until the session was
    # torn down -- and any concurrent write to that quote blocked behind
    # it. A 400 must not leave a lock behind.
    new_lines = quote.lines
    if 'lines' in data:
        lines, error = _parse_lines(data)
        if error:
            return jsonify({'error': error}), 400
        new_lines = lines

    try:
        from app.shared.validators import validate_gst_type
        new_gst_type = (validate_gst_type(data.get('gst_type'))
                        if 'gst_type' in data else quote.gst_type)
        new_quote_date = (validate_date_string(data.get('quote_date'), required=True)
                          if 'quote_date' in data else quote.quote_date)
        new_valid_until = (validate_date_string(data.get('valid_until'))
                           if 'valid_until' in data else quote.valid_until)
    except ValueError:
        return jsonify({'error': 'Invalid input'}), 400

    new_client_name = quote.client_name
    if 'client_name' in data:
        new_client_name = str(data.get('client_name') or '').strip()
        if not new_client_name:
            return jsonify({'error': 'Client name is required'}), 400

    if new_valid_until and new_quote_date and new_valid_until < new_quote_date:
        return jsonify({'error': 'valid_until cannot be before quote_date'}), 400

    # The GST check needs the customer and the caller, not the new
    # values, so it can run before anything is assigned.
    gst_error = check_gst_type(quote.customer, request.current_user, new_gst_type)
    if gst_error:
        return jsonify({'error': gst_error}), 400

    # Everything checked. Now write.
    quote.lines = new_lines
    quote.gst_type = new_gst_type
    quote.gst_basis = gst_basis_for(quote.customer, request.current_user, new_gst_type)
    quote.quote_date = new_quote_date
    quote.valid_until = new_valid_until
    quote.client_name = new_client_name
    if 'notes' in data:
        quote.notes = data.get('notes')

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
    """Mark a quote as sent, recording HOW, WHERE and WHICH BYTES.

    The optional body carries the transmission record:

        sent_at               ISO timestamp of the TRANSMISSION (not of
                              this update). A quote a person sent from
                              their own mail client an hour ago was
                              transmitted an hour ago; defaulting to now()
                              would date the transmission by when someone
                              remembered to record it.
        sent_via              one of Quote.SENT_VIAS. The distinction that
                              matters is `email_system` (this application
                              sent it, so the mail server holds the
                              delivery record) versus `email_manual` (a
                              person sent it from their own client, and
                              there is nothing to check but this row).
        sent_to               the address it went to.
        sent_document_sha256  sha256 of the exact PDF bytes transmitted.

    All four are optional and independent: a caller that only flips the
    status gets exactly the old behaviour, and a caller that knows the
    details records them. Nothing is inferred -- an unrecorded
    transmission stays unrecorded rather than being guessed at.
    """
    quote, status = _get_quote(quote_id)
    if quote is None:
        return jsonify({'error': 'Invalid quote ID' if status == 400 else 'Quote not found'}), status

    if quote.status not in ('draft', 'sent'):
        return jsonify({
            'error': f'Cannot mark a {quote.status} quote as sent',
            'status': quote.status,
        }), 409

    data = request.get_json(silent=True) or {}

    # VALIDATE FIRST, THEN ASSIGN -- a rejected request must not have
    # already mutated the row (see the update route for the same rule).
    new_sent_at = quote.sent_at
    if data.get('sent_at'):
        try:
            new_sent_at = datetime.fromisoformat(
                str(data['sent_at']).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid sent_at timestamp'}), 400
        if new_sent_at.tzinfo is None:
            new_sent_at = new_sent_at.replace(tzinfo=timezone.utc)

    new_sent_via = quote.sent_via
    if 'sent_via' in data:
        via = (data.get('sent_via') or '').strip().lower() or None
        if via is not None and via not in QUOTE_SENT_VIAS:
            return jsonify({
                'error': f'Invalid sent_via. Must be one of: {", ".join(QUOTE_SENT_VIAS)}',
            }), 400
        new_sent_via = via

    new_sent_to = quote.sent_to
    if 'sent_to' in data:
        to = (data.get('sent_to') or '').strip() or None
        if to is not None and len(to) > 500:
            return jsonify({'error': 'sent_to is too long'}), 400
        new_sent_to = to

    new_sha = quote.sent_document_sha256
    if 'sent_document_sha256' in data:
        sha = (data.get('sent_document_sha256') or '').strip().lower() or None
        if sha is not None and not _SHA256_RE.match(sha):
            return jsonify({
                'error': 'sent_document_sha256 must be 64 hex characters',
            }), 400
        new_sha = sha

    old_values = quote.to_dict()
    quote.status = 'sent'
    quote.sent_at = new_sent_at or get_utc_now()
    quote.sent_via = new_sent_via
    quote.sent_to = new_sent_to
    quote.sent_document_sha256 = new_sha

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
