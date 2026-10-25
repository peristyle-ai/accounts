"""Payroll API endpoints — payroll_expansion.md §6.

All endpoints require `@api_key_required` and are scoped to
`request.current_user.id` (multi-tenant from day 1 — see §2 of the plan).

Endpoints:

  Employees
    GET    /api/payroll/employees                       — list, filter by status/position/q
    POST   /api/payroll/employees                       — create
    GET    /api/payroll/employees/<id>                  — detail (+ last 12 pay events)
    PUT    /api/payroll/employees/<id>                  — update (PII *_plain setters)
    POST   /api/payroll/employees/<id>/terminate        — set end_date + status
    DELETE /api/payroll/employees/<id>                  — only when active and zero events

  Pay events
    GET    /api/payroll/pay-events                      — list, filter
    POST   /api/payroll/pay-events                      — create (draft), auto-fill defaults
    GET    /api/payroll/pay-events/<id>                 — detail (+ lines + deliveries)
    PUT    /api/payroll/pay-events/<id>                 — update — only when draft
    POST   /api/payroll/pay-events/<id>/finalize        — draft → finalized
    POST   /api/payroll/pay-events/<id>/mark-paid       — finalized → paid
  POST   /api/payroll/pay-events/<id>/reopen        — finalized → draft (correct a prepared week)
    POST   /api/payroll/pay-events/<id>/cancel          — draft → cancelled
    DELETE /api/payroll/pay-events/<id>                 — only when draft
    POST   /api/payroll/pay-events/<id>/generate-pdf    — server-side PDF
    GET    /api/payroll/pay-events/<id>/pdf             — download the generated PDF
    POST   /api/payroll/pay-events/<id>/send-payslip    — send to work + personal inboxes

  Super payments
    GET    /api/payroll/super-payments                  — list
    POST   /api/payroll/super-payments                  — create (covers N pay events)
    GET    /api/payroll/super-payments/<id>             — detail
    POST   /api/payroll/super-payments/<id>/mark-paid   — pending → paid
    POST   /api/payroll/super-payments/<id>/reconcile   — paid → reconciled

  Summary (powers /hmi/payroll/ dashboard — §7)
    GET    /api/payroll/summary/ytd?fy_year=&employee_id=...    — single-employee YTD
    GET    /api/payroll/summary/super-owing                    — employer-wide super owing
    GET    /api/payroll/summary/payg-withheld                  — PAYG withheld for the FY
    GET    /api/payroll/summary/recent-deliveries?limit=10     — activity feed

PII handling (payroll_expansion.md §12, Phase 1a):
  - `tfn`, `bank_bsb`, `bank_account_number` are encrypted at rest via Fernet.
  - `Employee.to_dict()` returns MASKED values. Always.
  - There is deliberately NO query parameter that returns plaintext. An
    earlier build had `?reveal=true`, which handed the full TFN, BSB and
    account number to any authenticated caller with no role check and no
    audit trail. It was removed on 2026-10-25 (security sweep S5); the
    server-side PDF generator reads the plaintext properties directly, so
    nothing needed it.

Idempotency:
  - `POST /generate-pdf` overwrites the existing PDF file if called twice.
  - `POST /send-payslip` refuses re-sends within a 5-minute window unless
    `force=true`.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import io
import logging
import os

from flask import Blueprint, request, jsonify, current_app, send_file
from sqlalchemy import or_, func

logger = logging.getLogger(__name__)

from app.models import db
from app.models.employee import Employee
from app.models.pay_event import PayEvent, PAY_EVENT_STATUSES
from app.models.pay_event_line import PayEventLine, PAY_EVENT_LINE_TYPES
from app.models.payslip_delivery import (
    PayslipDelivery, PAYSLIP_DELIVERY_STATUSES, PAYSLIP_RECIPIENT_KINDS,
)
from app.models.super_payment import SuperPayment, SUPER_PAYMENT_STATUSES
from app.shared.decorators import api_key_required, log_activity
from app.shared.mail_config import (
    MailServerNotConfigured,
    resolve_mail_server,
)
from app.shared.payslip_recipients import resolve_targets, parse_modes
from app.shared.validators import (
    validate_decimal, validate_date_string, validate_uuid,
)


payroll_api_bp = Blueprint('payroll_api', __name__, url_prefix='/api/payroll')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(payload, status=200):
    return jsonify(payload), status


def _err(message, status=400, **extra):
    out = {'error': message}
    out.update(extra)
    return jsonify(out), status


def _get_owned_employee(user_id, employee_id):
    if not validate_uuid(employee_id):
        return None
    return Employee.query.filter_by(id=employee_id, user_id=user_id).first()


def _get_owned_pay_event(user_id, pay_event_id):
    if not validate_uuid(pay_event_id):
        return None
    return PayEvent.query.filter_by(id=pay_event_id, user_id=user_id).first()


def _get_owned_super_payment(user_id, sp_id):
    if not validate_uuid(sp_id):
        return None
    return SuperPayment.query.filter_by(id=sp_id, user_id=user_id).first()


def _australian_fy_bounds(fy_year):
    """Australian FY (Jul–Jun): fy_year=2026 → 2025-07-01 .. 2026-06-30."""
    return date(fy_year - 1, 7, 1), date(fy_year, 6, 30)


def _infer_fy_year(today=None):
    """Return the FY year-label for `today` under the convention that
    fy_year=Y means the FY ending 30 Jun Y (i.e. runs 1 Jul Y-1 .. 30 Jun Y).

    So Sep 2026 → FY2027, Jan 2027 → FY2027, Jul 2027 → FY2028.
    """
    if today is None:
        today = date.today()
    return today.year + 1 if today.month >= 7 else today.year


# ===========================================================================
# Employees  (§6.1)
# ===========================================================================

@payroll_api_bp.route('/employees', methods=['GET'])
@api_key_required
def list_employees():
    user_id = request.current_user.id
    q = Employee.query.filter_by(user_id=user_id)

    status = (request.args.get('status') or '').strip()
    if status:
        q = q.filter(Employee.employment_status == status)

    position = (request.args.get('position') or '').strip()
    if position:
        q = q.filter(Employee.position.ilike(f"%{position}%"))

    search = (request.args.get('q') or '').strip()
    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            Employee.legal_name.ilike(like),
            Employee.preferred_name.ilike(like),
        ))

    employees = q.order_by(Employee.legal_name.asc()).all()
    # Masked only -- see the PII handling note in the module docstring.
    return _ok({
        'employees': [e.to_dict() for e in employees],
    })


@payroll_api_bp.route('/employees', methods=['POST'])
@api_key_required
def create_employee():
    user_id = request.current_user.id
    data = request.get_json(silent=True) or {}

    legal_name = (data.get('legal_name') or '').strip()
    if not legal_name:
        return _err('legal_name is required', 400)

    e = Employee(
        user_id=user_id,
        legal_name=legal_name,
        preferred_name=(data.get('preferred_name') or '').strip() or None,
        position=(data.get('position') or '').strip() or None,
        email_work=(data.get('email_work') or '').strip() or None,
        email_personal=(data.get('email_personal') or '').strip() or None,
        employment_status=(data.get('employment_status') or 'active').strip(),
        pay_frequency=(data.get('pay_frequency') or 'weekly').strip(),
        super_fund_name=(data.get('super_fund_name') or '').strip() or None,
        super_fund_member_no=(data.get('super_fund_member_no') or '').strip() or None,
        bank_account_name=(data.get('bank_account_name') or '').strip() or None,
        bank_reference_prefix=(data.get('bank_reference_prefix') or '').strip() or None,
        notes=(data.get('notes') or '').strip() or None,
    )

    try:
        if data.get('default_gross_amount') not in (None, ''):
            e.default_gross_amount = validate_decimal(data['default_gross_amount'], min_value=0)
        if data.get('super_rate_pct') not in (None, ''):
            e.super_rate_pct = validate_decimal(data['super_rate_pct'], min_value=0, max_value=100)
    except ValueError as exc:
        logger.warning('Validation error in create_employee (amounts): %s', exc)
        return _err('Invalid amount', 400)

    try:
        if data.get('start_date'):
            e.start_date = validate_date_string(data['start_date'])
        if data.get('end_date'):
            e.end_date = validate_date_string(data['end_date'])
    except ValueError as exc:
        logger.warning('Validation error in create_employee (dates): %s', exc)
        return _err('Invalid date format', 400)

    try:
        if 'tfn' in data and data['tfn'] != '':
            e.tfn_plain = data['tfn']
        if 'bank_bsb' in data and data['bank_bsb'] != '':
            e.bank_bsb_plain = data['bank_bsb']
        if 'bank_account_number' in data and data['bank_account_number'] != '':
            e.bank_account_number_plain = data['bank_account_number']
    except Exception as exc:
        logger.error('PII encryption failed in create_employee: %s', exc, exc_info=True)
        return _err('Internal server error', 500)

    db.session.add(e)
    db.session.commit()

    log_activity(
        user_id=user_id, action='CREATE', table_name='employees',
        record_id=e.id, new_values=e.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()

    return _ok({'message': 'Employee created', 'employee': e.to_dict()}, 201)


@payroll_api_bp.route('/employees/<employee_id>', methods=['GET'])
@api_key_required
def get_employee(employee_id):
    user_id = request.current_user.id
    e = _get_owned_employee(user_id, employee_id)
    if e is None:
        return _err('Employee not found', 404)

    events = (PayEvent.query
              .filter_by(user_id=user_id, employee_id=e.id)
              .order_by(PayEvent.payment_date.desc())
              .limit(12).all())
    supers = (SuperPayment.query
              .filter_by(user_id=user_id, employee_id=e.id)
              .order_by(SuperPayment.remittance_date.desc())
              .limit(12).all())

    return _ok({
        'employee': e.to_dict(),
        'recent_pay_events': [pe.to_dict() for pe in events],
        'recent_super_payments': [sp.to_dict() for sp in supers],
    })


@payroll_api_bp.route('/employees/<employee_id>', methods=['PUT'])
@api_key_required
def update_employee(employee_id):
    user_id = request.current_user.id
    e = _get_owned_employee(user_id, employee_id)
    if e is None:
        return _err('Employee not found', 404)

    data = request.get_json(silent=True) or {}
    if not data:
        return _err('No data provided', 400)

    old_values = e.to_dict()

    if 'legal_name' in data:
        new_name = (data['legal_name'] or '').strip()
        if not new_name:
            return _err('legal_name cannot be empty', 400)
        e.legal_name = new_name
    for attr in ('preferred_name', 'position', 'email_work', 'email_personal',
                 'employment_status', 'pay_frequency', 'super_fund_name',
                 'super_fund_member_no', 'bank_account_name',
                 'bank_reference_prefix', 'notes'):
        if attr in data:
            val = data[attr]
            if isinstance(val, str):
                val = val.strip() or None
            setattr(e, attr, val)

    try:
        if 'default_gross_amount' in data:
            val = data['default_gross_amount']
            e.default_gross_amount = (validate_decimal(val, min_value=0)
                                       if val not in (None, '')
                                       else Decimal('0'))
        if 'super_rate_pct' in data:
            val = data['super_rate_pct']
            e.super_rate_pct = (validate_decimal(val, min_value=0, max_value=100)
                                 if val not in (None, '')
                                 else Decimal('12.00'))
    except ValueError as exc:
        logger.warning('Validation error in update_employee (amounts): %s', exc)
        return _err('Invalid amount', 400)

    try:
        if 'start_date' in data:
            e.start_date = validate_date_string(data['start_date'])
        if 'end_date' in data:
            e.end_date = validate_date_string(data['end_date'])
    except ValueError as exc:
        logger.warning('Validation error in update_employee (dates): %s', exc)
        return _err('Invalid date format', 400)

    try:
        if 'tfn' in data:
            e.tfn_plain = data['tfn'] if data['tfn'] != '' else None
        if 'bank_bsb' in data:
            e.bank_bsb_plain = data['bank_bsb'] if data['bank_bsb'] != '' else None
        if 'bank_account_number' in data:
            e.bank_account_number_plain = (data['bank_account_number']
                                            if data['bank_account_number'] != ''
                                            else None)
    except Exception as exc:
        logger.error('PII encryption failed in update_employee: %s', exc, exc_info=True)
        return _err('Internal server error', 500)

    log_activity(
        user_id=user_id, action='UPDATE', table_name='employees',
        record_id=e.id, old_values=old_values, new_values=e.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return _ok({'message': 'Employee updated', 'employee': e.to_dict()})


@payroll_api_bp.route('/employees/<employee_id>/terminate', methods=['POST'])
@api_key_required
def terminate_employee(employee_id):
    user_id = request.current_user.id
    e = _get_owned_employee(user_id, employee_id)
    if e is None:
        return _err('Employee not found', 404)

    e.employment_status = 'terminated'
    if not e.end_date:
        e.end_date = date.today()
    db.session.commit()
    log_activity(
        user_id=user_id, action='UPDATE', table_name='employees',
        record_id=e.id, new_values=e.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return _ok({'message': 'Employee terminated', 'employee': e.to_dict()})


@payroll_api_bp.route('/employees/<employee_id>', methods=['DELETE'])
@api_key_required
def delete_employee(employee_id):
    user_id = request.current_user.id
    e = _get_owned_employee(user_id, employee_id)
    if e is None:
        return _err('Employee not found', 404)

    if e.employment_status != 'active':
        return _err('Only active employees can be deleted', 409)
    n_events = PayEvent.query.filter_by(user_id=user_id, employee_id=e.id).count()
    if n_events > 0:
        return _err(f'Employee has {n_events} pay events; cannot delete', 409)

    old_values = e.to_dict()
    db.session.delete(e)
    db.session.commit()
    log_activity(
        user_id=user_id, action='DELETE', table_name='employees',
        record_id=employee_id, old_values=old_values,
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return _ok({'message': 'Employee deleted'})


# ===========================================================================
# Pay events  (§6.2)
# ===========================================================================

@payroll_api_bp.route('/pay-events', methods=['GET'])
@api_key_required
def list_pay_events():
    user_id = request.current_user.id
    q = PayEvent.query.filter_by(user_id=user_id)

    employee_id = (request.args.get('employee_id') or '').strip()
    if employee_id:
        if not validate_uuid(employee_id):
            return _err('Invalid employee_id', 400)
        q = q.filter(PayEvent.employee_id == employee_id)

    if request.args.get('start_date'):
        try:
            q = q.filter(PayEvent.payment_date >= validate_date_string(request.args['start_date']))
        except ValueError as exc:
            logger.warning('Invalid start_date in list_pay_events: %s', exc)
            return _err('Invalid date format', 400)
    if request.args.get('end_date'):
        try:
            q = q.filter(PayEvent.payment_date <= validate_date_string(request.args['end_date']))
        except ValueError as exc:
            logger.warning('Invalid end_date in list_pay_events: %s', exc)
            return _err('Invalid date format', 400)

    status = (request.args.get('status') or '').strip()
    if status:
        q = q.filter(PayEvent.status == status)

    events = q.order_by(PayEvent.payment_date.desc()).all()
    return _ok({'pay_events': [pe.to_dict() for pe in events]})


@payroll_api_bp.route('/pay-events', methods=['POST'])
@api_key_required
def create_pay_event():
    user_id = request.current_user.id
    data = request.get_json(silent=True) or {}

    employee_id = (data.get('employee_id') or '').strip()
    if not validate_uuid(employee_id):
        return _err('employee_id is required (UUID)', 400)
    employee = _get_owned_employee(user_id, employee_id)
    if employee is None:
        return _err('Employee not found', 404)

    try:
        payment_date = validate_date_string(data.get('payment_date'))
        pay_period_start = validate_date_string(data.get('pay_period_start'))
        pay_period_end = validate_date_string(data.get('pay_period_end'))
        gross_amount = validate_decimal(data.get('gross_amount'), min_value=0)
        payg_tax_amount = validate_decimal(data.get('payg_tax_amount'), min_value=0)
        net_amount = validate_decimal(data.get('net_amount'), min_value=0)
    except (ValueError, TypeError) as exc:
        logger.warning('Invalid required field in create_pay_event: %s', exc)
        return _err('Invalid input', 400)

    if pay_period_end < pay_period_start:
        return _err('pay_period_end must be >= pay_period_start', 400)

    pay_frequency = (data.get('pay_frequency') or employee.pay_frequency or 'weekly').strip()
    position_snapshot = (data.get('position_snapshot') or employee.position)

    try:
        super_ote_amount = (validate_decimal(data.get('super_ote_amount'), min_value=0)
                             if data.get('super_ote_amount') not in (None, '')
                             else gross_amount)
        explicit_super_payable = (
            validate_decimal(data.get('super_payable_amount'), min_value=0)
            if data.get('super_payable_amount') not in (None, '')
            else None
        )
    except ValueError as exc:
        logger.warning('Validation error in create_pay_event (super): %s', exc)
        return _err('Invalid amount', 400)

    super_payable_amount = (
        explicit_super_payable
        if explicit_super_payable is not None
        else PayEvent.compute_super_payable(super_ote_amount, employee.super_rate_pct)
    )

    pe = PayEvent(
        user_id=user_id,
        employee_id=employee.id,
        payment_date=payment_date,
        pay_period_start=pay_period_start,
        pay_period_end=pay_period_end,
        pay_frequency=pay_frequency,
        position_snapshot=position_snapshot,
        gross_amount=gross_amount,
        payg_tax_amount=payg_tax_amount,
        net_amount=net_amount,
        super_ote_amount=super_ote_amount,
        super_payable_amount=super_payable_amount,
        bank_reference=(data.get('bank_reference') or '').strip() or None,
        status='draft',
    )

    db.session.add(pe)
    db.session.flush()  # need pe.id for line items

    lines_input = data.get('lines')
    if lines_input:
        for line in lines_input:
            if not isinstance(line, dict):
                continue
            try:
                amount = validate_decimal(line.get('amount'), required=True)
            except ValueError as exc:
                logger.warning('Invalid line amount in create_pay_event: %s', exc)
                return _err('Invalid line amount', 400)
            db.session.add(PayEventLine(
                pay_event_id=pe.id,
                line_type=(line.get('line_type') or 'earning'),
                description=line.get('description'),
                quantity=validate_decimal(line['quantity']) if line.get('quantity') not in (None, '') else None,
                rate=validate_decimal(line['rate']) if line.get('rate') not in (None, '') else None,
                amount=amount,
                is_taxable=bool(line.get('is_taxable', True)),
                sort_order=int(line.get('sort_order', 0) or 0),
            ))
    else:
        # Default 3-line breakdown — matches Alice Smith's existing payslips.
        db.session.add(PayEventLine(
            pay_event_id=pe.id, line_type='earning',
            description='Gross wages', amount=gross_amount,
            is_taxable=True, sort_order=0))
        db.session.add(PayEventLine(
            pay_event_id=pe.id, line_type='tax',
            description='PAYG tax withheld',
            amount=-payg_tax_amount, is_taxable=False, sort_order=1))
        db.session.add(PayEventLine(
            pay_event_id=pe.id, line_type='earning',
            description='Net pay deposited', amount=net_amount,
            is_taxable=False, sort_order=2))

    db.session.commit()

    log_activity(
        user_id=user_id, action='CREATE', table_name='pay_events',
        record_id=pe.id, new_values=pe.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()

    return _ok({'message': 'Pay event created', 'pay_event': pe.to_dict()}, 201)


@payroll_api_bp.route('/pay-events/<pay_event_id>', methods=['GET'])
@api_key_required
def get_pay_event(pay_event_id):
    user_id = request.current_user.id
    pe = _get_owned_pay_event(user_id, pay_event_id)
    if pe is None:
        return _err('Pay event not found', 404)

    lines = (PayEventLine.query
             .filter_by(pay_event_id=pe.id)
             .order_by(PayEventLine.sort_order.asc()).all())
    deliveries = (PayslipDelivery.query
                  .filter_by(pay_event_id=pe.id)
                  .order_by(PayslipDelivery.sent_at.desc()).all())

    return _ok({
        'pay_event': pe.to_dict(),
        'lines': [ln.to_dict() for ln in lines],
        'deliveries': [d.to_dict() for d in deliveries],
    })


@payroll_api_bp.route('/pay-events/<pay_event_id>', methods=['PUT'])
@api_key_required
def update_pay_event(pay_event_id):
    user_id = request.current_user.id
    pe = _get_owned_pay_event(user_id, pay_event_id)
    if pe is None:
        return _err('Pay event not found', 404)
    if pe.status != 'draft':
        return _err('Only draft pay events can be edited', 409)

    data = request.get_json(silent=True) or {}
    if not data:
        return _err('No data provided', 400)

    old_values = pe.to_dict()

    try:
        if 'payment_date' in data:
            pe.payment_date = validate_date_string(data['payment_date'])
        if 'pay_period_start' in data:
            pe.pay_period_start = validate_date_string(data['pay_period_start'])
        if 'pay_period_end' in data:
            pe.pay_period_end = validate_date_string(data['pay_period_end'])
    except ValueError as exc:
        logger.warning('Validation error in update_pay_event (dates): %s', exc)
        return _err('Invalid date format', 400)

    if pe.pay_period_end < pe.pay_period_start:
        return _err('pay_period_end must be >= pay_period_start', 400)

    try:
        for attr in ('gross_amount', 'payg_tax_amount', 'net_amount',
                     'super_ote_amount', 'super_payable_amount', 'super_paid_amount'):
            if attr in data and data[attr] not in (None, ''):
                setattr(pe, attr, validate_decimal(data[attr], min_value=0))
    except ValueError as exc:
        logger.warning('Validation error in update_pay_event (amounts): %s', exc)
        return _err('Invalid amount', 400)

    for attr in ('pay_frequency', 'position_snapshot', 'bank_reference', 'notes'):
        if attr in data:
            val = data[attr]
            if isinstance(val, str):
                val = val.strip() or None
            setattr(pe, attr, val)

    if 'super_paid_date' in data:
        try:
            pe.super_paid_date = validate_date_string(data['super_paid_date'])
        except ValueError as exc:
            logger.warning('Invalid super_paid_date in update_pay_event: %s', exc)
            return _err('Invalid date format', 400)

    log_activity(
        user_id=user_id, action='UPDATE', table_name='pay_events',
        record_id=pe.id, old_values=old_values, new_values=pe.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return _ok({'message': 'Pay event updated', 'pay_event': pe.to_dict()})


@payroll_api_bp.route('/pay-events/<pay_event_id>/finalize', methods=['POST'])
@api_key_required
def finalize_pay_event(pay_event_id):
    user_id = request.current_user.id
    pe = _get_owned_pay_event(user_id, pay_event_id)
    if pe is None:
        return _err('Pay event not found', 404)
    if pe.status != 'draft':
        return _err(f'Cannot finalize from status={pe.status}', 409)
    pe.status = 'finalized'
    db.session.commit()
    log_activity(
        user_id=user_id, action='UPDATE', table_name='pay_events',
        record_id=pe.id, new_values=pe.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return _ok({'message': 'Pay event finalized', 'pay_event': pe.to_dict()})


@payroll_api_bp.route('/pay-events/<pay_event_id>/mark-paid', methods=['POST'])
@api_key_required
def mark_pay_event_paid(pay_event_id):
    user_id = request.current_user.id
    pe = _get_owned_pay_event(user_id, pay_event_id)
    if pe is None:
        return _err('Pay event not found', 404)
    if pe.status not in ('finalized', 'paid'):
        return _err(f'Cannot mark paid from status={pe.status}', 409)

    data = request.get_json(silent=True) or {}
    changed = False

    if data.get('super_paid_amount') not in (None, ''):
        try:
            pe.super_paid_amount = validate_decimal(data['super_paid_amount'], min_value=0)
            changed = True
        except ValueError as exc:
            logger.warning('Invalid super_paid_amount in mark_pay_event_paid: %s', exc)
            return _err('Invalid amount', 400)
    if data.get('super_paid_date'):
        try:
            pe.super_paid_date = validate_date_string(data['super_paid_date'])
            changed = True
        except ValueError as exc:
            logger.warning('Invalid super_paid_date in mark_pay_event_paid: %s', exc)
            return _err('Invalid date format', 400)

    # Bank-side confirmation reference for the wage transfer (e.g. ING Osko
    # "Confirmation ID H6051146167"). Distinct from bank_reference, which is
    # the reference that appears on the payee's statement.
    if data.get('payment_reference') is not None:
        ref = str(data['payment_reference']).strip()
        if len(ref) > 100:
            return _err('payment_reference too long (max 100 chars)', 400)
        pe.payment_reference = ref or None
        changed = True

    # Optional free-text notes (e.g. "Paid via ING Osko, status PROCESSING").
    if data.get('notes') is not None:
        pe.notes = data['notes'] or None
        changed = True

    if pe.status != 'paid':
        pe.status = 'paid'
        changed = True

    # NOTE: commit whenever ANY field changed, not only on the status
    # transition. Previously the commit lived inside `if pe.status != 'paid'`,
    # so calling this endpoint on an already-paid event set the fields on the
    # ORM object, echoed them back in the JSON response, and then discarded
    # them without writing to the database.
    if changed:
        db.session.commit()
        log_activity(
            user_id=user_id, action='UPDATE', table_name='pay_events',
            record_id=pe.id, new_values=pe.to_dict(),
            ip_address=request.remote_addr,
        )
        db.session.commit()

    return _ok({'message': 'Pay event marked paid', 'pay_event': pe.to_dict()})


@payroll_api_bp.route('/pay-events/<pay_event_id>/cancel', methods=['POST'])
@api_key_required
def cancel_pay_event(pay_event_id):
    user_id = request.current_user.id
    pe = _get_owned_pay_event(user_id, pay_event_id)
    if pe is None:
        return _err('Pay event not found', 404)
    if pe.status != 'draft':
        return _err(f'Only draft pay events can be cancelled (current={pe.status})', 409)
    pe.status = 'cancelled'
    db.session.commit()
    log_activity(
        user_id=user_id, action='UPDATE', table_name='pay_events',
        record_id=pe.id, new_values=pe.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return _ok({'message': 'Pay event cancelled', 'pay_event': pe.to_dict()})


@payroll_api_bp.route('/pay-events/<pay_event_id>/reopen', methods=['POST'])
@api_key_required
def reopen_pay_event(pay_event_id):
    """Move a finalized pay event back to draft so it can be edited.

    Why this exists (2026-10-22): the weekly payroll run "prepare" phase
    finalizes the pay event so the export endpoints (which filter on
    status finalized/paid) will include it. Finalizing locks the amounts
    -- only draft events can be edited, cancelled or deleted. If the
    prepared week turns out to have different hours than the default
    assumption, there was previously no way to correct it short of
    hand-editing the database.

    Only a finalized event that has NOT been paid can be reopened. A
    paid event is a statement that money moved, so it stays put.
    """
    user_id = request.current_user.id
    pe = _get_owned_pay_event(user_id, pay_event_id)
    if pe is None:
        return _err('Pay event not found', 404)
    if pe.status != 'finalized':
        return _err(
            f'Only a finalized pay event can be reopened (current={pe.status})',
            409)

    old_status = pe.status
    pe.status = 'draft'
    db.session.commit()
    log_activity(
        user_id=user_id, action='UPDATE', table_name='pay_events',
        record_id=pe.id,
        new_values={'status': 'draft', 'previous_status': old_status},
        ip_address=request.remote_address,
    )
    db.session.commit()
    return _ok({
        'message': 'Pay event reopened to draft',
        'pay_event': pe.to_dict(),
    })


@payroll_api_bp.route('/pay-events/<pay_event_id>', methods=['DELETE'])
@api_key_required
def delete_pay_event(pay_event_id):
    user_id = request.current_user.id
    pe = _get_owned_pay_event(user_id, pay_event_id)
    if pe is None:
        return _err('Pay event not found', 404)
    if pe.status != 'draft':
        return _err(f'Only draft pay events can be deleted (current={pe.status})', 409)
    db.session.delete(pe)
    db.session.commit()
    log_activity(
        user_id=user_id, action='DELETE', table_name='pay_events',
        record_id=pay_event_id, ip_address=request.remote_addr,
    )
    db.session.commit()
    return _ok({'message': 'Pay event deleted'})


# ---------------------------------------------------------------------------
# PDF generation + download + email send
# ---------------------------------------------------------------------------

def _payslip_pdf_path(user_id, pay_event_id):
    upload_root = current_app.config.get('UPLOAD_FOLDER', 'uploads')
    return os.path.abspath(os.path.join(upload_root, 'payslips', str(user_id),
                                         f'{pay_event_id}.pdf'))


def _generate_payslip_pdf(user, employee, pay_event):
    """Server-side PDF generation. Idempotent — overwrites existing file."""
    from app.shared.pdf import generate_payslip_pdf as _gen
    lines = (PayEventLine.query
             .filter_by(pay_event_id=pay_event.id)
             .order_by(PayEventLine.sort_order.asc()).all())
    buf = _gen(user, employee, pay_event, lines=lines)

    path = _payslip_pdf_path(user.id, pay_event.id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(buf.getvalue())
    return path


@payroll_api_bp.route('/pay-events/<pay_event_id>/generate-pdf', methods=['POST'])
@api_key_required
def generate_payslip(pay_event_id):
    user_id = request.current_user.id
    pe = _get_owned_pay_event(user_id, pay_event_id)
    if pe is None:
        return _err('Pay event not found', 404)
    employee = Employee.query.get(pe.employee_id)
    if employee is None:
        return _err('Employee not found', 404)

    try:
        path = _generate_payslip_pdf(request.current_user, employee, pe)
    except Exception as exc:
        logger.error('PDF generation failed in generate_payslip: %s', exc, exc_info=True)
        return _err('Internal server error', 500)

    rel = os.path.relpath(path, os.path.abspath(
        current_app.config.get('UPLOAD_FOLDER', 'uploads')))
    pe.payslip_pdf_path = rel
    db.session.commit()

    return _ok({
        'message': 'Payslip PDF generated',
        'payslip_pdf_path': rel,
        'size_bytes': os.path.getsize(path),
    })


@payroll_api_bp.route('/pay-events/<pay_event_id>/pdf', methods=['GET'])
@api_key_required
def download_payslip(pay_event_id):
    user_id = request.current_user.id
    pe = _get_owned_pay_event(user_id, pay_event_id)
    if pe is None:
        return _err('Pay event not found', 404)

    path = _payslip_pdf_path(user_id, pe.id)
    if not os.path.exists(path):
        employee = Employee.query.get(pe.employee_id)
        if employee is None:
            return _err('Employee not found', 404)
        try:
            path = _generate_payslip_pdf(request.current_user, employee, pe)
            rel = os.path.relpath(path, os.path.abspath(
                current_app.config.get('UPLOAD_FOLDER', 'uploads')))
            pe.payslip_pdf_path = rel
            db.session.commit()
        except Exception as exc:
            logger.error('PDF generation failed in download_payslip: %s', exc, exc_info=True)
            return _err('Internal server error', 500)

    filename = f'payslip-{pe.id[:8]}-{pe.payment_date.isoformat()}.pdf'
    return send_file(path, mimetype='application/pdf', as_attachment=True,
                     download_name=filename)


def _recipient_modes(data=None):
    """Which recipient kinds ('work', 'personal') a payslip send may use.

    Precedence: per-call override in the POST body (`recipient_kinds`)
    > the `PAYSLIP_RECIPIENT_MODES` system setting > the default
    'work,personal'.

    See app/shared/payslip_recipients.py for the rationale.
    """
    modes = None
    if isinstance(data, dict):
        modes = data.get('recipient_kinds')
    if modes is None:
        from app.models.system_setting import get_setting
        modes = get_setting('PAYSLIP_RECIPIENT_MODES', 'work,personal')
    return parse_modes(modes)


@payroll_api_bp.route('/pay-events/<pay_event_id>/send-payslip', methods=['POST'])
@api_key_required
def send_payslip(pay_event_id):
    """Send payslip via the local mail server.

    Creates one `payslip_deliveries` row per recipient. Records
    `delivery_status='queued'` after a successful POST. Idempotent on
    (pay_event_id, recipient_email) within a 5-minute window unless
    `force=true`.

    The mail-server call is best-effort: if the POST fails, the row is still
    recorded with `delivery_status='failed'` so a future Phase 3 worker can
    retry. Phase 1a just records intent + status.

    Recipients: by default both `email_work` and `email_personal` are
    sent to. The `PAYSLIP_RECIPIENT_MODES` system setting narrows that at
    runtime ('work', 'personal', or 'work,personal') with no code change.
    A per-call override can be passed in the POST body as
    `{"recipient_kinds": ["personal"]}`.
    """
    user = request.current_user
    user_id = user.id
    pe = _get_owned_pay_event(user_id, pay_event_id)
    if pe is None:
        return _err('Pay event not found', 404)
    if pe.status not in ('finalized', 'paid'):
        return _err(f'Only finalized/paid events can be emailed (current={pe.status})', 409)

    employee = Employee.query.get(pe.employee_id)
    if employee is None:
        return _err('Employee not found', 404)

    data = request.get_json(silent=True) or {}
    force = str(data.get('force', '')).lower() in ('1', 'true', 'yes')

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    recent = (PayslipDelivery.query
              .filter_by(pay_event_id=pe.id)
              .filter(PayslipDelivery.sent_at >= cutoff)
              .all())
    if recent and not force:
        return _err(
            'Already sent within 5 min. Pass force=true to override.',
            409, recent_deliveries=[d.to_dict() for d in recent],
        )

    # Ensure the PDF exists on disk
    pdf_path = _payslip_pdf_path(user_id, pe.id)
    if not os.path.exists(pdf_path):
        try:
            pdf_path = _generate_payslip_pdf(user, employee, pe)
        except Exception as exc:
            logger.error('PDF generation failed in send_payslip: %s', exc, exc_info=True)
            return _err('Internal server error', 500)

    targets = resolve_targets(employee, _recipient_modes(data))
    if not targets:
        return _err(
            'No payslip recipients resolved. Employee has '
            f'email_work={employee.email_work!r} email_personal={employee.email_personal!r}; '
            f'recipient kinds allowed = {_recipient_modes(data)}.', 400)

    # Resolve the mail-server settings ONCE, up front, and fail loudly if
    # they are not configured. Resolving inside the per-recipient loop (as
    # this used to) meant a configuration error was recorded as a per-
    # recipient DELIVERY failure and the endpoint still answered HTTP 200
    # "Payslip sent to N recipient(s)" -- an operator could not tell a
    # misconfigured mail server from a successful send. No delivery row is
    # created here because no delivery was attempted.
    try:
        mail_settings = resolve_mail_server(current_app.config)
    except MailServerNotConfigured as exc:
        logger.error('Mail server not configured: %s', exc)
        return _err(str(exc), 503)

    deliveries = []
    # Business name lookup: explicit user setting > system_settings table.
    # Stored on User.business_name at onboarding; system_settings is the
    # admin-tunable fallback (set via PUT /api/settings/<key>).
    from app.models.system_setting import get_setting
    business_name = user.business_name or get_setting('BUSINESS_NAME')
    subject = (f'Your payslip \u2013 {pe.pay_period_start.isoformat()} '
               f'to {pe.pay_period_end.isoformat()} ({business_name})')

    body = (
        f"Hi {employee.preferred_name or employee.legal_name},\n\n"
        f"Please find attached your payslip for the pay period\n"
        f"  {pe.pay_period_start.isoformat()} to {pe.pay_period_end.isoformat()}\n"
        f"  (payment date {pe.payment_date.isoformat()}).\n\n"
        f"  Gross: ${float(pe.gross_amount):,.2f}\n"
        f"  PAYG withheld: ${float(pe.payg_tax_amount):,.2f}\n"
        f"  Net deposited: ${float(pe.net_amount):,.2f}\n"
        f"  Super (paid separately): ${float(pe.super_payable_amount):,.2f}\n\n"
        f"Bank reference: {pe.bank_reference or 'n/a'}\n\n"
        f"\u2014 the operating agent"
    )

    from app.shared.pdf import generate_payslip_pdf as _gen_for_send
    mail_results = []
    mail_from = current_app.config.get(
        'MAIL_FROM_ADDRESS',
        os.environ.get('MAIL_FROM_ADDRESS', ''),
    )
    mail_from_domain = current_app.config.get(
        'MAIL_FROM_DOMAIN',
        os.environ.get('MAIL_FROM_DOMAIN', ''),
    )
    for kind, email_addr in targets:
        d = PayslipDelivery(
            pay_event_id=pe.id,
            recipient_email=email_addr,
            recipient_kind=kind,
            sent_from=mail_from,
            subject=subject,
            attachment_count=1,
            attachment_paths=[os.path.relpath(pdf_path, os.path.abspath(
                current_app.config.get('UPLOAD_FOLDER', 'uploads')))],
            delivery_status='queued',
        )
        db.session.add(d)
        db.session.flush()

        # Try the mail server. If it fails we record the failure but still
        # return success — the row stays so a future worker can retry.
        try:
            import requests
            from flask import current_app as _ca
            # The mail-server settings were resolved once, before the
            # loop, and there is NO hardcoded default host anywhere: the
            # old fallback was 'https://mail.example.com:5003', a reserved
            # documentation domain, so a deployment that had not set
            # MAIL_SERVER_API silently dialled a host that can never exist.
            mail_api = mail_settings.api
            mail_user = mail_settings.user
            mail_pass = mail_settings.password
            # The mail server serves HTTPS with a self-signed cert, so
            # requests needs an explicit CA bundle. Unset means "use the
            # system trust store" (correct for a public-CA endpoint).
            mail_ca = mail_settings.ca_bundle or True
            # Authenticate with the mail server first (its /api/emails/mime
            # requires a Bearer token — bare POST returns 401 "Token is missing").
            auth_token = None
            if mail_pass:
                try:
                    auth_resp = requests.post(
                        f'{mail_api}/auth/login',
                        json={'email': mail_user, 'password': mail_pass},
                        timeout=10,
                        verify=mail_ca,
                    )
                    if auth_resp.ok:
                        auth_token = (auth_resp.json() or {}).get('token')
                except Exception as auth_exc:
                    current_app.logger.warning(
                        'Mail auth login failed for %s: %s', mail_user, auth_exc)
            # Re-read the PDF bytes for the attachment (mail server expects inline).
            with open(pdf_path, 'rb') as _f:
                pdf_bytes = _f.read()
            # Build a raw MIME message — /api/emails/mime expects
            # `mime_content` (ASCII string of a fully-formed MIME message),
            # not a structured JSON envelope.
            from email.message import EmailMessage as _EmailMessage
            from email.utils import formatdate as _formatdate, make_msgid as _make_msgid
            _msg = _EmailMessage()
            _msg['From'] = mail_from
            _msg['To'] = email_addr
            _msg['Subject'] = subject
            _msg['Date'] = _formatdate(localtime=True)
            _msg['Message-ID'] = _make_msgid(domain=mail_from_domain or 'localhost')
            _msg.set_content(body)
            _msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf',
                                filename=os.path.basename(pdf_path))
            mime_bytes = _msg.as_bytes()
            if any(_b >= 128 for _b in mime_bytes):
                # Fall back to latin-1 if any non-ascii slipped in.
                mime_str = mime_bytes.decode('latin-1')
            else:
                mime_str = mime_bytes.decode('ascii')
            mime_headers = {'Content-Type': 'application/json'}
            if auth_token:
                mime_headers['Authorization'] = f'Bearer {auth_token}'
            payload = {'to': email_addr, 'mime_content': mime_str}
            r = requests.post(f'{mail_api}/api/emails/mime',
                              json=payload, headers=mime_headers, timeout=15,
                              verify=mail_ca)
            if r.ok:
                try:
                    resp = r.json()
                except Exception:
                    resp = {'raw': r.text[:500]}
                d.delivery_id = (resp.get('email_id')
                                  or resp.get('id')
                                  or resp.get('message_id'))
                d.delivery_status = 'sent'
                d.mail_api_response = resp
            else:
                d.delivery_status = 'failed'
                d.error_message = f'HTTP {r.status_code}: {r.text[:500]}'
                d.mail_api_response = {'status_code': r.status_code, 'body': r.text[:500]}
        except Exception as exc:
            d.delivery_status = 'failed'
            # Stored in DB for server-side debugging only — not sent to client.
            d.error_message = str(exc)
            logger.warning('Mail send failed for %s: %s', email_addr, exc)
        db.session.commit()
        mail_results.append(d.to_dict())

    # Report what ACTUALLY happened. The old message counted the number of
    # targets, so a run in which every delivery failed still answered
    # "Payslip sent to 2 recipient(s)" -- the caller (and the operator
    # reading the log) had no way to tell success from total failure
    # without reading each delivery row.
    sent = [d for d in mail_results if d.get('delivery_status') == 'sent']
    failed = [d for d in mail_results if d.get('delivery_status') != 'sent']
    if not failed:
        message = f'Payslip sent to {len(sent)} recipient(s)'
    elif not sent:
        message = (f'Payslip NOT sent: all {len(failed)} recipient(s) failed')
    else:
        message = (f'Payslip sent to {len(sent)} of {len(targets)} recipient(s); '
                   f'{len(failed)} failed')

    payload = {
        'deliveries': mail_results,
        'sent_count': len(sent),
        'failed_count': len(failed),
    }
    if failed:
        payload['errors'] = [
            {
                'recipient_email': d.get('recipient_email'),
                'delivery_status': d.get('delivery_status'),
                'error_message': d.get('error_message'),
            }
            for d in failed
        ]
        # A partial or total failure is a failure. Answer 502 so the
        # caller cannot mistake it for a successful send.
        # NOTE: _err's first parameter is also called `message`, so the
        # message is passed positionally and NOT duplicated in **payload.
        return _err(message, 502, **payload)
    payload['message'] = message
    return _ok(payload)


# ===========================================================================
# Super payments  (§6.3)
# ===========================================================================

@payroll_api_bp.route('/super-payments', methods=['GET'])
@api_key_required
def list_super_payments():
    user_id = request.current_user.id
    q = SuperPayment.query.filter_by(user_id=user_id)

    employee_id = (request.args.get('employee_id') or '').strip()
    if employee_id:
        if not validate_uuid(employee_id):
            return _err('Invalid employee_id', 400)
        q = q.filter(SuperPayment.employee_id == employee_id)
    if request.args.get('start_date'):
        try:
            q = q.filter(SuperPayment.remittance_date >= validate_date_string(request.args['start_date']))
        except ValueError as exc:
            logger.warning('Invalid start_date in list_super_payments: %s', exc)
            return _err('Invalid date format', 400)
    if request.args.get('end_date'):
        try:
            q = q.filter(SuperPayment.remittance_date <= validate_date_string(request.args['end_date']))
        except ValueError as exc:
            logger.warning('Invalid end_date in list_super_payments: %s', exc)
            return _err('Invalid date format', 400)

    rows = q.order_by(SuperPayment.remittance_date.desc()).all()
    return _ok({'super_payments': [sp.to_dict() for sp in rows]})


@payroll_api_bp.route('/super-payments', methods=['POST'])
@api_key_required
def create_super_payment():
    user_id = request.current_user.id
    data = request.get_json(silent=True) or {}

    employee_id = (data.get('employee_id') or '').strip()
    if not validate_uuid(employee_id):
        return _err('employee_id is required (UUID)', 400)
    employee = _get_owned_employee(user_id, employee_id)
    if employee is None:
        return _err('Employee not found', 404)

    pay_event_ids = data.get('pay_event_ids') or []
    if not isinstance(pay_event_ids, list):
        return _err('pay_event_ids must be a list of UUIDs', 400)

    try:
        remittance_date = validate_date_string(data.get('remittance_date'))
        amount = validate_decimal(data.get('amount'), min_value=0)
    except (ValueError, TypeError) as exc:
        logger.warning('Invalid required field in create_super_payment: %s', exc)
        return _err('Invalid input', 400)

    # Validate each pay_event_id belongs to this user/employee
    if pay_event_ids:
        n = (PayEvent.query
             .filter(PayEvent.user_id == user_id,
                     PayEvent.employee_id == employee.id,
                     PayEvent.id.in_(pay_event_ids))
             .count())
        if n != len(set(pay_event_ids)):
            return _err('One or more pay_event_ids do not belong to this employee', 400)

    sp = SuperPayment(
        user_id=user_id,
        employee_id=employee.id,
        remittance_date=remittance_date,
        amount=amount,
        pay_event_ids=pay_event_ids,
        fund_name_snapshot=(data.get('fund_name_snapshot') or employee.super_fund_name),
        fund_member_snapshot=(data.get('fund_member_snapshot') or employee.super_fund_member_no),
        payment_reference=(data.get('payment_reference') or '').strip() or None,
        notes=(data.get('notes') or '').strip() or None,
        status='pending',
    )
    db.session.add(sp)
    db.session.commit()
    log_activity(
        user_id=user_id, action='CREATE', table_name='super_payments',
        record_id=sp.id, new_values=sp.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return _ok({'message': 'Super payment created', 'super_payment': sp.to_dict()}, 201)


@payroll_api_bp.route('/super-payments/<sp_id>', methods=['GET'])
@api_key_required
def get_super_payment(sp_id):
    user_id = request.current_user.id
    sp = _get_owned_super_payment(user_id, sp_id)
    if sp is None:
        return _err('Super payment not found', 404)
    return _ok({'super_payment': sp.to_dict()})


@payroll_api_bp.route('/super-payments/<sp_id>/mark-paid', methods=['POST'])
@api_key_required
def mark_super_payment_paid(sp_id):
    user_id = request.current_user.id
    sp = _get_owned_super_payment(user_id, sp_id)
    if sp is None:
        return _err('Super payment not found', 404)
    if sp.status != 'pending':
        return _err(f'Cannot mark paid from status={sp.status}', 409)

    data = request.get_json(silent=True) or {}

    # Optional bank-side reference (e.g. NAB confirmation number).
    # If caller didn't provide one, leave the existing value alone.
    if data.get('payment_reference') is not None:
        ref = str(data['payment_reference']).strip()
        if len(ref) > 100:
            return _err('payment_reference too long (max 100 chars)', 400)
        sp.payment_reference = ref or None

    # Optional free-text notes (e.g. "manual NAB IB upload due to wizard gap").
    if data.get('notes') is not None:
        sp.notes = data['notes'] or None

    # Optional bank-side remittance date — if caller paid on a different day
    # than the planned remittance_date, let them correct it.
    if data.get('remittance_date'):
        try:
            sp.remittance_date = validate_date_string(data['remittance_date'], required=True)
        except ValueError as exc:
            logger.warning('Invalid remittance_date in mark_super_payment_paid: %s', exc)
            return _err('Invalid date format', 400)

    sp.status = 'paid'
    # Also stamp the per-event columns for the linked events so the YTD
    # summary and per-employee view see the cleared super.
    if sp.pay_event_ids:
        events = (PayEvent.query
                  .filter(PayEvent.user_id == user_id,
                          PayEvent.id.in_(sp.pay_event_ids))
                  .all())
        share = (sp.amount / Decimal(len(events))).quantize(Decimal('0.01')) if events else None
        for ev in events:
            ev.super_paid_amount = share
            ev.super_paid_date = sp.remittance_date
    db.session.commit()
    log_activity(
        user_id=user_id, action='UPDATE', table_name='super_payments',
        record_id=sp.id, new_values=sp.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return _ok({'message': 'Super payment marked paid', 'super_payment': sp.to_dict()})


@payroll_api_bp.route('/super-payments/<sp_id>', methods=['PUT'])
@api_key_required
def update_super_payment(sp_id):
    """Correct a super payment while it is still pending.

    Super payments record what was actually remitted to the fund, so a
    paid/reconciled row is immutable. While the row is still 'pending' the
    remittance details can be corrected -- most often the fund member
    number, which was captured as a snapshot when the row was created and
    may predate a correction to the employee record.
    """
    user_id = request.current_user.id
    sp = _get_owned_super_payment(user_id, sp_id)
    if sp is None:
        return _err('Super payment not found', 404)
    if sp.status != 'pending':
        return _err(
            f'Cannot edit a super payment with status={sp.status}; '
            'only pending payments can be corrected', 409)

    data = request.get_json(silent=True) or {}
    if not data:
        return _err('No data provided', 400)

    if 'fund_member_snapshot' in data:
        val = data['fund_member_snapshot']
        if val is not None:
            val = str(val).strip() or None
            if val and len(val) > 50:
                return _err('fund_member_snapshot too long (max 50 chars)', 400)
        sp.fund_member_snapshot = val
    if 'fund_name_snapshot' in data:
        val = data['fund_name_snapshot']
        sp.fund_name_snapshot = (str(val).strip() or None) if val is not None else None
    if 'notes' in data:
        sp.notes = data['notes'] or None
    if 'remittance_date' in data:
        try:
            sp.remittance_date = validate_date_string(
                data['remittance_date'], required=True)
        except ValueError as exc:
            logger.warning('Invalid remittance_date in update_super_payment: %s', exc)
            return _err('Invalid date format', 400)
    if 'amount' in data:
        try:
            sp.amount = validate_decimal(data['amount'], min_value=0)
        except (ValueError, TypeError) as exc:
            logger.warning('Invalid amount in update_super_payment: %s', exc)
            return _err('Invalid input', 400)

    db.session.commit()
    log_activity(
        user_id=user_id, action='UPDATE', table_name='super_payments',
        record_id=sp.id, new_values=sp.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return _ok({'message': 'Super payment updated', 'super_payment': sp.to_dict()})


@payroll_api_bp.route('/super-payments/<sp_id>/reconcile', methods=['POST'])
@api_key_required
def reconcile_super_payment(sp_id):
    user_id = request.current_user.id
    sp = _get_owned_super_payment(user_id, sp_id)
    if sp is None:
        return _err('Super payment not found', 404)
    if sp.status != 'paid':
        return _err(f'Cannot reconcile from status={sp.status}', 409)
    sp.status = 'reconciled'
    db.session.commit()
    log_activity(
        user_id=user_id, action='UPDATE', table_name='super_payments',
        record_id=sp.id, new_values=sp.to_dict(),
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return _ok({'message': 'Super payment reconciled', 'super_payment': sp.to_dict()})


# ===========================================================================
# Summary  (§6.4) — powers the HMI /payroll dashboard
# ===========================================================================

def _q_pay_events_for_fy(user_id, fy_year, employee_id=None):
    start, end = _australian_fy_bounds(fy_year)
    q = (PayEvent.query
         .filter(PayEvent.user_id == user_id,
                 PayEvent.payment_date >= start,
                 PayEvent.payment_date <= end,
                 PayEvent.status != 'cancelled'))
    if employee_id:
        q = q.filter(PayEvent.employee_id == employee_id)
    return q


def _sum_pay_events(q):
    """Aggregate the pay-events query — returns dict of Decimal totals."""
    totals = {
        'gross': Decimal('0'), 'payg_tax': Decimal('0'),
        'net': Decimal('0'), 'super_payable': Decimal('0'),
        'super_paid': Decimal('0'),
        'count': 0,
    }
    rows = q.with_entities(
        func.coalesce(func.sum(PayEvent.gross_amount), 0),
        func.coalesce(func.sum(PayEvent.payg_tax_amount), 0),
        func.coalesce(func.sum(PayEvent.net_amount), 0),
        func.coalesce(func.sum(PayEvent.super_payable_amount), 0),
        func.coalesce(func.sum(PayEvent.super_paid_amount), 0),
        func.count(PayEvent.id),
    ).first()
    if rows:
        totals['gross'] = Decimal(rows[0])
        totals['payg_tax'] = Decimal(rows[1])
        totals['net'] = Decimal(rows[2])
        totals['super_payable'] = Decimal(rows[3])
        totals['super_paid'] = Decimal(rows[4])
        totals['count'] = int(rows[5])
    totals['super_outstanding'] = (totals['super_payable'] - totals['super_payable']
                                     + (totals['super_payable'] - totals['super_paid']))
    return totals


@payroll_api_bp.route('/summary', methods=['GET'])
@api_key_required
def summary_overall():
    """Employer-wide summary for a FY. Powers the dashboard headline."""
    user_id = request.current_user.id
    fy_year_raw = request.args.get('fy_year')
    if fy_year_raw:
        try:
            fy_year = int(fy_year_raw)
        except (TypeError, ValueError):
            return _err('fy_year must be an integer', 400)
    else:
        fy_year = _infer_fy_year()

    fy_start, fy_end = _australian_fy_bounds(fy_year)

    pay_events_q = _q_pay_events_for_fy(user_id, fy_year)
    totals = _sum_pay_events(pay_events_q)

    # Per-status counts
    status_rows = (pay_events_q
                   .with_entities(PayEvent.status,
                                  func.count(PayEvent.id))
                   .group_by(PayEvent.status).all())
    pay_event_counts = {s: 0 for s in PAY_EVENT_STATUSES}
    for status, n in status_rows:
        pay_event_counts[status] = int(n)

    # Per-employee mini totals
    per_emp_rows = (pay_events_q
                    .with_entities(
                        PayEvent.employee_id,
                        func.coalesce(func.sum(PayEvent.gross_amount), 0),
                        func.coalesce(func.sum(PayEvent.net_amount), 0),
                        func.coalesce(func.sum(PayEvent.super_paid_amount), 0),
                        func.count(PayEvent.id),
                        func.max(PayEvent.payment_date),
                    )
                    .group_by(PayEvent.employee_id).all())

    # Hydrate employee info
    emp_ids = [r[0] for r in per_emp_rows]
    emps = {e.id: e for e in Employee.query.filter(Employee.id.in_(emp_ids)).all()} if emp_ids else {}
    by_employee = []
    for emp_id, gross, net, supaid, count, last_pay in per_emp_rows:
        e = emps.get(emp_id)
        by_employee.append({
            'employee_id': emp_id,
            'legal_name': e.legal_name if e else None,
            'preferred_name': e.preferred_name if e else None,
            'position': e.position if e else None,
            'ytd_gross': str(gross),
            'ytd_net': str(net),
            'ytd_super_paid': str(supaid),
            'pay_event_count': int(count),
            'last_payment_date': last_pay.isoformat() if last_pay else None,
        })

    # Recent deliveries (across all employees for this user)
    limit = int(request.args.get('limit', 10) or 10)
    pe_ids_subq = pay_events_q.with_entities(PayEvent.id).subquery()
    deliveries = (PayslipDelivery.query
                  .filter(PayslipDelivery.pay_event_id.in_(db.session.query(pe_ids_subq)))
                  .order_by(PayslipDelivery.sent_at.desc())
                  .limit(limit).all())
    # Fall back to ALL deliveries if FY is empty
    if not deliveries:
        deliveries = (PayslipDelivery.query
                      .order_by(PayslipDelivery.sent_at.desc())
                      .limit(limit).all())
    emp_lookup = {e.id: e for e in Employee.query.all()}
    pe_lookup = {pe.id: pe for pe in PayEvent.query.filter(
        PayEvent.id.in_([d.pay_event_id for d in deliveries])).all()}
    recent_deliveries = []
    for d in deliveries:
        pe = pe_lookup.get(d.pay_event_id)
        emp = pe and emp_lookup.get(pe.employee_id)
        recent_deliveries.append({
            'pay_event_id': d.pay_event_id,
            'employee_name': emp.preferred_name or emp.legal_name if emp else None,
            'recipient_email': d.recipient_email,
            'sent_at': d.sent_at.isoformat() if d.sent_at else None,
            'delivery_status': d.delivery_status,
        })

    return _ok({
        'fy_year': fy_year,
        'period': {'start': fy_start.isoformat(), 'end': fy_end.isoformat()},
        'totals': {k: str(v) if isinstance(v, Decimal) else v
                   for k, v in totals.items()},
        'pay_event_counts': pay_event_counts,
        'by_employee': by_employee,
        'recent_deliveries': recent_deliveries,
    })


@payroll_api_bp.route('/summary/ytd', methods=['GET'])
@api_key_required
def summary_ytd():
    """YTD totals for one employee. ?fy_year=2026&employee_id=..."""
    user_id = request.current_user.id
    employee_id = (request.args.get('employee_id') or '').strip()
    if not validate_uuid(employee_id):
        return _err('employee_id is required (UUID)', 400)
    employee = _get_owned_employee(user_id, employee_id)
    if employee is None:
        return _err('Employee not found', 404)

    fy_year_raw = request.args.get('fy_year')
    if fy_year_raw:
        try:
            fy_year = int(fy_year_raw)
        except (TypeError, ValueError):
            return _err('fy_year must be an integer', 400)
    else:
        fy_year = _infer_fy_year()

    fy_start, fy_end = _australian_fy_bounds(fy_year)

    q = _q_pay_events_for_fy(user_id, fy_year, employee_id=employee.id)
    totals = _sum_pay_events(q)
    pay_event_counts = {s: 0 for s in PAY_EVENT_STATUSES}
    for status, n in q.with_entities(PayEvent.status, func.count(PayEvent.id)).group_by(PayEvent.status).all():
        pay_event_counts[status] = int(n)

    events = q.order_by(PayEvent.payment_date.desc()).all()
    supers = (SuperPayment.query
              .filter_by(user_id=user_id, employee_id=employee.id)
              .order_by(SuperPayment.remittance_date.desc()).all())

    return _ok({
        'fy_year': fy_year,
        'period': {'start': fy_start.isoformat(), 'end': fy_end.isoformat()},
        'employee': {'id': employee.id,
                     'legal_name': employee.legal_name,
                     'preferred_name': employee.preferred_name,
                     'position': employee.position},
        'totals': {k: str(v) if isinstance(v, Decimal) else v
                   for k, v in totals.items()},
        'pay_event_counts': pay_event_counts,
        'pay_events': [pe.to_dict() for pe in events],
        'super_payments': [sp.to_dict() for sp in supers],
    })


@payroll_api_bp.route('/summary/super-owing', methods=['GET'])
@api_key_required
def summary_super_owing():
    """Employer-wide super outstanding. The headline HMI card."""
    user_id = request.current_user.id
    fy_year_raw = request.args.get('fy_year')
    fy_year = (int(fy_year_raw) if fy_year_raw and str(fy_year_raw).isdigit()
               else _infer_fy_year())
    fy_start, fy_end = _australian_fy_bounds(fy_year)

    rows = (PayEvent.query
            .filter(PayEvent.user_id == user_id,
                    PayEvent.payment_date >= fy_start,
                    PayEvent.payment_date <= fy_end,
                    PayEvent.status != 'cancelled')
            .with_entities(
                func.coalesce(func.sum(PayEvent.super_payable_amount), 0),
                func.coalesce(func.sum(PayEvent.super_paid_amount), 0),
            ).first())

    payable = Decimal(rows[0])
    paid = Decimal(rows[1])
    outstanding = payable - paid

    per_emp = (PayEvent.query
               .filter(PayEvent.user_id == user_id,
                       PayEvent.payment_date >= fy_start,
                       PayEvent.payment_date <= fy_end,
                       PayEvent.status != 'cancelled')
               .with_entities(
                   PayEvent.employee_id,
                   func.coalesce(func.sum(PayEvent.super_payable_amount), 0),
                   func.coalesce(func.sum(PayEvent.super_paid_amount), 0),
               )
               .group_by(PayEvent.employee_id).all())
    emp_lookup = {e.id: e for e in Employee.query.all()}
    per_employee = []
    for emp_id, payable_e, paid_e in per_emp:
        e = emp_lookup.get(emp_id)
        per_employee.append({
            'employee_id': emp_id,
            'legal_name': e.legal_name if e else None,
            'preferred_name': e.preferred_name if e else None,
            'super_payable': str(payable_e),
            'super_paid': str(paid_e),
            'super_outstanding': str(Decimal(payable_e) - Decimal(paid_e)),
        })

    return _ok({
        'fy_year': fy_year,
        'period': {'start': fy_start.isoformat(), 'end': fy_end.isoformat()},
        'super_payable_total': str(payable),
        'super_paid_total': str(paid),
        'super_outstanding_total': str(outstanding),
        'per_employee': per_employee,
    })


@payroll_api_bp.route('/summary/payg-withheld', methods=['GET'])
@api_key_required
def summary_payg_withheld():
    user_id = request.current_user.id
    fy_year_raw = request.args.get('fy_year')
    fy_year = (int(fy_year_raw) if fy_year_raw and str(fy_year_raw).isdigit()
               else _infer_fy_year())
    fy_start, fy_end = _australian_fy_bounds(fy_year)

    rows = (PayEvent.query
            .filter(PayEvent.user_id == user_id,
                    PayEvent.payment_date >= fy_start,
                    PayEvent.payment_date <= fy_end,
                    PayEvent.status != 'cancelled')
            .with_entities(
                func.coalesce(func.sum(PayEvent.gross_amount), 0),
                func.coalesce(func.sum(PayEvent.payg_tax_amount), 0),
                func.coalesce(func.sum(PayEvent.net_amount), 0),
                func.count(PayEvent.id),
            ).first())

    gross = Decimal(rows[0])
    payg = Decimal(rows[1])
    net = Decimal(rows[2])
    count = int(rows[3])

    per_emp = (PayEvent.query
               .filter(PayEvent.user_id == user_id,
                       PayEvent.payment_date >= fy_start,
                       PayEvent.payment_date <= fy_end,
                       PayEvent.status != 'cancelled')
               .with_entities(
                   PayEvent.employee_id,
                   func.coalesce(func.sum(PayEvent.gross_amount), 0),
                   func.coalesce(func.sum(PayEvent.payg_tax_amount), 0),
               )
               .group_by(PayEvent.employee_id).all())
    emp_lookup = {e.id: e for e in Employee.query.all()}
    per_employee = []
    for emp_id, gross_e, payg_e in per_emp:
        e = emp_lookup.get(emp_id)
        per_employee.append({
            'employee_id': emp_id,
            'legal_name': e.legal_name if e else None,
            'preferred_name': e.preferred_name if e else None,
            'gross': str(gross_e),
            'payg_withheld': str(payg_e),
        })

    return _ok({
        'fy_year': fy_year,
        'period': {'start': fy_start.isoformat(), 'end': fy_end.isoformat()},
        'gross_total': str(gross),
        'payg_withheld_total': str(payg),
        'net_total': str(net),
        'pay_event_count': count,
        'per_employee': per_employee,
    })


@payroll_api_bp.route('/summary/recent-deliveries', methods=['GET'])
@api_key_required
def summary_recent_deliveries():
    user_id = request.current_user.id
    limit = int(request.args.get('limit', 10) or 10)
    rows = (PayslipDelivery.query
            .join(PayEvent, PayslipDelivery.pay_event_id == PayEvent.id)
            .filter(PayEvent.user_id == user_id)
            .order_by(PayslipDelivery.sent_at.desc())
            .limit(limit).all())
    emp_lookup = {e.id: e for e in Employee.query.all()}
    pe_lookup = {pe.id: pe for pe in PayEvent.query.filter(
        PayEvent.id.in_([r.pay_event_id for r in rows])).all()}
    out = []
    for d in rows:
        pe = pe_lookup.get(d.pay_event_id)
        emp = pe and emp_lookup.get(pe.employee_id)
        out.append({
            'delivery_id': d.id,
            'pay_event_id': d.pay_event_id,
            'employee_name': emp.preferred_name or emp.legal_name if emp else None,
            'recipient_email': d.recipient_email,
            'sent_at': d.sent_at.isoformat() if d.sent_at else None,
            'delivery_status': d.delivery_status,
            'subject': d.subject,
        })
    return _ok({'recent_deliveries': out, 'limit': limit})