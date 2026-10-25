"""
API endpoints for BAS (Business Activity Statement) lodgements.

POST /api/bas-lodgements           — record a new lodgement
GET  /api/bas-lodgements           — list lodgements (filter by FY/quarter)
GET  /api/bas-lodgements/<id>      — get one
PUT  /api/bas-lodgements/<id>      — update notes/adjustments
DELETE /api/bas-lodgements/<id>    — delete (admin only)

The model is defined in app.models.bas_lodgement.BasLodgement.
The actual quarterly BAS COMPUTATION (income/expenses/GST) is at
GET /api/reports/quarterly-bas — this module just persists the
FACT of lodgement with the ATO receipt ID.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, request, jsonify
from sqlalchemy import and_

from app.models import db, get_utc_now
from app.models.bas_lodgement import BasLodgement
from app.models.user import User
from app.shared.decorators import api_key_required, log_activity
from app.shared.roles import role_required

bas_bp = Blueprint('bas', __name__, url_prefix='/api/bas-lodgements')


def _parse_decimal(value, field_name):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        from flask import abort
        abort(400, description=f'{field_name} is not a valid number')


def _parse_date(value, field_name):
    if not value:
        from flask import abort
        abort(400, description=f'{field_name} is required')
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        from flask import abort
        abort(400, description=f'{field_name} must be YYYY-MM-DD')


def _parse_datetime(value, field_name):
    if not value:
        from flask import abort
        abort(400, description=f'{field_name} is required')
    try:
        # Accept ISO 8601 with or without timezone
        s = str(value).replace('Z', '+00:00')
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        from flask import abort
        abort(400, description=f'{field_name} must be ISO 8601 datetime')


@bas_bp.route('', methods=['POST'])
@api_key_required
def create_lodgement():
    """Record a new BAS lodgement."""
    user = request.current_user
    data = request.get_json() or {}

    financial_year = data.get('financial_year', '').strip()
    quarter = data.get('quarter')
    if not financial_year:
        return jsonify({'error': 'financial_year is required (e.g. FY2025/26)'}), 400
    if quarter is None:
        return jsonify({'error': 'quarter is required (1-4)'}), 400
    quarter = int(quarter)
    if quarter < 1 or quarter > 4:
        return jsonify({'error': 'quarter must be 1-4'}), 400

    period_start = _parse_date(data.get('period_start'), 'period_start')
    period_end = _parse_date(data.get('period_end'), 'period_end')

    lodged_at = _parse_datetime(data.get('lodged_at'), 'lodged_at')
    if lodged_at.tzinfo is None:
        lodged_at = lodged_at.replace(tzinfo=timezone.utc)

    final_amount = _parse_decimal(data.get('final_amount'), 'final_amount')
    if final_amount is None:
        return jsonify({'error': 'final_amount is required'}), 400

    final_amount_type = data.get('final_amount_type', '').strip()
    if final_amount_type not in ('credit', 'owe', 'zero'):
        return jsonify({'error': "final_amount_type must be 'credit', 'owe', or 'zero'"}), 400

    # Sanity-check type matches amount sign
    if final_amount_type == 'credit' and final_amount <= 0:
        return jsonify({'error': 'final_amount_type=credit requires final_amount > 0'}), 400
    if final_amount_type == 'owe' and final_amount >= 0:
        return jsonify({'error': 'final_amount_type=owe requires final_amount < 0 (use negative)'}), 400

    # Optional snapshot fields
    gst_collected = _parse_decimal(data.get('gst_collected'), 'gst_collected')
    gst_paid = _parse_decimal(data.get('gst_paid'), 'gst_paid')
    computed_net_gst = data.get('computed_net_gst')
    if computed_net_gst is None and gst_collected is not None and gst_paid is not None:
        computed_net_gst = gst_collected - gst_paid
    computed_net_gst = _parse_decimal(computed_net_gst, 'computed_net_gst')

    prior_credit_carried = _parse_decimal(data.get('prior_credit_carried', 0), 'prior_credit_carried') or Decimal('0')
    other_adjustments = _parse_decimal(data.get('other_adjustments', 0), 'other_adjustments') or Decimal('0')

    # Check uniqueness: one lodgement per (user, FY, quarter)
    existing = BasLodgement.query.filter_by(
        user_id=user.id, financial_year=financial_year, quarter=quarter
    ).first()
    if existing:
        return jsonify({
            'error': f'BAS lodgement for {financial_year} Q{quarter} already exists',
            'existing_id': existing.id
        }), 409

    lodgement = BasLodgement(
        user_id=user.id,
        financial_year=financial_year,
        quarter=quarter,
        period_start=period_start,
        period_end=period_end,
        ato_receipt_id=data.get('ato_receipt_id'),
        ato_account_name=data.get('ato_account_name'),
        lodged_at=lodged_at,
        lodgement_method=data.get('lodgement_method', 'online'),
        gst_collected=gst_collected,
        gst_paid=gst_paid,
        computed_net_gst=computed_net_gst,
        final_amount=final_amount,
        final_amount_type=final_amount_type,
        prior_credit_carried=prior_credit_carried,
        other_adjustments=other_adjustments,
        adjustments_note=data.get('adjustments_note'),
        notes=data.get('notes'),
        screenshot_path=data.get('screenshot_path'),
    )
    db.session.add(lodgement)
    db.session.commit()

    return jsonify({'lodgement': lodgement.to_dict()}), 201


@bas_bp.route('', methods=['GET'])
@api_key_required
def list_lodgements():
    """List BAS lodgements, optionally filtered by FY/quarter."""
    user = request.current_user
    q = BasLodgement.query.filter_by(user_id=user.id)

    fy = request.args.get('financial_year')
    if fy:
        q = q.filter_by(financial_year=fy)
    quarter = request.args.get('quarter', type=int)
    if quarter:
        q = q.filter_by(quarter=quarter)

    lodgements = q.order_by(BasLodgement.period_end.desc()).all()
    return jsonify({
        'count': len(lodgements),
        'lodgements': [l.to_dict() for l in lodgements],
    }), 200


@bas_bp.route('/<lodgement_id>', methods=['GET'])
@api_key_required
def get_lodgement(lodgement_id):
    user = request.current_user
    lodgement = BasLodgement.query.filter_by(id=lodgement_id, user_id=user.id).first()
    if not lodgement:
        return jsonify({'error': 'Lodgement not found'}), 404
    return jsonify({'lodgement': lodgement.to_dict()}), 200


@bas_bp.route('/<lodgement_id>', methods=['PUT'])
@api_key_required
def update_lodgement(lodgement_id):
    """Update notes / adjustments / screenshot — fields that are SAFE to edit
    after lodgement. ATO receipt ID, period dates, and amounts are locked
    once lodged (set them wrong by deleting + recreating)."""
    user = request.current_user
    lodgement = BasLodgement.query.filter_by(id=lodgement_id, user_id=user.id).first()
    if not lodgement:
        return jsonify({'error': 'Lodgement not found'}), 404

    data = request.get_json() or {}
    if 'adjustments_note' in data:
        lodgement.adjustments_note = data['adjustments_note']
    if 'notes' in data:
        lodgement.notes = data['notes']
    if 'screenshot_path' in data:
        lodgement.screenshot_path = data['screenshot_path']
    if 'prior_credit_carried' in data:
        lodgement.prior_credit_carried = _parse_decimal(data['prior_credit_carried'], 'prior_credit_carried') or Decimal('0')
    if 'other_adjustments' in data:
        lodgement.other_adjustments = _parse_decimal(data['other_adjustments'], 'other_adjustments') or Decimal('0')

    db.session.commit()
    return jsonify({'lodgement': lodgement.to_dict()}), 200


@bas_bp.route('/<lodgement_id>', methods=['DELETE'])
@api_key_required
@role_required('admin')
def delete_lodgement(lodgement_id):
    """Delete a BAS lodgement. Use only if the lodgement was recorded by
    mistake (e.g. wrong receipt ID) — the actual ATO lodgement on ato.gov.au
    is unaffected."""
    user = request.current_user
    lodgement = BasLodgement.query.filter_by(id=lodgement_id, user_id=user.id).first()
    if not lodgement:
        return jsonify({'error': 'Lodgement not found'}), 404

    db.session.delete(lodgement)
    db.session.commit()
    return jsonify({'deleted': lodgement_id}), 200