"""Eagerly import all models so SQLAlchemy can resolve relationships at import
time (this matches the pattern used by the rest of the app — see run.py).

Phase 1b: db + get_utc_now are imported from app.db (extracted to break the
circular import with app/__init__.py). The model files below also import
from app.db.
"""
from app.db import db, get_utc_now  # Phase 1b: import from app.db (not local)

# Eagerly import all models so SQLAlchemy can resolve relationships at import
# time (this matches the pattern used by the rest of the app — see run.py).
from .user import User
# Phase 1b (schema-mode install): the User class is composed of auth
# (public.users) + profile (accounts.user_profiles) + payment settings
# (accounts.user_payment_settings). The two new tables are created in
# schema/init.sql; the models live here so SQLAlchemy can resolve the
# User.profile + User.payment_settings relationships at import time.
from .user_profile import UserProfile
from .user_payment_settings import UserPaymentSettings
from .expense import Expense
from .invoice import Invoice
from .customer import Customer
from .account_category import AccountCategory
from .activity_log import ActivityLog
from .ocr_queue import OcrQueue
from .bas_lodgement import BasLodgement
from .bank_transaction import BankTransaction, BANK_TXN_METHODS, BANK_TXN_SOURCES

# Payroll models (2026-08-27 — payroll_expansion.md).
from .employee import Employee
from .pay_event import PayEvent, PAY_EVENT_STATUSES
from .pay_event_line import PayEventLine, PAY_EVENT_LINE_TYPES
from .payslip_delivery import PayslipDelivery, PAYSLIP_DELIVERY_STATUSES, PAYSLIP_RECIPIENT_KINDS
from .super_payment import SuperPayment, SUPER_PAYMENT_STATUSES

# System-wide key-value settings (added 2026-09-17 for open-source
# release — replaces hardcoded business name / fund identifiers with
# runtime-tunable values). See app/models/system_setting.py.
from .system_setting import (
    SystemSetting,
    get_setting,
    set_setting,
    seed_defaults,
    DEFAULT_SETTINGS,
)

# Aged-receivable chasing log (2026-09-08 — tracks statements/chases
# sent to customers for overdue invoices).
from .invoice_reminder import (
    InvoiceReminder,
    INVOICE_REMINDER_TYPES,
    INVOICE_REMINDER_CHANNELS,
    INVOICE_REMINDER_DIRECTIONS,
    INVOICE_REMINDER_SENDERS,
)

# Remittance advice -> bank reconciliation log (2026-09-08 — Xero payments
# must be reconciled against the bank account before the invoice is marked paid).
from .pending_payment_reconciliation import (
    PendingPaymentReconciliation,
    PENDING_RECONCILIATION_STATUSES,
)

# Org-scoped bank destinations for outbound payments — added 2026-09-21
# to give the Wrkr Pay super clearing house a home in the schema (used by
# scripts/gen_super_aba.py to build NAB-compatible ABA files for super).
# F-05 encryption mirrors the Employee model.
from .payee_directory import Payee, PAYEE_USE_CASES


__all__ = [
    'db', 'get_utc_now',
    'User', 'UserProfile', 'UserPaymentSettings', 'Expense', 'Invoice', 'Customer', 'AccountCategory',
    'ActivityLog', 'OcrQueue', 'BasLodgement',
    'BankTransaction', 'BANK_TXN_METHODS', 'BANK_TXN_SOURCES',
    'Employee', 'PayEvent', 'PayEventLine', 'PayslipDelivery', 'SuperPayment',
    'PAY_EVENT_STATUSES', 'PAY_EVENT_LINE_TYPES',
    'PAYSLIP_DELIVERY_STATUSES', 'PAYSLIP_RECIPIENT_KINDS',
    'SUPER_PAYMENT_STATUSES',
    'InvoiceReminder',
    'INVOICE_REMINDER_TYPES',
    'INVOICE_REMINDER_CHANNELS',
    'INVOICE_REMINDER_DIRECTIONS',
    'INVOICE_REMINDER_SENDERS',
    'PendingPaymentReconciliation',
    'PENDING_RECONCILIATION_STATUSES',
    'Payee',
    'PAYEE_USE_CASES',
]
