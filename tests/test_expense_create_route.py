"""Regression tests for POST /api/expenses.

The bug these guard against
---------------------------
The create route has passed ``project_code`` / ``project_name`` /
``attachment_sha256`` to the ``Expense`` constructor since 2026-09-24 and
2026-09-25. The ORM model never grew those attributes, and no migration
ever created the columns, so SQLAlchemy raised

    TypeError: 'project_code' is an invalid keyword argument for Expense

for an unmapped kwarg -- and the endpoint answered **HTTP 500 on every
call**. The route is the documented way to add an expense: the PWA's
"add expense" button and the mail intake both POST to it, so a missing
column there takes out expense creation for the whole installation.

The invariant is structural, not a snapshot: EVERY kwarg the route passes
to the constructor must be a mapped attribute of the model. That way the
next field someone adds to the route fails the test until the model and
the database grow to match.
"""

import ast
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ENV = ROOT / '.env'
if ENV.exists():
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        os.environ.setdefault(k.strip(), v.strip())


def _route_expense_kwargs():
    """The kwargs the create route passes to Expense(...), read from the AST."""
    tree = ast.parse((ROOT / 'app' / 'api' / 'routes.py').read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == 'Expense':
            return {kw.arg for kw in node.keywords if kw.arg}
    raise AssertionError('no Expense(...) call found in app/api/routes.py')


def test_expense_model_accepts_the_fields_the_route_passes():
    """The defect, reduced to one line: the constructor must not reject them."""
    from app.models.expense import Expense
    expense = Expense(
        project_code='LD-2026-00001',
        project_name='A project',
        attachment_sha256='0' * 64,
    )
    assert expense.project_code == 'LD-2026-00001'
    assert expense.project_name == 'A project'
    assert expense.attachment_sha256 == '0' * 64


def test_every_kwarg_the_route_passes_is_a_mapped_attribute():
    """The structural invariant that stops this class of defect recurring."""
    from app.models.expense import Expense
    mapped = set(Expense.__table__.columns.keys())
    passed = _route_expense_kwargs()
    assert passed, 'found no Expense(...) kwargs -- has the route changed?'
    unmapped = passed - mapped
    assert not unmapped, (
        'the create route passes kwargs the model does not define: '
        f'{sorted(unmapped)} -- POST /api/expenses answers HTTP 500 for those'
    )


@pytest.mark.parametrize('field,column_type', [
    ('project_code', 'VARCHAR(50)'),
    ('project_name', 'VARCHAR(255)'),
    ('attachment_sha256', 'VARCHAR(64)'),
])
def test_the_migration_creates_the_column(field, column_type):
    """The migration must create the column the model maps, in the accounts schema."""
    migration = ROOT / 'schema' / 'migrations' / '2026-11-23_add_expense_project_fields.sql'
    assert migration.exists(), 'the migration file is missing'
    text = migration.read_text()
    assert 'ALTER TABLE accounts.expenses' in text, 'the migration must be schema-qualified'
    assert f'ADD COLUMN IF NOT EXISTS {field} {column_type}' in text, \
        f'the migration does not create {field} {column_type}'
