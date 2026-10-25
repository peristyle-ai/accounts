"""Which inboxes a payslip send goes to.

Added 2026-10-22. The send-payslip endpoint used to email BOTH
`email_work` and `email_personal` for every employee. the operator asked for
Sample Employee's payslips to go to her personal Gmail only, so the
recipient set is now runtime-tunable:

  * system setting ``PAYSLIP_RECIPIENT_MODES`` -- 'work', 'personal', or
    'work,personal' (default). Editable at runtime via
    ``PUT /api/settings/PAYSLIP_RECIPIENT_MODES``.
  * per-call override -- POST body ``{"recipient_kinds": ["personal"]}``.

Precedence: per-call override > system setting > default.

These are pure functions with no Flask/DB imports so they can be unit
tested in isolation.
"""

VALID_KINDS = ('work', 'personal')
DEFAULT_MODES = 'work,personal'


def parse_modes(raw):
    """Normalise a modes value to a list of valid recipient kinds.

    Accepts a comma-separated string or any iterable. Unknown kinds are
    dropped silently (a typo must not silently widen or narrow delivery
    in a way the operator did not intend -- dropping is the safe side).
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split(',')
    out = []
    for item in raw:
        kind = str(item).strip().lower()
        if kind and kind in VALID_KINDS and kind not in out:
            out.append(kind)
    return out


def resolve_targets(employee, modes):
    """Build the [(kind, email_address)] list for a payslip send.

    Only kinds listed in ``modes`` are considered, and only when the
    employee actually has that address on file.
    """
    allowed = set(parse_modes(modes))
    targets = []
    if 'work' in allowed and getattr(employee, 'email_work', None):
        targets.append(('work', employee.email_work))
    if 'personal' in allowed and getattr(employee, 'email_personal', None):
        targets.append(('personal', employee.email_personal))
    return targets
