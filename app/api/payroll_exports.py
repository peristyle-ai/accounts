"""Payroll export endpoints.

Two file formats are produced, both filtered by pay-event status and date
range:

  GET /api/exports/payroll/aba       — NAB-compatible ABA file (120-char
                                       fixed-width records, CRLF-terminated)
  GET /api/exports/payroll/super-csv — AustralianSuper contribution CSV
                                       (UTF-8 BOM, header row)

  GET /api/exports/payroll/preview   — JSON summary (counts + totals) so
                                       users can sanity-check the date
                                       range before downloading

By default both export endpoints filter on `status='finalized'` — the
"pending payroll" the operator asked for ("raised but before you have marked
them as paid"). Pass `?status=draft,finalized,paid` to override.

Both download endpoints log the export as an `export_payroll_*` activity
log entry with `source='api'` so we can audit later.

Date range: `from_date` and `to_date` filter on `payment_date`. If
omitted, defaults to "all finalized pay events".

History: added 2026-09-17 per the operator's request.
"""

import io
from datetime import date, datetime
from decimal import Decimal

from flask import Blueprint, request, jsonify, send_file

from app.models import db
from app.models.employee import Employee
from app.models.pay_event import PayEvent
from app.models.super_payment import SuperPayment
from app.models.activity_log import ActivityLog
from app.models.system_setting import get_setting as _get_setting
from app.shared.aba import build_aba_file, lodgement_reference
from app.shared.super_csv import build_super_csv
from app.shared.decorators import api_key_required


payroll_exports_bp = Blueprint('payroll_exports', __name__, url_prefix='/api/exports/payroll')


DEFAULT_STATUS = 'finalized'


def _parse_date_param(name):
    """Optional ISO date query param. None if missing, 400 if malformed."""
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        from flask import abort
        abort(400, description=f'{name} must be YYYY-MM-DD')


def _parse_statuses():
    """Parse the ?status= query param into a tuple of allowed values.
    Defaults to DEFAULT_STATUS ('finalized')."""
    raw = request.args.get('status', DEFAULT_STATUS)
    parts = [p.strip() for p in raw.split(',') if p.strip()]
    allowed = ('draft', 'finalized', 'paid', 'cancelled')
    for p in parts:
        if p not in allowed:
            from flask import abort
            abort(400, description=f'invalid status: {p!r}')
    return tuple(parts)


def _query_pay_events(user_id, statuses, from_date, to_date):
    """Build the query for pay events to include in an export.

    Filters by user, statuses, and payment_date range. Sorts by payment
    date then by employee name so the output is deterministic across
    requests (important for ABA files — banks prefer stable ordering).
    """
    q = (
        PayEvent.query
        .filter(
            PayEvent.user_id == user_id,
            PayEvent.status.in_(statuses),
        )
    )
    if from_date:
        q = q.filter(PayEvent.payment_date >= from_date)
    if to_date:
        q = q.filter(PayEvent.payment_date <= to_date)
    # Join employee so we can sort by name
    q = q.join(Employee, PayEvent.employee_id == Employee.id)
    return q.order_by(PayEvent.payment_date.asc(), Employee.legal_name.asc())


def _covered_pay_event_ids(user_id):
    """Return the set of pay event IDs already covered by ANY SuperPayment
    for this user (regardless of remittance status).

    Used by the Super CSV export to avoid double-uploading a contribution
    that already has a SuperPayment row (even pending). If we don't filter
    here, an accountant could accidentally pay the same super twice.

    The ABA export intentionally does NOT use this filter — re-running the
    bank file is sometimes needed (e.g. to fix a typo). The bank upload
    is a one-time op per cycle; super contributions are not.
    """
    covered = set()
    for sp in SuperPayment.query.filter_by(user_id=user_id).all():
        for pid in (sp.pay_event_ids or []):
            covered.add(pid)
    return covered


def _log_export(user_id, kind, period_label, counts, source, filter_params):
    """Persist an export activity row for audit."""
    db.session.add(ActivityLog(
        user_id=user_id,
        action=f'export_payroll_{kind}',
        table_name='pay_events',
        record_id=period_label,
        new_values={
            **counts,
            'source': source,
            **filter_params,
        },
    ))
    db.session.commit()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@payroll_exports_bp.route('/preview', methods=['GET'])
@api_key_required
def preview():
    """JSON summary of what the export endpoints would produce."""
    user = request.current_user
    statuses = _parse_statuses()
    from_d = _parse_date_param('from_date')
    to_d = _parse_date_param('to_date')

    events = _query_pay_events(user.id, statuses, from_d, to_d).all()

    # Same filter as the Super CSV export: exclude events already covered
    # by any SuperPayment (so the preview matches what would actually be
    # downloadable).
    covered = _covered_pay_event_ids(user.id)
    events = [e for e in events if e.id not in covered]

    employees = {e.id: e for e in Employee.query.all()}
    total_net = sum((Decimal(str(ev.net_amount)) for ev in events), Decimal('0'))
    total_gross = sum((Decimal(str(ev.gross_amount)) for ev in events), Decimal('0'))
    total_super = sum((Decimal(str(ev.super_payable_amount)) for ev in events), Decimal('0'))

    return jsonify({
        'statuses': list(statuses),
        'from_date': from_d.isoformat() if from_d else None,
        'to_date': to_d.isoformat() if to_d else None,
        'pay_event_count': len(events),
        'distinct_employees': len({ev.employee_id for ev in events}),
        'total_net': str(total_net.quantize(Decimal('0.01'))),
        'total_gross': str(total_gross.quantize(Decimal('0.01'))),
        'total_super_payable': str(total_super.quantize(Decimal('0.01'))),
    }), 200


@payroll_exports_bp.route('/aba', methods=['GET'])
@api_key_required
def export_aba():
    """Return the ABA file for the matching pay events.

    Query params:
      status       (csv)   default 'finalized'
      from_date    (str)   optional YYYY-MM-DD
      to_date      (str)   optional YYYY-MM-DD
      process_date (str)   optional YYYY-MM-DD; date the bank should release
                            the payments. Defaults to today.

    Returns: application/octet-stream with .aba extension.
    """
    user = request.current_user

    # Pre-flight: payer must have bank details configured
    if not user.bsb_plain or not user.account_number_plain:
        return jsonify({
            'error': 'Business bank details (BSB + account number) are not configured. '
                     'Update via PUT /api/auth/business.',
        }), 400

    statuses = _parse_statuses()
    from_d = _parse_date_param('from_date')
    to_d = _parse_date_param('to_date')

    # Processing date — when the bank should release the payments. Defaults
    # to today, but callers usually want a future date (NAB typically needs
    # 1+ business day lead time).
    process_str = request.args.get('process_date')
    if process_str:
        try:
            process_date = date.fromisoformat(process_str)
        except (ValueError, TypeError):
            return jsonify({'error': 'process_date must be YYYY-MM-DD'}), 400
    else:
        process_date = date.today()

    events = _query_pay_events(user.id, statuses, from_d, to_d).all()
    if not events:
        return jsonify({
            'error': f'No pay events match status={list(statuses)} '
                     f'from_date={from_d} to_date={to_d}. Nothing to export.',
        }), 404

    # Build the payment rows. We need plaintext bank details for the file.
    payments = []
    skipped = []
    for ev in events:
        emp = Employee.query.get(ev.employee_id)
        if not emp:
            skipped.append({'event_id': ev.id, 'reason': 'employee not found'})
            continue
        if not emp.bank_bsb_plain or not emp.bank_account_number_plain:
            skipped.append({
                'event_id': ev.id,
                'reason': f'employee {emp.legal_name} has no bank details',
            })
            continue
        # Lodgement reference = the reference the payee sees on their
        # statement. Uses the pay event's own bank_reference so an
        # ABA-uploaded payment reconciles with a manual transfer.
        ref = lodgement_reference(ev, emp)

        payments.append({
            'bsb': emp.bank_bsb_plain,
            'account_number': emp.bank_account_number_plain,
            'account_name': emp.bank_account_name or emp.legal_name,
            'amount': ev.net_amount,
            'lodgement_ref': ref,
        })

    if not payments:
        return jsonify({
            'error': 'No employees in the matching pay events have bank details configured.',
            'skipped': skipped,
        }), 400

    aba_bytes = build_aba_file(user, payments, processing_date=process_date)

    period_label = (
        f'{from_d.isoformat()}_to_{to_d.isoformat()}' if from_d or to_d
        else 'all'
    )

    counts = {
        'pay_event_count': len(events),
        'payment_count': len(payments),
        'skipped': len(skipped),
        'total_amount_cents': sum(
            int((Decimal(str(p['amount'])) * 100).quantize(Decimal('1')))
            for p in payments
        ),
    }
    _log_export(
        user.id, 'aba', period_label, counts,
        source='api',
        filter_params={
            'statuses': list(statuses),
            'process_date': process_date.isoformat(),
        },
    )

    filename = f'peristyle-payroll-{period_label}.aba'
    return send_file(
        io.BytesIO(aba_bytes),
        mimetype='application/octet-stream',
        as_attachment=True,
        download_name=filename,
    )


@payroll_exports_bp.route('/super-csv', methods=['GET'])
@api_key_required
def export_super_csv():
    """Return an AustralianSuper-compatible CSV of pending super contributions.

    Query params: same as /aba.
    """
    user = request.current_user
    statuses = _parse_statuses()
    from_d = _parse_date_param('from_date')
    to_d = _parse_date_param('to_date')

    events = _query_pay_events(user.id, statuses, from_d, to_d).all()
    if not events:
        return jsonify({
            'error': f'No pay events match status={list(statuses)} '
                     f'from_date={from_d} to_date={to_d}. Nothing to export.',
        }), 404

    # Exclude pay events already covered by any SuperPayment (pending or
    # paid). Otherwise the same contribution would appear in every export
    # and risk being uploaded twice to AustralianSuper.
    covered = _covered_pay_event_ids(user.id)
    events = [e for e in events if e.id not in covered]

    if not events:
        return jsonify({
            'error': 'All matching pay events already have a SuperPayment '
                     'on file. Nothing new to export.',
        }), 404

    rows = []
    skipped = []
    for ev in events:
        emp = Employee.query.get(ev.employee_id)
        if not emp:
            skipped.append({'event_id': ev.id, 'reason': 'employee not found'})
            continue
        if not emp.super_fund_member_no:
            skipped.append({
                'event_id': ev.id,
                'reason': f'employee {emp.legal_name} has no super member number',
            })
            continue

        rows.append({
            'member_number': emp.super_fund_member_no,
            'legal_name': emp.legal_name,
            'preferred_name': emp.preferred_name,
            # TFN + DOB pulled from the encrypted Employee columns. By the
            # time they reach the SAFF builder they're plaintext (TFN = 9
            # digits, DOB = ISO date). NEVER log or echo these in HTTP
            # responses — the *_plain accessors are deliberately not in the
            # standard to_dict() output. (DOB is encrypted at rest via the
            # same Fernet pattern as TFN.)
            'tfn': emp.tfn_plain,
            'date_of_birth': emp.date_of_birth_plain,
            # Address + sex (added 2026-09-17 for AusSuper SAFF/Mapped).
            # Plaintext fields — low-sensitivity PII, matches Customer/User.
            'sex': emp.sex,
            'address_line1': emp.address_line1,
            'city': emp.city,
            'state': emp.state,
            'postcode': emp.postcode,
            'pay_period_start': ev.pay_period_start,
            'pay_period_end': ev.pay_period_end,
            'payment_date': ev.payment_date,
            'ote_amount': ev.super_ote_amount,
            'sgc_amount': ev.super_payable_amount,
            'salary_sacrifice': '0',
            'fund_name': emp.super_fund_name or _get_setting('DEFAULT_FUND_NAME'),
        })

    if not rows:
        return jsonify({
            'error': 'No employees in the matching pay events have super fund details configured.',
            'skipped': skipped,
        }), 400

    csv_text = build_super_csv(rows)
    period_label = (
        f'{from_d.isoformat()}_to_{to_d.isoformat()}' if from_d or to_d
        else 'all'
    )

    counts = {
        'pay_event_count': len(events),
        'contribution_row_count': len(rows),
        'skipped': len(skipped),
        'total_super_cents': sum(
            int((Decimal(str(r['sgc_amount'])) * 100).quantize(Decimal('1')))
            for r in rows
        ),
    }
    _log_export(
        user.id, 'super-csv', period_label, counts,
        source='api',
        filter_params={'statuses': list(statuses)},
    )

    filename = f'peristyle-super-{period_label}.csv'
    return send_file(
        io.BytesIO(csv_text.encode('utf-8')),
        mimetype='text/csv; charset=utf-8',
        as_attachment=True,
        download_name=filename,
    )
