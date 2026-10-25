"""API endpoints for managing system_settings table values.

GET  /api/settings                 — list all key/value pairs
GET  /api/settings/<key>          — fetch a single setting (404 if absent)
PUT  /api/settings/<key>          — set a value (insert or update)
DELETE /api/settings/<key>        — delete a setting (revert to env-var / default fallback)

Auth: @api_key_required (accepts both X-API-Key header and HMI session cookie).

Future-friendly: returns values as plain strings. Add JSON validation when
a setting becomes structured (e.g. nested objects); for now everything is
text.
"""

from flask import Blueprint, jsonify, request

from app.models import db
from app.models.system_setting import (
    SystemSetting,
    get_setting,
    set_setting,
)
from app.shared.decorators import api_key_required
from app.shared.roles import role_required


settings_bp = Blueprint('settings', __name__, url_prefix='/api/settings')


@settings_bp.route('', methods=['GET'])
@api_key_required
def list_settings():
    """Return all current settings (DB rows only — env-var fallbacks
    aren't included here since the HMI/admin UI is the source of truth
    for the visible state)."""
    rows = SystemSetting.query.order_by(SystemSetting.key).all()
    return jsonify({
        'settings': {row.key: row.value for row in rows},
    }), 200


@settings_bp.route('/<key>', methods=['GET'])
@api_key_required
def get_setting_route(key):
    """Fetch a single setting. Looks up via the same precedence as
    `get_setting()` — DB first, then env var, then caller default."""
    # Pass through get_setting with a sentinel so we can distinguish
    # 'value is null' (caller didn't set it) from 'setting is genuinely
    # unset' (resolved default was also null). We use None as both for
    # our actual settings, but a 404 here signals "not configured".
    value = get_setting(key)
    if value is None:
        return jsonify({'error': f'Setting {key!r} is not configured'}), 404
    return jsonify({'key': key, 'value': value}), 200


@settings_bp.route('/<key>', methods=['PUT'])
@api_key_required
@role_required('admin')
def set_setting_route(key):
    """Insert or update a setting. Body: {"value": "..."}.

    Validates the key name lightly (max 100 chars, alphanumeric + underscore
    + dot + dash) so we don't accept arbitrary keys that would pollute the
    namespace. Empty value deletes the row (so admin can clear an override
    and fall back to the env var or DEFAULT_SETTINGS seed).
    """
    data = request.get_json() or {}
    value = data.get('value')
    if value is None:
        return jsonify({'error': 'Request body must include "value"'}), 400

    # Light validation: 100-char key, restricted character set
    if len(key) > 100:
        return jsonify({'error': 'Setting key must be ≤100 characters'}), 400
    import re
    if not re.match(r'^[A-Za-z0-9._-]+$', key):
        return jsonify({'error': 'Setting key must contain only letters, digits, dot, underscore, or dash'}), 400

    # Empty string → delete (revert to fallback). Otherwise set.
    if value == '':
        row = SystemSetting.query.filter_by(key=key).first()
        if row is not None:
            db.session.delete(row)
            db.session.commit()
        return jsonify({'key': key, 'value': '', 'deleted': True}), 200

    set_setting(key, str(value))
    return jsonify({'key': key, 'value': str(value), 'updated': True}), 200


@settings_bp.route('/<key>', methods=['DELETE'])
@api_key_required
@role_required('admin')
def delete_setting_route(key):
    """Delete a setting (revert to env-var / DEFAULT_SETTINGS fallback)."""
    row = SystemSetting.query.filter_by(key=key).first()
    if row is None:
        return jsonify({'error': f'Setting {key!r} is not configured'}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({'key': key, 'deleted': True}), 200
