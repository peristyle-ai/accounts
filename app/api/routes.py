import logging
from decimal import Decimal
from flask import Blueprint, request, jsonify, current_app, send_file
from werkzeug.utils import secure_filename
import os
import io
<<<<<<< HEAD

logger = logging.getLogger(__name__)
=======
from decimal import Decimal
from datetime import date, datetime, timezone
>>>>>>> 08a5f02 (feat(hmi): review page + sender/sent columns on expenses)

from app.models import db, get_utc_now
from app.models.user import User
from app.models.expense import Expense
from app.models.invoice import Invoice
from app.models.account_category import AccountCategory
from app.models.activity_log import ActivityLog
from app.models.customer import Customer
from app.shared.decorators import api_key_required, log_activity
from app.shared.auth import verify_credentials
from app.shared.rate_limiter import rate_limit_api, record_attempt, _get_client_ip
from app.shared.validators import validate_decimal, validate_date_string, validate_gst_type, validate_uuid
from app.shared.address import validate_state, validate_postcode

api_bp = Blueprint('api', __name__, url_prefix='/api')


# ---------------------------------------------------------------------------
# Sender-tracking helpers (2026-10-26).
# Jessica asked for "who was the sender and when it was sent" on the
# review page. The sender arrives with the receipt over WhatsApp/Telegram;
# before this, only the ingest audit log knew who sent what.
#
#   submitted_by    display name (roster-resolved at ingest time)
#   submitted_phone raw sender phone
#   submitted_at    ISO-8601 timestamp the receipt was sent/received
# ---------------------------------------------------------------------------

def _clean_sender_field(value, max_len, field_name):
    """Trim a sender field to a bounded string or None. Returns (value, error)."""
    if value is None:
        return None, None
    value = str(value).strip()
    if not value:
        return None, None
    if len(value) > max_len:
        return None, f'{field_name} must be {max_len} characters or fewer'
    return value, None


def _parse_submitted_at(value):
    """Parse an ISO-8601 timestamp into a timezone-aware datetime, or None."""
    if value in (None, ''):
        return None
    if isinstance(value, (date, datetime)):
        dt = value
    else:
        raw = str(value).strip()
        if raw.endswith('Z'):
            raw = raw[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Address payload helpers (added 2026-09-17 alongside the structured-address
# refactor; see app/shared/address.py and the Xero export for context).
# ---------------------------------------------------------------------------

_ADDRESS_FIELDS = ('address_line1', 'address_line2', 'city', 'state', 'postcode', 'country')


def _any_address_field(data):
    return any(field in data for field in _ADDRESS_FIELDS)


def _validate_address_payload(data, country_default='Australia'):
    """Return an error string if the address fields in `data` are invalid,
    otherwise None.

    The `country` key (if present) is used for state/postcode validation.
    Falls back to country_default if not provided — for Australia that means
    state must be one of NSW/VIC/QLD/SA/WA/TAS/ACT/NT and postcode must be
    4 digits.
    """
    country = data.get('country') or country_default
    if 'state' in data:
        ok, _result = validate_state(data['state'], country=country)
        if not ok:
            return _result
    if 'postcode' in data:
        ok, _result = validate_postcode(data['postcode'], country=country)
        if not ok:
            return _result
    return None


def _validate_and_normalize_address(data, country_default='Australia', country_field='country'):
    """Like _validate_address_payload, but also returns the normalised form
    of every address field present in `data` (uppercase state, stripped
    postcode, etc.). Caller should use the normalised form when persisting.

    Returns (error_string_or_None, normalised_dict).

    `country_field` lets callers with a differently-named country column
    (e.g. user.address_country) pass the right key name for normalisation
    purposes.
    """
    country = data.get(country_field) or country_default
    normalised = {}

    if 'state' in data:
        ok, result = validate_state(data['state'], country=country)
        if not ok:
            return result, {}
        normalised['state'] = result

    if 'postcode' in data:
        ok, result = validate_postcode(data['postcode'], country=country)
        if not ok:
            return result, {}
        normalised['postcode'] = result

    return None, normalised


@api_bp.route('/auth/login', methods=['POST'])
@rate_limit_api
def login():
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    email = data.get('email', '').strip().lower()
    password = data.get('password', '')

    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400

    client_ip = _get_client_ip()
    user = User.query.filter_by(email=email).first()

    # verify_credentials ALWAYS performs a hash comparison, even for an
    # unknown email, so the response time does not reveal whether the
    # account exists. (2026-10-25 security sweep S9: the previous
    # expression short-circuited and skipped the hash comparison.)
    if not verify_credentials(user, password) or not user.is_active:
        record_attempt(client_ip, success=False, account=email)
        return jsonify({'error': 'Invalid credentials'}), 401

    record_attempt(client_ip, success=True, account=email)
    return jsonify({'message': 'Login successful', 'user': user.to_dict()}), 200


@api_bp.route('/auth/api-key', methods=['POST'])
@api_key_required
def generate_api_key():
    user = request.current_user
    api_key = user.generate_api_key()
    db.session.commit()

    return jsonify({'message': 'API key generated', 'api_key': api_key}), 200


@api_bp.route('/account-categories', methods=['GET'])
@api_key_required
def list_account_categories():
    categories = AccountCategory.query.order_by(AccountCategory.name).all()
    return jsonify({'account_categories': [c.to_dict() for c in categories]}), 200


@api_bp.route('/account-categories', methods=['POST'])
@api_key_required
def create_account_category():
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400

    category = AccountCategory(name=name, description=data.get('description', ''))
    db.session.add(category)
    db.session.commit()

    log_activity(
        user_id=request.current_user.id,
        action='CREATE',
        table_name='account_categories',
        record_id=category.id,
        new_values=category.to_dict(),
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'Account category created', 'account_category': category.to_dict()}), 201


@api_bp.route('/account-categories/<category_id>', methods=['PUT'])
@api_key_required
def update_account_category(category_id):
    if not validate_uuid(category_id):
        return jsonify({'error': 'Invalid category ID'}), 400

    category = AccountCategory.query.get(category_id)
    if not category:
        return jsonify({'error': 'Account category not found'}), 404

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    old_values = category.to_dict()

    if 'name' in data:
        category.name = data['name'].strip()
    if 'description' in data:
        category.description = data['description']

    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='account_categories',
        record_id=category.id,
        old_values=old_values,
        new_values=category.to_dict(),
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'Account category updated', 'account_category': category.to_dict()}), 200


@api_bp.route('/account-categories/<category_id>', methods=['DELETE'])
@api_key_required
def delete_account_category(category_id):
    if not validate_uuid(category_id):
        return jsonify({'error': 'Invalid category ID'}), 400

    category = AccountCategory.query.get(category_id)
    if not category:
        return jsonify({'error': 'Account category not found'}), 404

    old_values = category.to_dict()

    log_activity(
        user_id=request.current_user.id,
        action='DELETE',
        table_name='account_categories',
        record_id=category.id,
        old_values=old_values,
        ip_address=request.remote_addr
    )

    db.session.delete(category)
    db.session.commit()

    return jsonify({'message': 'Account category deleted'}), 200


@api_bp.route('/customers', methods=['GET'])
@api_key_required
def list_customers():
    customers = Customer.query.filter_by(user_id=request.current_user.id).order_by(Customer.name).all()
    return jsonify({'customers': [c.to_dict() for c in customers]}), 200


def _validate_cc_emails(data, field='invoice_cc_emails'):
    """Validate the per-customer invoice Cc mailing list.

    Returns (error_message_or_none, normalised_value_or_None).

    Rules:
      - Must be a JSON array of strings.
      - Empty list `[]` is allowed (means "no Cc").
      - Each entry must look like an email address (basic regex; we don't
        want to be RFC-perfect, just reject obvious typos so customers
        don't silently bounce).
      - Server lowercases + strips whitespace + dedupes (preserves first
        occurrence order).
      - Max 50 entries (sanity cap; no real customer has anywhere near
        that many invoice readers).

    Added 2026-09-28 alongside the customers.invoice_cc_emails JSONB column.
    """
    import re
    if field not in data:
        return None, None
    value = data[field]
    if value is None:
        return None, []  # null means "clear the list"
    if not isinstance(value, list):
        return f"`{field}` must be a JSON array of email strings.", None
    if len(value) > 50:
        return f"`{field}` accepts at most 50 entries (got {len(value)}).", None
    email_re = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    seen = set()
    normalised = []
    for i, entry in enumerate(value):
        if not isinstance(entry, str):
            return f"`{field}[{i}]` must be a string, got {type(entry).__name__}.", None
        cleaned = entry.strip().lower()
        if not cleaned:
            continue  # skip empty entries silently
        if len(cleaned) > 255:
            return f"`{field}[{i}]` is longer than 255 characters.", None
        if not email_re.match(cleaned):
            return f"`{field}[{i}]` ('{entry}') does not look like a valid email address.", None
        if cleaned in seen:
            continue
        seen.add(cleaned)
        normalised.append(cleaned)
    return None, normalised


@api_bp.route('/customers/<customer_id>', methods=['GET'])
@api_key_required
def get_customer(customer_id):
    if not validate_uuid(customer_id):
        return jsonify({'error': 'Invalid customer ID'}), 400

    customer = Customer.query.filter_by(id=customer_id, user_id=request.current_user.id).first()
    if not customer:
        return jsonify({'error': 'Customer not found'}), 404

    return jsonify({'customer': customer.to_dict()}), 200


# -----------------------------------------------------------------------------
# Statement of Account — added 2026-09-01.
#
#   GET /api/customers/<customer_id>/statement.json
#   GET /api/customers/<customer_id>/statement.pdf[?as_of=YYYY-MM-DD]
#
# Returns every invoice (paid + outstanding) for the customer that belongs to
# the requesting user, plus totals + 30/60/90/90+ aging of the outstanding
# balance. Both invoices and customers are scoped by user_id (F-10).
# -----------------------------------------------------------------------------
def _build_statement_payload(user_id, customer_id, as_of_date):
    """Shared helper for both the JSON and PDF endpoints."""
    from datetime import date as _date, datetime as _datetime

    customer = Customer.query.filter_by(id=customer_id, user_id=user_id).first()
    if not customer:
        return None, None, None

    # parse / default as_of
    if as_of_date is None:
        as_of_date = _date.today()
    if isinstance(as_of_date, str):
        try:
            as_of_date = _datetime.strptime(as_of_date, '%Y-%m-%d').date()
        except ValueError:
            return 'invalid_as_of', None, None

    invoices = (
        Invoice.query
        .filter_by(customer_id=customer_id, user_id=user_id)
        .order_by(Invoice.invoice_date.asc(), Invoice.created_at.asc())
        .all()
    )
    return customer, invoices, as_of_date


@api_bp.route('/customers/<customer_id>/statement.json', methods=['GET'])
@api_key_required
def customer_statement_json(customer_id):
    if not validate_uuid(customer_id):
        return jsonify({'error': 'Invalid customer ID'}), 400

    customer, invoices, as_of_date = _build_statement_payload(
        request.current_user.id, customer_id,
        request.args.get('as_of'),
    )
    if customer is None:
        return jsonify({'error': 'Customer not found'}), 404
    if customer == 'invalid_as_of':
        return jsonify({'error': 'as_of must be YYYY-MM-DD'}), 400

    from decimal import Decimal
    def D(x):
        if x is None or x == '':
            return Decimal('0.00')
        return Decimal(str(x))

    total_invoiced = Decimal('0.00')
    total_paid = Decimal('0.00')
    outstanding = Decimal('0.00')
    aging = {'current': '0.00', '1_30': '0.00', '31_60': '0.00',
             '61_90': '0.00', '90_plus': '0.00'}

    line_items = []
    for inv in invoices:
        total = D(inv.total_amount)
        paid = D(inv.amount_paid) if inv.status == 'paid' else Decimal('0.00')
        bal = total - paid
        total_invoiced += total
        total_paid += paid
        if inv.status != 'paid':
            outstanding += bal
            if inv.due_date and as_of_date > inv.due_date:
                days = (as_of_date - inv.due_date).days
                if days <= 30:
                    aging['1_30'] = str(Decimal(aging['1_30']) + bal)
                elif days <= 60:
                    aging['31_60'] = str(Decimal(aging['31_60']) + bal)
                elif days <= 90:
                    aging['61_90'] = str(Decimal(aging['61_90']) + bal)
                else:
                    aging['90_plus'] = str(Decimal(aging['90_plus']) + bal)
            else:
                aging['current'] = str(Decimal(aging['current']) + bal)

        status_disp = inv.status
        if status_disp == 'sent' and inv.due_date and as_of_date > inv.due_date:
            status_disp = 'overdue'

        line_items.append({
            'id': inv.id,
            'invoice_date': inv.invoice_date.isoformat(),
            'invoice_number': inv.id[:8].upper(),
            'description': inv.description,
            'due_date': inv.due_date.isoformat() if inv.due_date else None,
            'amount': str(total),
            'amount_paid': str(paid) if inv.status == 'paid' else None,
            'balance': str(bal) if bal > 0 else '0.00',
            'status': status_disp,
            'status_raw': inv.status,
            'reminder_count': inv.reminders.count() if hasattr(inv, 'reminders') else 0,
        })

    return jsonify({
        'customer': customer.to_dict(),
        'as_of': as_of_date.isoformat(),
        'summary': {
            'total_invoiced': str(total_invoiced),
            'total_paid':     str(total_paid),
            'outstanding':    str(outstanding),
            'invoice_count':  len(invoices),
            'paid_count':     sum(1 for i in invoices if i.status == 'paid'),
            'outstanding_count': sum(1 for i in invoices if i.status != 'paid'),
        },
        'aging': aging,
        'invoices': line_items,
    }), 200


@api_bp.route('/customers/<customer_id>/statement.pdf', methods=['GET'])
@api_key_required
def customer_statement_pdf(customer_id):
    if not validate_uuid(customer_id):
        return jsonify({'error': 'Invalid customer ID'}), 400

    customer, invoices, as_of_date = _build_statement_payload(
        request.current_user.id, customer_id,
        request.args.get('as_of'),
    )
    if customer is None:
        return jsonify({'error': 'Customer not found'}), 404
    if customer == 'invalid_as_of':
        return jsonify({'error': 'as_of must be YYYY-MM-DD'}), 400

    from app.shared.pdf import generate_statement_of_account_pdf as build_pdf
    buffer = build_pdf(request.current_user, customer, invoices, as_of_date)

    as_of_str = as_of_date.isoformat() if hasattr(as_of_date, 'isoformat') else str(as_of_date)
    safe_name = ''.join(c for c in customer.name if c.isalnum() or c in (' ', '_')).strip().replace(' ', '_')
    return send_file(
        buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'Statement_{safe_name}_{as_of_str}.pdf',
    )


@api_bp.route('/customers', methods=['POST'])
@api_key_required
def create_customer():
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400

    # Reject the legacy free-text `address` field outright. 2026-09-17
    # refactor: the legacy column has been dropped, so callers MUST send
    # structured fields. Helps avoid silent drift between denormalised blob
    # and structured columns.
    if 'address' in data:
        return jsonify({
            'error': (
                'The legacy `address` field is no longer supported. '
                'Send structured fields instead: address_line1, city, state, postcode, country.'
            )
        }), 400

    addr_error, normalised_addr = _validate_and_normalize_address(data, country_default='Australia')
    if addr_error:
        return jsonify({'error': addr_error}), 400

    cc_error, normalised_cc = _validate_cc_emails(data)
    if cc_error:
        return jsonify({'error': cc_error}), 400

    customer = Customer(
        user_id=request.current_user.id,
        name=name,
        contact_name=data.get('contact_name'),
        address_line1=data.get('address_line1'),
        address_line2=data.get('address_line2'),
        city=data.get('city'),
        state=normalised_addr.get('state', data.get('state')),
        postcode=normalised_addr.get('postcode', data.get('postcode')),
        country=data.get('country', 'Australia'),
        contact_email=data.get('contact_email'),
        abn=data.get('abn'),
        contact_number=data.get('contact_number'),
        gst=data.get('gst', True),
        invoice_cc_emails=normalised_cc or [],
    )

    db.session.add(customer)
    db.session.commit()

    log_activity(
        user_id=request.current_user.id,
        action='CREATE',
        table_name='customers',
        record_id=customer.id,
        new_values=customer.to_dict(),
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'Customer created', 'customer': customer.to_dict()}), 201


@api_bp.route('/customers/<customer_id>', methods=['PUT'])
@api_key_required
def update_customer(customer_id):
    if not validate_uuid(customer_id):
        return jsonify({'error': 'Invalid customer ID'}), 400

    customer = Customer.query.filter_by(id=customer_id, user_id=request.current_user.id).first()
    if not customer:
        return jsonify({'error': 'Customer not found'}), 404

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    old_values = customer.to_dict()

    if 'name' in data:
        new_name = data['name'].strip()
        if not new_name:
            return jsonify({'error': 'Name cannot be empty'}), 400
        customer.name = new_name
    if 'contact_name' in data:
        customer.contact_name = data['contact_name']

    # Address — structured only (the legacy `address` field was removed
    # in the 2026-09-17 refactor).
    if 'address' in data:
        return jsonify({
            'error': (
                'The legacy `address` field is no longer supported. '
                'Send structured fields instead: address_line1, city, state, postcode, country.'
            )
        }), 400

    addr_error, normalised_addr = _validate_and_normalize_address(data, country_default='Australia')
    if addr_error:
        return jsonify({'error': addr_error}), 400
    for field in ('address_line1', 'address_line2', 'city', 'state', 'postcode', 'country'):
        if field in data:
            setattr(customer, field, normalised_addr.get(field, data[field]))

    if 'contact_email' in data:
        customer.contact_email = data['contact_email']
    if 'abn' in data:
        customer.abn = data['abn']
    if 'contact_number' in data:
        customer.contact_number = data['contact_number']
    if 'gst' in data:
        customer.gst = data['gst']

    if 'invoice_cc_emails' in data:
        cc_error, normalised_cc = _validate_cc_emails(data)
        if cc_error:
            return jsonify({'error': cc_error}), 400
        customer.invoice_cc_emails = normalised_cc or []

    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='customers',
        record_id=customer.id,
        old_values=old_values,
        new_values=customer.to_dict(),
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'Customer updated', 'customer': customer.to_dict()}), 200


@api_bp.route('/customers/<customer_id>', methods=['DELETE'])
@api_key_required
def delete_customer(customer_id):
    if not validate_uuid(customer_id):
        return jsonify({'error': 'Invalid customer ID'}), 400

    customer = Customer.query.filter_by(id=customer_id, user_id=request.current_user.id).first()
    if not customer:
        return jsonify({'error': 'Customer not found'}), 404

    if customer.invoices.first():
        return jsonify({'error': 'Cannot delete customer with existing invoices'}), 400

    old_values = customer.to_dict()

    log_activity(
        user_id=request.current_user.id,
        action='DELETE',
        table_name='customers',
        record_id=customer.id,
        old_values=old_values,
        ip_address=request.remote_addr
    )

    db.session.delete(customer)
    db.session.commit()

    return jsonify({'message': 'Customer deleted'}), 200


@api_bp.route('/expenses', methods=['GET'])
@api_key_required
def list_expenses():
    user = request.current_user

    query = Expense.query.filter_by(user_id=user.id)

    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    if start_date:
        try:
            start = validate_date_string(start_date)
            query = query.filter(Expense.expense_date >= start)
        except ValueError as e:
            logger.warning('Invalid start_date in list_expenses: %s', e)
            return jsonify({'error': 'Invalid date format'}), 400

    if end_date:
        try:
            end = validate_date_string(end_date)
            query = query.filter(Expense.expense_date <= end)
        except ValueError as e:
            logger.warning('Invalid end_date in list_expenses: %s', e)
            return jsonify({'error': 'Invalid date format'}), 400

    expenses = query.order_by(Expense.expense_date.desc()).all()
    return jsonify({'expenses': [e.to_dict() for e in expenses]}), 200


@api_bp.route('/expenses', methods=['POST'])
@api_key_required
def create_expense():
    user = request.current_user
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    try:
        vendor_name = data.get('vendor_name', '').strip()
        if not vendor_name:
            return jsonify({'error': 'Vendor name is required'}), 400

        amount = data.get('amount') or data.get('ex_gst_amount')
        if amount is None:
            return jsonify({'error': 'Amount is required'}), 400

        input_amount = validate_decimal(amount, required=True, min_value=0)
        amount_type = data.get('amount_type', 'excludes')
        gst_type = validate_gst_type(data.get('gst_type', 0))
        currency = data.get('currency', 'AUD')
        expense_date = validate_date_string(data.get('expense_date'), required=True)
        account_category_id = data.get('account_category_id')

<<<<<<< HEAD
=======
        # 2026-09-24: project_code from enp_crm /api/leads/search (LD-YYYY-NNNNNN).
        # Optional — NULL for overhead/admin expenses.
        project_code = data.get('project_code')
        if project_code is not None:
            project_code = str(project_code).strip() or None
            if project_code and len(project_code) > 50:
                return jsonify({'error': 'project_code must be 50 characters or fewer'}), 400

        # 2026-09-24: project_name — human-readable label, denormalized from cmo's
        # lead.name at ingest time. Optional; defaults to None. Length-bounded.
        project_name = data.get('project_name')
        if project_name is not None:
            project_name = str(project_name).strip() or None
            if project_name and len(project_name) > 255:
                return jsonify({'error': 'project_name must be 255 characters or fewer'}), 400

        # 2026-09-25: attachment_sha256 (receipt dedup). Optional. Must be
        # 64-char lowercase hex if provided. Set by ingest_receipt after
        # hashing the attachment bytes.
        attachment_sha256 = data.get('attachment_sha256')
        if attachment_sha256 is not None:
            attachment_sha256 = str(attachment_sha256).strip().lower() or None
            if attachment_sha256:
                if (len(attachment_sha256) != 64
                        or not all(c in '0123456789abcdef' for c in attachment_sha256)):
                    return jsonify({'error': 'attachment_sha256 must be 64 lowercase hex characters'}), 400

        # 2026-10-26: sender tracking (Jessica's review-page request).
        # submitted_by = display name, submitted_phone = raw sender,
        # submitted_at = when the receipt was sent/received.
        submitted_by, err = _clean_sender_field(data.get('submitted_by'), 120, 'submitted_by')
        if err:
            return jsonify({'error': err}), 400
        submitted_phone, err = _clean_sender_field(data.get('submitted_phone'), 32, 'submitted_phone')
        if err:
            return jsonify({'error': err}), 400
        submitted_at = _parse_submitted_at(data.get('submitted_at'))

>>>>>>> 08a5f02 (feat(hmi): review page + sender/sent columns on expenses)
        if account_category_id and not validate_uuid(account_category_id):
            return jsonify({'error': 'Invalid account category ID'}), 400

        original_currency_amount = None
        exchange_rate = None

        if currency == 'USD':
            from app.shared.currency import get_usd_to_aud_rate, convert_usd_to_aud
            exchange_rate = get_usd_to_aud_rate(data.get('expense_date'))
            if exchange_rate:
                original_currency_amount = input_amount
                input_amount = convert_usd_to_aud(input_amount, exchange_rate)

        if amount_type == 'includes':
            ex_gst_amount = (input_amount / (Decimal('1') + gst_type)).quantize(Decimal('0.01'))
        else:
            ex_gst_amount = input_amount

        gst_amount = Expense.calculate_gst(ex_gst_amount, gst_type)
        total_amount = Expense.calculate_total(ex_gst_amount, gst_amount)

    except ValueError as e:
        logger.warning('Validation error in create_expense: %s', e)
        return jsonify({'error': 'Invalid input'}), 400

    expense = Expense(
        user_id=user.id,
        source='api',
        vendor_name=vendor_name,
        description=data.get('description', ''),
        currency=currency,
        original_currency_amount=original_currency_amount,
        exchange_rate=exchange_rate,
        ex_gst_amount=ex_gst_amount,
        gst_amount=gst_amount,
        gst_type=gst_type,
        total_amount=total_amount,
        expense_date=expense_date,
<<<<<<< HEAD
        account_category_id=account_category_id
=======
        account_category_id=account_category_id,
        project_code=project_code,
        project_name=project_name,
        attachment_sha256=attachment_sha256,
        submitted_by=submitted_by,
        submitted_phone=submitted_phone,
        submitted_at=submitted_at,
        requires_review=bool(data.get('requires_review', False))
>>>>>>> 08a5f02 (feat(hmi): review page + sender/sent columns on expenses)
    )

    db.session.add(expense)
    db.session.flush()

    log_activity(
        user_id=user.id,
        action='CREATE',
        table_name='expenses',
        record_id=expense.id,
        new_values=expense.to_dict(),
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'Expense created', 'expense': expense.to_dict()}), 201


@api_bp.route('/expenses/<expense_id>', methods=['GET'])
@api_key_required
def get_expense(expense_id):
    if not validate_uuid(expense_id):
        return jsonify({'error': 'Invalid expense ID'}), 400

    expense = Expense.query.filter_by(id=expense_id, user_id=request.current_user.id).first()
    if not expense:
        return jsonify({'error': 'Expense not found'}), 404

    return jsonify({'expense': expense.to_dict()}), 200


@api_bp.route('/expenses/<expense_id>', methods=['PUT'])
@api_key_required
def update_expense(expense_id):
    if not validate_uuid(expense_id):
        return jsonify({'error': 'Invalid expense ID'}), 400

    expense = Expense.query.filter_by(id=expense_id, user_id=request.current_user.id).first()
    if not expense:
        return jsonify({'error': 'Expense not found'}), 404

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    old_values = expense.to_dict()

    try:
        # 2026-08-04 OCR refactor: editing an expense (e.g. by an agent doing
        # manual review) clears the requires_review flag as soon as the user
        # supplies a real vendor name. This is safe because the only way
        # requires_review is set is by the OCR pipeline, and the only way to
        # clear it is to provide the missing data.
        editing_real_data = False

        if 'vendor_name' in data:
            vendor_name = data['vendor_name'].strip()
            if not vendor_name:
                return jsonify({'error': 'Vendor name cannot be empty'}), 400
            expense.vendor_name = vendor_name
            if vendor_name and vendor_name != 'REQUIRES REVIEW':
                editing_real_data = True

        if 'description' in data:
            expense.description = data['description']

        # 2026-10-26: sender tracking — set/clear explicitly (None clears).
        if 'submitted_by' in data:
            val, err = _clean_sender_field(data.get('submitted_by'), 120, 'submitted_by')
            if err:
                return jsonify({'error': err}), 400
            expense.submitted_by = val
        if 'submitted_phone' in data:
            val, err = _clean_sender_field(data.get('submitted_phone'), 32, 'submitted_phone')
            if err:
                return jsonify({'error': err}), 400
            expense.submitted_phone = val
        if 'submitted_at' in data:
            expense.submitted_at = _parse_submitted_at(data.get('submitted_at'))

        amount_type = data.get('amount_type', 'excludes')

        if 'amount' in data or 'ex_gst_amount' in data:
            input_amount = validate_decimal(data.get('amount') or data.get('ex_gst_amount'), required=True, min_value=0)
            currency = data.get('currency', expense.currency or 'AUD')

            original_currency_amount = None
            exchange_rate = None

            if currency == 'USD':
                from app.shared.currency import get_usd_to_aud_rate, convert_usd_to_aud
                expense_date_str = data.get('expense_date') or expense.expense_date.isoformat()
                exchange_rate = get_usd_to_aud_rate(expense_date_str)
                if exchange_rate:
                    original_currency_amount = input_amount
                    input_amount = convert_usd_to_aud(input_amount, exchange_rate)

            if amount_type == 'includes':
                expense.ex_gst_amount = (input_amount / (Decimal('1') + expense.gst_type)).quantize(Decimal('0.01'))
            else:
                expense.ex_gst_amount = input_amount

            expense.currency = currency
            expense.original_currency_amount = original_currency_amount
            expense.exchange_rate = exchange_rate

        if 'gst_type' in data:
            expense.gst_type = validate_gst_type(data['gst_type'])

        if 'expense_date' in data:
            expense.expense_date = validate_date_string(data['expense_date'], required=True)

        if 'account_category_id' in data:
            if data['account_category_id'] and not validate_uuid(data['account_category_id']):
                return jsonify({'error': 'Invalid account category ID'}), 400
            expense.account_category_id = data['account_category_id']

        if 'currency' in data:
            expense.currency = data['currency']

        if 'ex_gst_amount' in data or 'gst_amount' in data or 'amount' in data:
            editing_real_data = True

        expense.gst_amount = Expense.calculate_gst(expense.ex_gst_amount, expense.gst_type)
        expense.total_amount = Expense.calculate_total(expense.ex_gst_amount, expense.gst_amount)

        if editing_real_data and expense.requires_review:
            expense.requires_review = False

    except ValueError as e:
        logger.warning('Validation error in update_expense: %s', e)
        return jsonify({'error': 'Invalid input'}), 400

    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='expenses',
        record_id=expense.id,
        old_values=old_values,
        new_values=expense.to_dict(),
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'Expense updated', 'expense': expense.to_dict()}), 200


@api_bp.route('/expenses/<expense_id>', methods=['DELETE'])
@api_key_required
def delete_expense(expense_id):
    if not validate_uuid(expense_id):
        return jsonify({'error': 'Invalid expense ID'}), 400

    expense = Expense.query.filter_by(id=expense_id, user_id=request.current_user.id).first()
    if not expense:
        return jsonify({'error': 'Expense not found'}), 404

    old_values = expense.to_dict()

    log_activity(
        user_id=request.current_user.id,
        action='DELETE',
        table_name='expenses',
        record_id=expense.id,
        old_values=old_values,
        ip_address=request.remote_addr
    )

    db.session.delete(expense)
    db.session.commit()

    return jsonify({'message': 'Expense deleted'}), 200


@api_bp.route('/expenses/<expense_id>/upload', methods=['POST'])
@api_key_required
def upload_expense_attachment(expense_id):
    if not validate_uuid(expense_id):
        return jsonify({'error': 'Invalid expense ID'}), 400

    expense = Expense.query.filter_by(id=expense_id, user_id=request.current_user.id).first()
    if not expense:
        return jsonify({'error': 'Expense not found'}), 404

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    filename = secure_filename(file.filename)
    allowed_extensions = {'pdf', 'png', 'jpg', 'jpeg'}
    if '.' not in filename or filename.rsplit('.', 1)[1].lower() not in allowed_extensions:
        return jsonify({'error': 'Invalid file type. Allowed: PDF, PNG, JPG'}), 400

    upload_folder = os.path.join(current_app.config.get('UPLOAD_FOLDER', 'uploads'), 'expenses')
    os.makedirs(upload_folder, exist_ok=True)

    filepath = os.path.join(upload_folder, f'{expense_id}_{filename}')
    file.save(filepath)

    old_values = expense.to_dict()
    expense.attachment_path = filepath

    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='expenses',
        record_id=expense.id,
        old_values=old_values,
        new_values=expense.to_dict(),
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'File uploaded', 'expense': expense.to_dict()}), 200


@api_bp.route('/invoices', methods=['GET'])
@api_key_required
def list_invoices():
    user = request.current_user

    query = Invoice.query.filter_by(user_id=user.id)

    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    if start_date:
        try:
            start = validate_date_string(start_date)
            query = query.filter(Invoice.invoice_date >= start)
        except ValueError as e:
            logger.warning('Invalid start_date in list_invoices: %s', e)
            return jsonify({'error': 'Invalid date format'}), 400

    if end_date:
        try:
            end = validate_date_string(end_date)
            query = query.filter(Invoice.invoice_date <= end)
        except ValueError as e:
            logger.warning('Invalid end_date in list_invoices: %s', e)
            return jsonify({'error': 'Invalid date format'}), 400

    invoices = query.order_by(Invoice.invoice_date.desc()).all()
    return jsonify({'invoices': [i.to_dict() for i in invoices]}), 200


@api_bp.route('/invoices', methods=['POST'])
@api_key_required
def create_invoice():
    user = request.current_user
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    try:
        client_name = data.get('client_name', '').strip()
        if not client_name:
            return jsonify({'error': 'Client name is required'}), 400

        customer_id = data.get('customer_id', '').strip()
        if not customer_id:
            return jsonify({'error': 'Customer ID is required'}), 400
        if not validate_uuid(customer_id):
            return jsonify({'error': 'Invalid customer ID'}), 400
        customer = Customer.query.filter_by(id=customer_id, user_id=request.current_user.id).first()
        if not customer:
            return jsonify({'error': 'Customer not found'}), 404

        ex_gst_amount = validate_decimal(data.get('ex_gst_amount'), required=True, min_value=0)
        gst_type = validate_gst_type(data.get('gst_type', 0))
        invoice_date = validate_date_string(data.get('invoice_date'), required=True)
        due_date = validate_date_string(data.get('due_date'))
        account_category_id = data.get('account_category_id')

        if account_category_id and not validate_uuid(account_category_id):
            return jsonify({'error': 'Invalid account category ID'}), 400

        status = data.get('status', 'draft')
        valid_statuses = ['draft', 'sent', 'paid', 'overdue', 'cancelled']
        if status not in valid_statuses:
            return jsonify({'error': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}), 400

        payment_date = validate_date_string(data.get('payment_date'))
        amount_paid = None
        if data.get('amount_paid') is not None and data.get('amount_paid') != '':
            amount_paid = validate_decimal(data.get('amount_paid'), required=True, min_value=0)

        gst_amount = Invoice.calculate_gst(ex_gst_amount, gst_type)
        total_amount = Invoice.calculate_total(ex_gst_amount, gst_amount)

    except ValueError as e:
        logger.warning('Validation error in create_invoice: %s', e)
        return jsonify({'error': 'Invalid input'}), 400

    invoice = Invoice(
        user_id=user.id,
        customer_id=customer_id,
        client_name=client_name,
        description=data.get('description', ''),
        ex_gst_amount=ex_gst_amount,
        gst_amount=gst_amount,
        gst_type=gst_type,
        total_amount=total_amount,
        invoice_date=invoice_date,
        due_date=due_date,
        account_category_id=account_category_id,
        status=status,
        payment_date=payment_date,
        amount_paid=amount_paid
    )

    db.session.add(invoice)
    db.session.flush()

    log_activity(
        user_id=user.id,
        action='CREATE',
        table_name='invoices',
        record_id=invoice.id,
        new_values=invoice.to_dict(),
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'Invoice created', 'invoice': invoice.to_dict()}), 201


@api_bp.route('/invoices/<invoice_id>', methods=['GET'])
@api_key_required
def get_invoice(invoice_id):
    if not validate_uuid(invoice_id):
        return jsonify({'error': 'Invalid invoice ID'}), 400

    invoice = Invoice.query.filter_by(id=invoice_id, user_id=request.current_user.id).first()
    if not invoice:
        return jsonify({'error': 'Invoice not found'}), 404

    return jsonify({'invoice': invoice.to_dict()}), 200


@api_bp.route('/invoices/<invoice_id>', methods=['PUT'])
@api_key_required
def update_invoice(invoice_id):
    if not validate_uuid(invoice_id):
        return jsonify({'error': 'Invalid invoice ID'}), 400

    invoice = Invoice.query.filter_by(id=invoice_id, user_id=request.current_user.id).first()
    if not invoice:
        return jsonify({'error': 'Invoice not found'}), 404

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    old_values = invoice.to_dict()

    try:
        if 'client_name' in data:
            client_name = data['client_name'].strip()
            if not client_name:
                return jsonify({'error': 'Client name cannot be empty'}), 400
            invoice.client_name = client_name

        if 'description' in data:
            invoice.description = data['description']

        # `notes` is a real column on the Invoice model (added with the
        # payment_reference/payment_method/notes migration) but was not
        # settable through the API. Accept it here so provenance notes
        # ("payment received via periodic arrangement", etc.) can be
        # recorded without a direct SQL write.
        if 'notes' in data:
            raw_notes = data['notes']
            invoice.notes = None if raw_notes is None else str(raw_notes)

        if 'ex_gst_amount' in data:
            invoice.ex_gst_amount = validate_decimal(data['ex_gst_amount'], required=True, min_value=0)

        if 'gst_type' in data:
            invoice.gst_type = validate_gst_type(data['gst_type'])

        if 'invoice_date' in data:
            invoice.invoice_date = validate_date_string(data['invoice_date'], required=True)

        if 'due_date' in data:
            invoice.due_date = validate_date_string(data['due_date'])

        if 'account_category_id' in data:
            if data['account_category_id'] and not validate_uuid(data['account_category_id']):
                return jsonify({'error': 'Invalid account category ID'}), 400
            invoice.account_category_id = data['account_category_id']

        if 'customer_id' in data:
            new_customer_id = data['customer_id']
            if not new_customer_id:
                return jsonify({'error': 'Customer ID cannot be empty'}), 400
            if not validate_uuid(new_customer_id):
                return jsonify({'error': 'Invalid customer ID'}), 400
            customer = Customer.query.filter_by(id=new_customer_id, user_id=request.current_user.id).first()
            if not customer:
                return jsonify({'error': 'Customer not found'}), 404
            invoice.customer_id = new_customer_id

        if 'status' in data:
            new_status = data['status']
            valid_statuses = ['draft', 'sent', 'paid', 'overdue', 'cancelled']
            if new_status not in valid_statuses:
                return jsonify({'error': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}), 400
            invoice.status = new_status

        if 'payment_date' in data:
            if data['payment_date']:
                invoice.payment_date = validate_date_string(data['payment_date'], required=True)
            else:
                invoice.payment_date = None

        if 'amount_paid' in data:
            if data['amount_paid'] is None or data['amount_paid'] == '':
                invoice.amount_paid = None
            else:
                invoice.amount_paid = validate_decimal(data['amount_paid'], required=True, min_value=0)

        from datetime import datetime as dt_class
        if 'sent_at' in data:
            if data['sent_at'] is None or data['sent_at'] == '':
                invoice.sent_at = None
            else:
                try:
                    invoice.sent_at = dt_class.fromisoformat(data['sent_at'].replace('Z', '+00:00'))
                except (ValueError, TypeError) as e:
                    logger.warning('Invalid sent_at in update_invoice: %s', e)
                    return jsonify({'error': 'Invalid sent_at timestamp'}), 400

        if 'confirmed_received_at' in data:
            if data['confirmed_received_at'] is None or data['confirmed_received_at'] == '':
                invoice.confirmed_received_at = None
            else:
                try:
                    invoice.confirmed_received_at = dt_class.fromisoformat(data['confirmed_received_at'].replace('Z', '+00:00'))
                except (ValueError, TypeError) as e:
                    logger.warning('Invalid confirmed_received_at in update_invoice: %s', e)
                    return jsonify({'error': 'Invalid confirmed_received_at timestamp'}), 400

        if 'paid_at' in data:
            if data['paid_at'] is None or data['paid_at'] == '':
                invoice.paid_at = None
            else:
                try:
                    invoice.paid_at = dt_class.fromisoformat(data['paid_at'].replace('Z', '+00:00'))
                except (ValueError, TypeError) as e:
                    logger.warning('Invalid paid_at in update_invoice: %s', e)
                    return jsonify({'error': 'Invalid paid_at timestamp'}), 400

        invoice.gst_amount = Invoice.calculate_gst(invoice.ex_gst_amount, invoice.gst_type)
        invoice.total_amount = Invoice.calculate_total(invoice.ex_gst_amount, invoice.gst_amount)

    except ValueError as e:
        logger.warning('Validation error in update_invoice: %s', e)
        return jsonify({'error': 'Invalid input'}), 400

    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='invoices',
        record_id=invoice.id,
        old_values=old_values,
        new_values=invoice.to_dict(),
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'Invoice updated', 'invoice': invoice.to_dict()}), 200


@api_bp.route('/invoices/<invoice_id>', methods=['DELETE'])
@api_key_required
def delete_invoice(invoice_id):
    if not validate_uuid(invoice_id):
        return jsonify({'error': 'Invalid invoice ID'}), 400

    invoice = Invoice.query.filter_by(id=invoice_id, user_id=request.current_user.id).first()
    if not invoice:
        return jsonify({'error': 'Invoice not found'}), 404

    old_values = invoice.to_dict()

    log_activity(
        user_id=request.current_user.id,
        action='DELETE',
        table_name='invoices',
        record_id=invoice.id,
        old_values=old_values,
        ip_address=request.remote_addr
    )

    db.session.delete(invoice)
    db.session.commit()

    return jsonify({'message': 'Invoice deleted'}), 200


@api_bp.route('/invoices/<invoice_id>/mark-paid', methods=['POST'])
@api_key_required
def mark_invoice_paid(invoice_id):
    """
    Mark an invoice as paid. Sets status='paid', records payment_date
    (defaults to today) and amount_paid (defaults to total_amount if not provided).

    2026-10-07 patch: optionally accepts bank-side provenance:
      - payment_reference: free-form NAB ref / OSKO ID etc (max 100 chars)
      - payment_method:    'nab_osko' | 'transfer_credit' | 'direct_debit' | 'cheque' | …
      - notes:             free-form context (e.g. "OSKO batch 07 Oct 14:28 AEST")
      - paid_at:           ISO 8601 timestamp override; defaults to now (UTC)
    """
    if not validate_uuid(invoice_id):
        return jsonify({'error': 'Invalid invoice ID'}), 400

    invoice = Invoice.query.filter_by(id=invoice_id, user_id=request.current_user.id).first()
    if not invoice:
        return jsonify({'error': 'Invoice not found'}), 404

    data = request.get_json() or {}

    old_values = invoice.to_dict()

    payment_date = data.get('payment_date')
    if payment_date:
        try:
            invoice.payment_date = validate_date_string(payment_date, required=True)
        except ValueError as e:
            logger.warning('Invalid payment_date in mark_invoice_paid: %s', e)
            return jsonify({'error': 'Invalid date format'}), 400
    else:
        from datetime import date as date_class
        invoice.payment_date = date_class.today()

    if 'amount_paid' in data and data['amount_paid'] is not None and data['amount_paid'] != '':
        try:
            invoice.amount_paid = validate_decimal(data['amount_paid'], required=True, min_value=0)
        except ValueError as e:
            logger.warning('Invalid amount_paid in mark_invoice_paid: %s', e)
            return jsonify({'error': 'Invalid amount'}), 400
    else:
        invoice.amount_paid = invoice.total_amount

    # ---- 2026-10-07: bank-side provenance + custom paid_at ----
    if 'payment_reference' in data:
        ref = (data.get('payment_reference') or '').strip()
        if len(ref) > 100:
            return jsonify({'error': 'payment_reference too long (max 100 chars)'}), 400
        invoice.payment_reference = ref or None

    if 'payment_method' in data:
        method = (data.get('payment_method') or '').strip()
        if len(method) > 50:
            return jsonify({'error': 'payment_method too long (max 50 chars)'}), 400
        invoice.payment_method = method or None

    if 'notes' in data:
        notes = data.get('notes')
        invoice.notes = notes if notes else None

    paid_at_str = data.get('paid_at')
    if paid_at_str:
        try:
            from datetime import datetime as dt_class, timezone as tz_class
            # Accept ISO 8601 with or without timezone; coerce to UTC for storage.
            parsed = dt_class.fromisoformat(str(paid_at_str).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz_class.utc)
            invoice.paid_at = parsed.astimezone(tz_class.utc)
        except (ValueError, TypeError) as e:
            logger.warning('Invalid paid_at in mark_invoice_paid: %s', e)
            return jsonify({'error': 'Invalid paid_at timestamp'}), 400
    else:
        invoice.paid_at = get_utc_now()

    invoice.status = 'paid'

    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='invoices',
        record_id=invoice.id,
        old_values=old_values,
        new_values=invoice.to_dict(),
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'Invoice marked as paid', 'invoice': invoice.to_dict()}), 200


@api_bp.route('/invoices/<invoice_id>/mark-sent', methods=['POST'])
@api_key_required
def mark_invoice_sent(invoice_id):
    """
    Mark an invoice as sent. Sets status='sent' and records sent_at timestamp
    (defaults to now). Optionally accepts a custom sent_at ISO timestamp.
    """
    if not validate_uuid(invoice_id):
        return jsonify({'error': 'Invalid invoice ID'}), 400

    invoice = Invoice.query.filter_by(id=invoice_id, user_id=request.current_user.id).first()
    if not invoice:
        return jsonify({'error': 'Invoice not found'}), 404

    data = request.get_json() or {}

    old_values = invoice.to_dict()

    sent_at_str = data.get('sent_at')
    if sent_at_str:
        try:
            from datetime import datetime as dt_class
            invoice.sent_at = dt_class.fromisoformat(sent_at_str.replace('Z', '+00:00'))
        except (ValueError, TypeError) as e:
            logger.warning('Invalid sent_at in mark_invoice_sent: %s', e)
            return jsonify({'error': 'Invalid sent_at timestamp'}), 400
    else:
        invoice.sent_at = get_utc_now()

    invoice.status = 'sent'

    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='invoices',
        record_id=invoice.id,
        old_values=old_values,
        new_values=invoice.to_dict(),
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'Invoice marked as sent', 'invoice': invoice.to_dict()}), 200


@api_bp.route('/invoices/<invoice_id>/mark-confirmed', methods=['POST'])
@api_key_required
def mark_invoice_confirmed(invoice_id):
    """
    Mark an invoice as confirmed received. Sets status='sent' (kept) and
    records confirmed_received_at timestamp (defaults to now). Optionally
    accepts a custom confirmed_received_at ISO timestamp.
    """
    if not validate_uuid(invoice_id):
        return jsonify({'error': 'Invalid invoice ID'}), 400

    invoice = Invoice.query.filter_by(id=invoice_id, user_id=request.current_user.id).first()
    if not invoice:
        return jsonify({'error': 'Invoice not found'}), 404

    data = request.get_json() or {}

    old_values = invoice.to_dict()

    confirmed_at_str = data.get('confirmed_received_at')
    if confirmed_at_str:
        try:
            from datetime import datetime as dt_class
            invoice.confirmed_received_at = dt_class.fromisoformat(confirmed_at_str.replace('Z', '+00:00'))
        except (ValueError, TypeError) as e:
            logger.warning('Invalid confirmed_received_at in mark_invoice_confirmed: %s', e)
            return jsonify({'error': 'Invalid confirmed_received_at timestamp'}), 400
    else:
        invoice.confirmed_received_at = get_utc_now()

    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='invoices',
        record_id=invoice.id,
        old_values=old_values,
        new_values=invoice.to_dict(),
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'Invoice confirmed received', 'invoice': invoice.to_dict()}), 200


@api_bp.route('/invoices/<invoice_id>/upload', methods=['POST'])
@api_key_required
def upload_invoice_attachment(invoice_id):
    if not validate_uuid(invoice_id):
        return jsonify({'error': 'Invalid invoice ID'}), 400

    invoice = Invoice.query.filter_by(id=invoice_id, user_id=request.current_user.id).first()
    if not invoice:
        return jsonify({'error': 'Invoice not found'}), 404

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    filename = secure_filename(file.filename)
    allowed_extensions = {'pdf', 'png', 'jpg', 'jpeg'}
    if '.' not in filename or filename.rsplit('.', 1)[1].lower() not in allowed_extensions:
        return jsonify({'error': 'Invalid file type. Allowed: PDF, PNG, JPG'}), 400

    upload_folder = os.path.join(current_app.config.get('UPLOAD_FOLDER', 'uploads'), 'invoices')
    os.makedirs(upload_folder, exist_ok=True)

    filepath = os.path.join(upload_folder, f'{invoice_id}_{filename}')
    file.save(filepath)

    old_values = invoice.to_dict()
    invoice.attachment_path = filepath

    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='invoices',
        record_id=invoice.id,
        old_values=old_values,
        new_values=invoice.to_dict(),
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'File uploaded', 'invoice': invoice.to_dict()}), 200


@api_bp.route('/invoices/<invoice_id>/pdf', methods=['GET'])
@api_key_required
def generate_invoice_pdf(invoice_id):
    if not validate_uuid(invoice_id):
        return jsonify({'error': 'Invalid invoice ID'}), 400

    invoice = Invoice.query.filter_by(id=invoice_id, user_id=request.current_user.id).first()
    if not invoice:
        return jsonify({'error': 'Invoice not found'}), 404

    from app.shared.pdf import generate_invoice_pdf as build_pdf
    buffer = build_pdf(request.current_user, invoice)

    return send_file(
        buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'invoice_{invoice.id[:8]}_{invoice.invoice_date}.pdf'
    )


@api_bp.route('/auth/business', methods=['GET'])
@api_key_required
def get_business_details():
    user = request.current_user
    return jsonify({'user': user.to_dict()}), 200


@api_bp.route('/auth/business', methods=['PUT'])
@api_key_required
def update_business_details():
    user = request.current_user
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    old_values = user.to_dict()

    if 'business_name' in data:
        user.business_name = data['business_name']
    if 'abn' in data:
        user.abn = data['abn']

    # Address — structured only (the legacy `address` field was removed
    # in the 2026-09-17 refactor). Note: user.address_country (the
    # address's country) is distinct from user.country (the user's locale,
    # 2-letter code).
    if 'address' in data:
        return jsonify({
            'error': (
                'The legacy `address` field is no longer supported. '
                'Send structured fields instead: address_line1, city, state, postcode, address_country.'
            )
        }), 400

    addr_error, normalised_addr = _validate_and_normalize_address(
        data, country_default='Australia', country_field='address_country',
    )
    if addr_error:
        return jsonify({'error': addr_error}), 400
    for field in ('address_line1', 'address_line2', 'city', 'state', 'postcode', 'address_country'):
        if field in data:
            setattr(user, field, normalised_addr.get(field, data[field]))

    if 'contact_email' in data:
        user.contact_email = data['contact_email']
    if 'contact_number' in data:
        user.contact_number = data['contact_number']
    if 'bank_name' in data:
        user.bank_name = data['bank_name']
    if 'account_name' in data:
        user.account_name = data['account_name']
    if 'account_number' in data:
        user.account_number_plain = data['account_number']
    if 'bsb' in data:
        user.bsb_plain = data['bsb']
    if 'payment_terms' in data:
        try:
            payment_terms = int(data['payment_terms'])
            if payment_terms < 0:
                return jsonify({'error': 'payment_terms must be a positive integer'}), 400
            user.payment_terms = payment_terms
        except (ValueError, TypeError):
            return jsonify({'error': 'payment_terms must be an integer'}), 400

    log_activity(
        user_id=user.id,
        action='UPDATE',
        table_name='users',
        record_id=user.id,
        old_values=old_values,
        new_values=user.to_dict(),
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'Business details updated', 'user': user.to_dict()}), 200


@api_bp.route('/auth/logo', methods=['POST'])
@api_key_required
def upload_logo():
    user = request.current_user

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    filename = secure_filename(file.filename)
    allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    if '.' not in filename or filename.rsplit('.', 1)[1].lower() not in allowed_extensions:
        return jsonify({'error': 'Invalid file type. Allowed: PNG, JPG, JPEG, GIF, WEBP'}), 400

    upload_folder = os.path.join(current_app.config.get('UPLOAD_FOLDER', 'uploads'), 'logos')
    os.makedirs(upload_folder, exist_ok=True)

    ext = filename.rsplit('.', 1)[1].lower()
    unique_filename = f'{user.id}.{ext}'
    filepath = os.path.join(upload_folder, unique_filename)
    file.save(filepath)

    old_values = {'logo_path': user.logo_path}
    user.logo_path = filepath

    log_activity(
        user_id=user.id,
        action='UPDATE',
        table_name='users',
        record_id=user.id,
        old_values=old_values,
        new_values={'logo_path': user.logo_path},
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'Logo uploaded', 'user': user.to_dict()}), 200


@api_bp.route('/auth/logo', methods=['DELETE'])
@api_key_required
def delete_logo():
    user = request.current_user

    if not user.logo_path:
        return jsonify({'error': 'No logo to delete'}), 404

    try:
        if os.path.exists(user.logo_path):
            os.remove(user.logo_path)
    except OSError:
        pass

    old_values = {'logo_path': user.logo_path}
    user.logo_path = None

    log_activity(
        user_id=user.id,
        action='UPDATE',
        table_name='users',
        record_id=user.id,
        old_values=old_values,
        new_values={'logo_path': None},
        ip_address=request.remote_addr
    )
    db.session.commit()

    return jsonify({'message': 'Logo deleted'}), 200


# ---------------------------------------------------------------------------
# OCR queue endpoints (2026-08-04 refactor)
# ---------------------------------------------------------------------------
# Exposes the ocr_queue table so an operating agent can pick up jobs that the
# new tesseract + regex pipeline couldn't auto-parse, and use its native
# vision tool to inspect the image and PATCH the expense back via the API.
# ---------------------------------------------------------------------------

from app.models.ocr_queue import OcrQueue


@api_bp.route('/ocr-queue', methods=['GET'])
@api_key_required
def list_ocr_queue():
    """List OCR queue jobs. Filter by status with ?status=pending|failed|completed."""
    status = request.args.get('status')
    needs_review = request.args.get('needs_review')  # '1' → only failed jobs with raw_text (review pool)

    q = OcrQueue.query
    if status:
        q = q.filter_by(status=status)
    q = q.order_by(OcrQueue.created_at.desc())
    rows = q.limit(100).all()

    out = []
    for j in rows:
        d = j.to_dict()
        exp = Expense.query.get(j.expense_id)
        d['expense'] = exp.to_dict() if exp else None
        out.append(d)

    if needs_review == '1':
        out = [
            d for d in out
            if d['status'] == 'failed'
            and isinstance(d.get('extracted_data'), dict)
            and 'raw_text' in d['extracted_data']
        ]

    return jsonify({'jobs': out, 'count': len(out)}), 200


@api_bp.route('/ocr-queue/<job_id>', methods=['GET'])
@api_key_required
def get_ocr_queue_job(job_id):
    if not validate_uuid(job_id):
        return jsonify({'error': 'Invalid job ID'}), 400
    job = OcrQueue.query.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    d = job.to_dict()
    exp = Expense.query.get(job.expense_id)
    d['expense'] = exp.to_dict() if exp else None
    return jsonify({'job': d}), 200


@api_bp.route('/ocr-queue/<job_id>/mark-completed', methods=['POST'])
@api_key_required
def mark_ocr_queue_completed(job_id):
    """Mark an OCR job done after manual review (clears requires_review on the expense)."""
    if not validate_uuid(job_id):
        return jsonify({'error': 'Invalid job ID'}), 400
    job = OcrQueue.query.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    job.status = 'completed'
    job.processed_at = get_utc_now()
    exp = Expense.query.get(job.expense_id)
    if exp and exp.requires_review:
        exp.requires_review = False
    db.session.commit()
    return jsonify({'message': 'Marked completed', 'job': job.to_dict()}), 200
