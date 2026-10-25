"""Single Touch Payroll (STP) Phase 1 export endpoint.

Destination: https://portal.singletouch.com.au  (ATO payroll portal)

  GET /api/exports/payroll/one-touch-payroll

Query params:
  from_date    (str)  optional YYYY-MM-DD -- filter on pay_event.payment_date
  to_date      (str)  optional YYYY-MM-DD -- filter on pay_event.payment_date
  employee_id  (str)  optional employee UUID
  status       (csv)  default 'paid,finalized' (drafts are excluded)

One CSV per pay run
-------------------
The portal validates each uploaded file on its own and rejects a file
whose "Pay or update date" differs between records:

    Inconsistent values found for 'Pay or update date'. This value must
    be the same for every record.

A pay event submission is therefore scoped to ONE pay run. This endpoint
groups the matching pay events by payment_date and emits one CSV per
group. When the requested range covers more than one pay run the files
are returned as a ZIP (one CSV per run, named by its pay run date); a
range that covers exactly one pay run returns that CSV directly.

The file format itself (93 columns, CRLF, no UTF-8 BOM, `D/MM/YYYY`
dates) is defined by `app.shared.stp_csv`.
"""

import io
import zipfile
from datetime import date
from decimal import Decimal

from flask import Blueprint, request, jsonify, send_file

from app.models import db
from app.models.employee import Employee
from app.models.pay_event import PayEvent
from app.models.activity_log import ActivityLog
from app.shared.decorators import api_key_required
from app.shared.stp_csv import (
    build_stp_csvs_by_payrun,
    build_stp_row,
    load_employer_from_config,
    payrun_consistency_errors,
)


stp_exports_bp = Blueprint('stp_exports', __name__, url_prefix='/api/exports/payroll')

DEFAULT_STATUS = 'paid,finalized'


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
    """Parse the ?status= query param. Defaults to paid + finalized."""
    raw = request.args.get('status', DEFAULT_STATUS)
    parts = [p.strip() for p in raw.split(',') if p.strip()]
    allowed = ('draft', 'finalized', 'paid', 'cancelled')
    for p in parts:
        if p not in allowed:
            from flask import abort
            abort(400, description=f'invalid status: {p!r}')
    return tuple(parts) or ('paid', 'finalized')


def _query_pay_events(user_id, statuses, from_date, to_date, employee_id):
    """Pay events in range, ordered by pay run date then employee name."""
    q = PayEvent.query.filter(
        PayEvent.user_id == user_id,
        PayEvent.status.in_(statuses),
    )
    if from_date:
        q = q.filter(PayEvent.payment_date >= from_date)
    if to_date:
        q = q.filter(PayEvent.payment_date <= to_date)
    if employee_id:
        q = q.filter(PayEvent.employee_id == employee_id)
    q = q.join(Employee, PayEvent.employee_id == Employee.id)
    return q.order_by(PayEvent.payment_date.asc(), Employee.legal_name.asc())


def _build_rows(events):
    """Map pay events + employees to STP row dicts. Skips unusable ones."""
    rows = []
    skipped = []
    employer = load_employer_from_config()
    for ev in events:
        emp = db.session.get(Employee, ev.employee_id)
        if not emp:
            skipped.append({'event_id': ev.id, 'reason': 'employee not found'})
            continue
        if not emp.tfn_plain:
            skipped.append({
                'event_id': ev.id,
                'reason': f'employee {emp.legal_name} has no TFN on file',
            })
            continue
        rows.append(build_stp_row(ev, emp, employer))
    return rows, skipped, employer


@stp_exports_bp.route('/one-touch-payroll', methods=['GET'])
@api_key_required
def export_one_touch_payroll():
    """Return the STP Phase 1 CSV(s) for the matching pay events.

    One CSV per pay run. A single-pay-run range returns that CSV as
    `text/csv`; a multi-run range returns a ZIP containing one CSV per
    pay run.
    """
    user = request.current_user
    statuses = _parse_statuses()
    from_d = _parse_date_param('from_date')
    to_d = _parse_date_param('to_date')
    employee_id = request.args.get('employee_id') or None

    events = _query_pay_events(user.id, statuses, from_d, to_d, employee_id).all()
    if not events:
        return jsonify({
            'error': f'No pay events match status={list(statuses)} '
                     f'from_date={from_d} to_date={to_d} '
                     f'employee_id={employee_id}. Nothing to export.',
        }), 404

    rows, skipped, employer = _build_rows(events)
    if not rows:
        return jsonify({
            'error': 'No matching pay events have the employee details '
                     'the STP format requires (TFN, name).',
            'skipped': skipped,
        }), 400

    files = build_stp_csvs_by_payrun(rows, employer)

    # Guard against shipping a file the portal will reject. Every
    # file-level column must be identical across all records in a file.
    validation_errors = []
    for f in files:
        for err in payrun_consistency_errors(f['csv']):
            validation_errors.append({
                'payrun_date': f['payrun_date'].isoformat() if f['payrun_date'] else None,
                'error': err,
            })
    if validation_errors:
        return jsonify({
            'error': 'The generated file would be rejected by the portal '
                     '(file-level values must be identical across every record).',
            'validation_errors': validation_errors,
        }), 500

    period_label = (
        f'{from_d.isoformat()}_to_{to_d.isoformat()}' if from_d or to_d
        else 'all'
    )
    counts = {
        'pay_event_count': len(events),
        'row_count': len(rows),
        'payrun_count': len(files),
        'skipped': len(skipped),
        'total_gross_cents': sum(
            int((Decimal(str(r.get('employee_gross_pay') or 0)) * 100).quantize(Decimal('1')))
            for r in rows
        ),
    }
    db.session.add(ActivityLog(
        user_id=user.id,
        action='export_payroll_one-touch-payroll',
        table_name='pay_events',
        record_id=period_label,
        new_values={
            **counts,
            'source': 'api',
            'statuses': list(statuses),
            'payrun_dates': [
                f['payrun_date'].isoformat() if f['payrun_date'] else None
                for f in files
            ],
        },
    ))
    db.session.commit()

    def _filename(f):
        d = f['payrun_date']
        return f'stp-payrun-{d.isoformat()}.csv' if d else 'stp-payrun-unknown.csv'

    # Single pay run -> the CSV itself.
    if len(files) == 1:
        f = files[0]
        return send_file(
            io.BytesIO(f['csv'].encode('utf-8')),
            # Flask appends the charset itself for text/* mimetypes --
            # passing it here yields 'text/csv; charset=utf-8; charset=utf-8'.
            mimetype='text/csv',
            as_attachment=True,
            download_name=_filename(f),
        )

    # Multiple pay runs -> a ZIP with one CSV per run.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.writestr(_filename(f), f['csv'].encode('utf-8'))
    buf.seek(0)
    return send_file(
        buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'stp-payruns-{period_label}.zip',
    )
