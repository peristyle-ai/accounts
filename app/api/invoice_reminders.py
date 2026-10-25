"""
API endpoints for invoice reminders / statement-of-account log.

Tracks aged-receivable chasing: every statement or chase email sent to a
customer about an invoice (or set of invoices) is logged here so we can
report "how many reminders have been sent" and reconstruct escalation
history.

POST   /api/invoice-reminders             — log a reminder covering N invoices
GET    /api/invoice-reminders             — list reminders (filterable)
GET    /api/invoice-reminders/<id>        — get one
DELETE /api/invoice-reminders/<id>        — delete (admin only)
GET    /api/invoices/<id>/reminders       — list reminders for an invoice
                                            (envelope includes total count)
GET    /api/customers/<id>/reminders      — list reminders for a customer

The model lives at app.models.invoice_reminder.InvoiceReminder.
"""
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from sqlalchemy import and_, or_, func

from app.models import (
    db,
    get_utc_now,
    InvoiceReminder,
    INVOICE_REMINDER_TYPES,
    INVOICE_REMINDER_CHANNELS,
    INVOICE_REMINDER_DIRECTIONS,
    INVOICE_REMINDER_SENDERS,
)
from app.models.invoice import Invoice
from app.models.customer import Customer
from app.shared.decorators import api_key_required, log_activity
from app.shared.roles import role_required


invoice_reminders_bp = Blueprint('invoice_reminders', __name__, url_prefix='/api')


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _parse_dt(value, field_name):
    """Accept ISO 8601 with or without timezone; default to UTC if naive."""
    if not value:
        return None
    try:
        s = str(value).replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        from flask import abort
        abort(400, description=f'{field_name} must be ISO 8601 datetime')


def _enum_or_400(value, choices, field_name):
    if value is None or value == '':
        from flask import abort
        abort(400, description=f'{field_name} is required')
    if value not in choices:
        from flask import abort
        abort(400, description=f'{field_name} must be one of {list(choices)} (got {value!r})')
    return value


def _truncate(s, n):
    if not s:
        return s
    return s if len(s) <= n else s[:n - 1] + '\u2026'


def _days_overdue(invoice, at_when):
    """Compute days_overdue for an invoice at a given datetime (for snapshotting)."""
    if not invoice or not invoice.due_date or not at_when:
        return None
    if not hasattr(at_when, 'date'):
        return None
    as_of = at_when.date() if isinstance(at_when, datetime) else at_when
    delta = (as_of - invoice.due_date).days
    return max(delta, 0)  # 0 if not yet overdue


# ----------------------------------------------------------------------------
# POST /api/invoice-reminders
# ----------------------------------------------------------------------------

@invoice_reminders_bp.route('/invoice-reminders', methods=['POST'])
@api_key_required
def create_invoice_reminder():
    """
    Log one reminder event covering one or more invoices.

    Body:
      invoice_ids:       [str]   — required; one row will be created per invoice
      reminder_type:     str     — required; see INVOICE_REMINDER_TYPES
      channel:           str     — default 'email'
      direction:         str     — default 'outbound'
      sent_at:           ISO dt  — default now (UTC)
      sent_by:           str     — email/name of sender
      sent_by_kind:      str     — 'human' (default) | 'agent' | 'cron' | 'system' | 'unknown'
      email_id:          str     — mail server message id (links rows from the same email)
      pdf_attachment_path: str
      recipients:        str     — free-form "To: ...; Cc: ..."
      subject:           str
      body_excerpt:      str     — auto-truncated to 500 chars
      notes:             str

    Returns: { reminders: [InvoiceReminder.to_dict(), ...] }
    """
    data = request.get_json() or {}

    invoice_ids = data.get('invoice_ids')
    if not invoice_ids or not isinstance(invoice_ids, list) or not all(isinstance(x, str) for x in invoice_ids):
        return jsonify({'error': 'invoice_ids must be a non-empty list of UUID strings'}), 400

    reminder_type = _enum_or_400(data.get('reminder_type'), INVOICE_REMINDER_TYPES, 'reminder_type')
    channel       = data.get('channel') or 'email'
    if channel not in INVOICE_REMINDER_CHANNELS:
        return jsonify({'error': f'channel must be one of {list(INVOICE_REMINDER_CHANNELS)}'}), 400
    direction     = data.get('direction') or 'outbound'
    if direction not in INVOICE_REMINDER_DIRECTIONS:
        return jsonify({'error': f'direction must be one of {list(INVOICE_REMINDER_DIRECTIONS)}'}), 400
    sent_by_kind = data.get('sent_by_kind') or 'human'
    if sent_by_kind not in INVOICE_REMINDER_SENDERS:
        return jsonify({'error': f'sent_by_kind must be one of {list(INVOICE_REMINDER_SENDERS)}'}), 400

    sent_at = _parse_dt(data.get('sent_at'), 'sent_at') or get_utc_now()

    # Load every invoice in one query and confirm they all exist + belong to user
    invoices = Invoice.query.filter(
        Invoice.id.in_(invoice_ids),
        Invoice.user_id == request.current_user.id,
    ).all()

    if len(invoices) != len(set(invoice_ids)):
        found = {i.id for i in invoices}
        missing = [i for i in invoice_ids if i not in found]
        return jsonify({'error': f'Invoice(s) not found: {missing}'}), 404

    # All invoices must belong to a single customer (a reminder covers one
    # customer's debt at a time)
    customer_ids = {i.customer_id for i in invoices}
    if len(customer_ids) > 1:
        return jsonify({
            'error': 'All invoice_ids must belong to the same customer. '
                     f'Got {len(customer_ids)} distinct customers.',
        }), 400

    customer_id = invoices[0].customer_id

    rows = []
    for inv in invoices:
        row = InvoiceReminder(
            user_id=request.current_user.id,
            customer_id=customer_id,
            invoice_id=inv.id,
            reminder_type=reminder_type,
            channel=channel,
            direction=direction,
            sent_at=sent_at,
            sent_by=data.get('sent_by'),
            sent_by_kind=sent_by_kind,
            email_id=data.get('email_id'),
            pdf_attachment_path=data.get('pdf_attachment_path'),
            recipients=data.get('recipients'),
            subject=data.get('subject'),
            body_excerpt=_truncate(data.get('body_excerpt'), 500),
            notes=data.get('notes'),
            days_overdue_at_send=_days_overdue(inv, sent_at),
        )
        db.session.add(row)
        rows.append(row)

    db.session.flush()  # populate ids

    log_activity(
        user_id=request.current_user.id,
        action='CREATE',
        table_name='invoice_reminders',
        record_id=rows[0].id,
        new_values={
            'reminder_type': reminder_type,
            'channel':       channel,
            'direction':     direction,
            'sent_at':       sent_at.isoformat(),
            'sent_by':       data.get('sent_by'),
            'sent_by_kind':  sent_by_kind,
            'email_id':      data.get('email_id'),
            'invoice_ids':   invoice_ids,
            'sibling_count': len(rows) - 1,
        },
        ip_address=request.remote_addr,
    )
    db.session.commit()

    return jsonify({
        'message':   f'Logged {len(rows)} reminder row(s)',
        'reminders': [r.to_dict() for r in rows],
    }), 201


# ----------------------------------------------------------------------------
# GET /api/invoice-reminders
# ----------------------------------------------------------------------------

@invoice_reminders_bp.route('/invoice-reminders', methods=['GET'])
@api_key_required
def list_invoice_reminders():
    """
    List reminders. All filters are optional and combine with AND.

    Query params:
      invoice_id        — exact match
      customer_id       — exact match
      reminder_type     — exact match (or repeat for IN-list)
      channel           — exact match
      direction         — exact match (default outbound-only filter)
      sent_by_kind      — exact match
      email_id          — exact match (groups all rows from one email event)
      since             — ISO datetime; sent_at >= since
      until             — ISO datetime; sent_at <= until
      limit             — default 100, max 500
      offset            — default 0
    """
    q = InvoiceReminder.query.filter_by(user_id=request.current_user.id)

    if request.args.get('invoice_id'):
        q = q.filter(InvoiceReminder.invoice_id == request.args['invoice_id'])
    if request.args.get('customer_id'):
        q = q.filter(InvoiceReminder.customer_id == request.args['customer_id'])
    if request.args.get('reminder_type'):
        types = request.args.getlist('reminder_type')
        if len(types) == 1:
            q = q.filter(InvoiceReminder.reminder_type == types[0])
        else:
            q = q.filter(InvoiceReminder.reminder_type.in_(types))
    if request.args.get('channel'):
        q = q.filter(InvoiceReminder.channel == request.args['channel'])
    if request.args.get('direction'):
        q = q.filter(InvoiceReminder.direction == request.args['direction'])
    if request.args.get('sent_by_kind'):
        q = q.filter(InvoiceReminder.sent_by_kind == request.args['sent_by_kind'])
    if request.args.get('email_id'):
        q = q.filter(InvoiceReminder.email_id == str(request.args['email_id']))
    if request.args.get('since'):
        dt = _parse_dt(request.args['since'], 'since')
        q = q.filter(InvoiceReminder.sent_at >= dt)
    if request.args.get('until'):
        dt = _parse_dt(request.args['until'], 'until')
        q = q.filter(InvoiceReminder.sent_at <= dt)

    try:
        limit = max(1, min(int(request.args.get('limit', 100)), 500))
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        return jsonify({'error': 'limit/offset must be integers'}), 400

    total = q.count()
    rows = q.order_by(InvoiceReminder.sent_at.desc()).limit(limit).offset(offset).all()

    return jsonify({
        'reminders': [r.to_dict() for r in rows],
        'total':     total,
        'limit':     limit,
        'offset':    offset,
    }), 200


# ----------------------------------------------------------------------------
# GET /api/invoice-reminders/<id>
# ----------------------------------------------------------------------------

@invoice_reminders_bp.route('/invoice-reminders/<reminder_id>', methods=['GET'])
@api_key_required
def get_invoice_reminder(reminder_id):
    row = InvoiceReminder.query.filter_by(id=reminder_id, user_id=request.current_user.id).first()
    if not row:
        return jsonify({'error': 'Reminder not found'}), 404
    return jsonify({'reminder': row.to_dict()}), 200


# ----------------------------------------------------------------------------
# PATCH /api/invoice-reminders/<id>
# ----------------------------------------------------------------------------

@invoice_reminders_bp.route('/invoice-reminders/<reminder_id>', methods=['PATCH'])
@api_key_required
def update_invoice_reminder(reminder_id):
    """
    Update mutable outcome fields on a reminder row. Useful for closing the
    loop when a chase pays off, or when a customer responds:

      response_received_at  ISO dt  — when the customer replied
      payment_received_at   ISO dt  — when the invoice was paid
      notes                 str     — free-form update

    These three fields are deliberately write-only via PATCH; everything else
    is immutable (sender, recipient, sent_at, etc.) so the audit trail
    stays clean.
    """
    row = InvoiceReminder.query.filter_by(id=reminder_id, user_id=request.current_user.id).first()
    if not row:
        return jsonify({'error': 'Reminder not found'}), 404

    data = request.get_json() or {}
    old = row.to_dict()

    if 'response_received_at' in data:
        row.response_received_at = _parse_dt(data['response_received_at'], 'response_received_at')
    if 'payment_received_at' in data:
        row.payment_received_at = _parse_dt(data['payment_received_at'], 'payment_received_at')
    if 'notes' in data:
        row.notes = data['notes']

    db.session.flush()
    log_activity(
        user_id=request.current_user.id,
        action='UPDATE',
        table_name='invoice_reminders',
        record_id=reminder_id,
        old_values={k: old.get(k) for k in ('response_received_at', 'payment_received_at', 'notes')},
        new_values={k: getattr(row, k).isoformat() if getattr(row, k, None) and hasattr(getattr(row, k), 'isoformat') else getattr(row, k, None)
                    for k in ('response_received_at', 'payment_received_at', 'notes')},
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return jsonify({'message': 'Reminder updated', 'reminder': row.to_dict()}), 200


# ----------------------------------------------------------------------------
# DELETE /api/invoice-reminders/<id>
# ----------------------------------------------------------------------------

@invoice_reminders_bp.route('/invoice-reminders/<reminder_id>', methods=['DELETE'])
@api_key_required
@role_required('admin')
def delete_invoice_reminder(reminder_id):
    row = InvoiceReminder.query.filter_by(id=reminder_id, user_id=request.current_user.id).first()
    if not row:
        return jsonify({'error': 'Reminder not found'}), 404
    old = row.to_dict()
    db.session.delete(row)
    log_activity(
        user_id=request.current_user.id,
        action='DELETE',
        table_name='invoice_reminders',
        record_id=reminder_id,
        old_values=old,
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return jsonify({'message': 'Reminder deleted'}), 200


# ----------------------------------------------------------------------------
# GET /api/invoices/<invoice_id>/reminders
# ----------------------------------------------------------------------------

@invoice_reminders_bp.route('/invoices/<invoice_id>/reminders', methods=['GET'])
@api_key_required
def list_reminders_for_invoice(invoice_id):
    """
    Return every reminder logged against this invoice, plus a `count` and
    a `last_sent_at` convenience field for UI aging tables.
    """
    inv = Invoice.query.filter_by(id=invoice_id, user_id=request.current_user.id).first()
    if not inv:
        return jsonify({'error': 'Invoice not found'}), 404

    rows = (
        InvoiceReminder.query
        .filter_by(user_id=request.current_user.id, invoice_id=invoice_id)
        .order_by(InvoiceReminder.sent_at.desc())
        .all()
    )

    return jsonify({
        'invoice_id':    invoice_id,
        'invoice_number': inv.id[:8].upper(),
        'count':          len(rows),
        'last_sent_at':   rows[0].sent_at.isoformat() if rows else None,
        'reminders':      [r.to_dict() for r in rows],
    }), 200


# ----------------------------------------------------------------------------
# GET /api/customers/<customer_id>/reminders
# ----------------------------------------------------------------------------

@invoice_reminders_bp.route('/customers/<customer_id>/reminders', methods=['GET'])
@api_key_required
def list_reminders_for_customer(customer_id):
    # Customers are user-scoped; reminders carry their own user_id.
    cust = Customer.query.filter_by(id=customer_id, user_id=request.current_user.id).first()
    if not cust:
        return jsonify({'error': 'Customer not found'}), 404

    q = InvoiceReminder.query.filter_by(user_id=request.current_user.id, customer_id=customer_id)
    rows = q.order_by(InvoiceReminder.sent_at.desc()).all()

    # Aggregate counts per invoice for quick UIs
    per_invoice = {}
    for r in rows:
        per_invoice.setdefault(r.invoice_id, {'count': 0, 'last_sent_at': None, 'types': []})
        per_invoice[r.invoice_id]['count'] += 1
        per_invoice[r.invoice_id]['types'].append(r.reminder_type)
        if (per_invoice[r.invoice_id]['last_sent_at'] is None
                or r.sent_at.isoformat() > per_invoice[r.invoice_id]['last_sent_at']):
            per_invoice[r.invoice_id]['last_sent_at'] = r.sent_at.isoformat()

    return jsonify({
        'customer_id':   customer_id,
        'customer_name': cust.name,
        'count':         len(rows),
        'per_invoice':   per_invoice,
        'reminders':     [r.to_dict() for r in rows],
    }), 200


# ----------------------------------------------------------------------------
# GET /api/invoice-reminders/options  (UI form helper)
# ----------------------------------------------------------------------------

@invoice_reminders_bp.route('/invoice-reminders/options', methods=['GET'])
@api_key_required
def invoice_reminder_options():
    return jsonify({
        'reminder_types':     list(INVOICE_REMINDER_TYPES),
        'channels':           list(INVOICE_REMINDER_CHANNELS),
        'directions':         list(INVOICE_REMINDER_DIRECTIONS),
        'sender_kinds':       list(INVOICE_REMINDER_SENDERS),
    }), 200
