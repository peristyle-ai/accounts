from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, send_file
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash
from datetime import date
from decimal import Decimal
import os
import uuid
import io

from app.models import db
from app.models.user import User
from app.models.expense import Expense
from app.models.invoice import Invoice
from app.models.account_category import AccountCategory
from app.models.activity_log import ActivityLog
from app.shared.auth import verify_credentials
from app.shared.rate_limiter import rate_limit_hmi, record_attempt, _get_client_ip

hmi_bp = Blueprint('hmi', __name__)


# =====================================================================
# Receipt URL helper (2026-10-26)
# ---------------------------------------------------------------------
# J asked for "Open the original receipt": a View-receipt button in the
# expenses list and one on the edit-expense form.
#
# The blocker: `attachment_path` is stored in TWO different shapes.
#   - absolute: "/home/enp/py_pg_accounts/uploads/expenses/<name>.jpg"
#     (UPLOAD_FOLDER is absolute in ~/py_pg_accounts/.env — every row
#      created via the API lands here; 63/63 rows on 2026-10-26)
#   - relative: "uploads/expenses/<name>.jpg"  (HMI form upload)
#
# The edit form used to do `attachment_path.replace('uploads/', '')`,
# which mangles the absolute form into
# "/home/enp/py_pg_accounts/expenses/<name>.jpg" -> /uploads//home/... -> 404.
# So every receipt preview was dead. serve_upload() serves from
# os.path.abspath(UPLOAD_FOLDER), so what we need is the path RELATIVE
# to that root: "expenses/<name>.jpg".
# =====================================================================

def _receipt_relative_path(attachment_path):
    """Return `attachment_path` relative to UPLOAD_FOLDER, or None."""
    if not attachment_path:
        return None
    p = str(attachment_path)
    upload_root = os.path.abspath(current_app.config.get('UPLOAD_FOLDER', 'uploads'))

    # Absolute path under the upload root -> relpath handles it cleanly.
    if os.path.isabs(p):
        try:
            rel = os.path.relpath(p, upload_root)
            if not rel.startswith('..'):
                return rel
        except ValueError:
            pass

    # Relative "uploads/expenses/x.jpg" -> strip the leading "uploads/".
    marker = 'uploads/'
    idx = p.find(marker)
    if idx >= 0:
        return p[idx + len(marker):]

    return os.path.basename(p)


def receipt_url(attachment_path):
    """URL for the stored receipt, or None when there is nothing to show.

    Returns None (rather than a link that 404s) when the row has no
    attachment or the file is no longer on disk.
    """
    rel = _receipt_relative_path(attachment_path)
    if not rel:
        return None
    upload_root = os.path.abspath(current_app.config.get('UPLOAD_FOLDER', 'uploads'))
    if not os.path.exists(os.path.join(upload_root, rel)):
        return None
    return url_for('serve_upload', filename=rel)


def receipt_filename(attachment_path):
    """Display name for the stored receipt (filename only, no paths)."""
    if not attachment_path:
        return None
    return os.path.basename(str(attachment_path))


@hmi_bp.app_context_processor
def inject_receipt_helpers():
    """Make receipt_url() / receipt_filename() available in every template."""
    return {
        'receipt_url': receipt_url,
        'receipt_filename': receipt_filename,
    }


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('hmi.login'))
        return f(*args, **kwargs)
    return decorated_function


@hmi_bp.route('/login', methods=['GET', 'POST'])
@rate_limit_hmi('login.html')
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not email or not password:
            flash('Email and password are required', 'error')
            return render_template('login.html')

        client_ip = _get_client_ip()
        user = User.query.filter_by(email=email).first()

        if not verify_credentials(user, password) or not user.is_active:
            record_attempt(client_ip, success=False, account=email)
            flash('Invalid email or password', 'error')
            return render_template('login.html')

        record_attempt(client_ip, success=True, account=email)
        session['user_id'] = user.id
        session['user_email'] = user.email
        flash('Logged in successfully', 'success')
        return redirect(url_for('hmi.dashboard'))

    return render_template('login.html')


@hmi_bp.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully', 'success')
    return redirect(url_for('hmi.login'))


@hmi_bp.route('/')
@login_required
def dashboard():
    user_id = session['user_id']

    current_date = date.today()
    if current_date.month >= 7:
        fy_year = current_date.year
    else:
        fy_year = current_date.year - 1

    from datetime import date as date_class
    from calendar import monthrange

    def get_quarter_dates(year, quarter):
        if quarter == 1:
            return date_class(year, 7, 1), date_class(year, 9, 30)
        elif quarter == 2:
            return date_class(year, 10, 1), date_class(year, 12, 31)
        elif quarter == 3:
            return date_class(year + 1, 1, 1), date_class(year + 1, 3, 31)
        else:
            return date_class(year + 1, 4, 1), date_class(year + 1, 6, 30)

    if current_date.month <= 3:
        current_quarter = 3
    elif current_date.month <= 6:
        current_quarter = 4
    elif current_date.month <= 9:
        current_quarter = 1
    else:
        current_quarter = 2

    q_start, q_end = get_quarter_dates(fy_year, current_quarter)

    recent_expenses = Expense.query.filter_by(user_id=user_id).order_by(Expense.expense_date.desc()).limit(5).all()
    recent_invoices = Invoice.query.filter_by(user_id=user_id).order_by(Invoice.invoice_date.desc()).limit(5).all()

    q_expenses = Expense.query.filter(
        Expense.user_id == user_id,
        Expense.expense_date >= q_start,
        Expense.expense_date <= q_end
    ).all()
    q_invoices = Invoice.query.filter(
        Invoice.user_id == user_id,
        Invoice.invoice_date >= q_start,
        Invoice.invoice_date <= q_end
    ).all()

    quarterly_gst_collected = sum(float(i.gst_amount) for i in q_invoices)
    quarterly_gst_paid = sum(float(e.gst_amount) for e in q_expenses)
    quarterly_gst_owing = quarterly_gst_collected - quarterly_gst_paid

    return render_template('dashboard.html',
                           recent_expenses=recent_expenses,
                           recent_invoices=recent_invoices,
                           current_quarter=current_quarter,
                           fy_year=fy_year,
                           quarterly_gst_owing=quarterly_gst_owing)


def _fmt_sent(dt):
    """Format a sender timestamp for display in Brisbane local time."""
    if not dt:
        return None
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo('Australia/Brisbane')).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return dt.strftime('%Y-%m-%d %H:%M')


def _sender_label(expense):
    """Display label for the person who sent the receipt."""
    if expense.submitted_by:
        return expense.submitted_by
    if expense.submitted_phone:
        return expense.submitted_phone
    return None


@hmi_bp.route('/review')
@login_required
def review():
    """Review queue — expenses still flagged `requires_review`.

    Operator, 2026-10-26: this used to be a filter on /expenses
    ("Show Requires Review" -> "Showing: Requires Review Only"). They want
    it to be a place you GO to, with a back button, not a mode you toggle.
    """
    user_id = session['user_id']
    expenses = (
        Expense.query
        .filter_by(user_id=user_id, requires_review=True)
        .order_by(
            Expense.submitted_at.desc().nullslast(),
            Expense.expense_date.desc(),
            Expense.id.desc(),
        )
        .all()
    )
    rows = [
        {
            'expense': e,
            'sender': _sender_label(e),
            'sent': _fmt_sent(e.submitted_at),
        }
        for e in expenses
    ]
    return render_template('review.html', rows=rows, review_count=len(rows))


@hmi_bp.route('/expenses')
@login_required
def expenses():
    user_id = session['user_id']

    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    requires_review = request.args.get('requires_review')

    query = Expense.query.filter_by(user_id=user_id)

    if start_date:
        try:
            start = date.fromisoformat(start_date)
            query = query.filter(Expense.expense_date >= start)
        except ValueError:
            pass

    if end_date:
        try:
            end = date.fromisoformat(end_date)
            query = query.filter(Expense.expense_date <= end)
        except ValueError:
            pass

    if requires_review == '1':
        query = query.filter(Expense.requires_review == True)

    # 2026-10-26: pending count for the "Review" button.
    review_count = Expense.query.filter_by(user_id=user_id, requires_review=True).count()

    expenses = query.order_by(Expense.expense_date.desc()).all()
    return render_template('expenses.html', expenses=expenses, review_count=review_count)


@hmi_bp.route('/expenses/new', methods=['GET', 'POST'])
@login_required
def new_expense():
    user_id = session['user_id']
    categories = AccountCategory.query.order_by(AccountCategory.name).all()

    if request.method == 'POST':
        vendor_name = request.form.get('vendor_name', '').strip()
        description = request.form.get('description', '').strip()
        amount = request.form.get('amount', '')
        amount_type = request.form.get('amount_type', 'excludes')
        gst_type = request.form.get('gst_type', '0')
        currency = request.form.get('currency', 'AUD')
        expense_date_str = request.form.get('expense_date', '')
        account_category_id = request.form.get('account_category_id', '')
        new_category_name = request.form.get('new_category_name', '').strip()
        attachment = request.files.get('attachment')

        if new_category_name and not account_category_id:
            existing = AccountCategory.query.filter_by(name=new_category_name).first()
            if existing:
                account_category_id = existing.id
            else:
                new_category = AccountCategory(name=new_category_name)
                db.session.add(new_category)
                db.session.flush()
                account_category_id = new_category.id
                categories = AccountCategory.query.order_by(AccountCategory.name).all()

        errors = []

        if not vendor_name:
            errors.append('Vendor name is required')
        if not amount:
            errors.append('Amount is required')
        if not expense_date_str:
            errors.append('Expense date is required')

        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('expense_form.html',
                                   categories=categories,
                                   vendor_name=vendor_name,
                                   description=description,
                                   amount=amount,
                                   amount_type=amount_type,
                                   gst_type=gst_type,
                                   currency=currency,
                                   expense_date=expense_date_str,
                                   account_category_id=account_category_id)

        try:
            input_amount = Decimal(amount)
            gst = Decimal(gst_type)
            expense_date = date.fromisoformat(expense_date_str)

            original_currency_amount = None
            exchange_rate = None

            if currency == 'USD':
                from app.shared.currency import get_usd_to_aud_rate, convert_usd_to_aud
                exchange_rate = get_usd_to_aud_rate(expense_date_str)
                if exchange_rate:
                    original_currency_amount = input_amount
                    input_amount = convert_usd_to_aud(input_amount, exchange_rate)
                else:
                    flash('Could not fetch exchange rate. Storing as AUD.', 'warning')
                    currency = 'AUD'

            if amount_type == 'includes':
                aud_amount = (input_amount / (Decimal('1') + gst)).quantize(Decimal('0.01'))
            else:
                aud_amount = input_amount

            gst_amount = (aud_amount * gst).quantize(Decimal('0.01'))
            total_amount = aud_amount + gst_amount
        except (ValueError, Exception) as e:
            flash(f'Invalid data: {str(e)}', 'error')
            return render_template('expense_form.html',
                                   categories=categories,
                                   vendor_name=vendor_name,
                                   description=description,
                                   amount=amount,
                                   amount_type=amount_type,
                                   gst_type=gst_type,
                                   currency=currency,
                                   expense_date=expense_date_str,
                                   account_category_id=account_category_id)

        attachment_path = None
        if attachment and attachment.filename:
            from werkzeug.utils import secure_filename
            import os
            filename = secure_filename(attachment.filename)
            allowed_extensions = {'pdf', 'png', 'jpg', 'jpeg'}
            if '.' not in filename or filename.rsplit('.', 1)[1].lower() not in allowed_extensions:
                flash('Invalid file type. Allowed: PDF, PNG, JPG', 'error')
                return render_template('expense_form.html',
                                       categories=categories,
                                       vendor_name=vendor_name,
                                       description=description,
                                       ex_gst_amount=ex_gst_amount,
                                       gst_type=gst_type,
                                       currency=currency,
                                       expense_date=expense_date_str,
                                       account_category_id=account_category_id)

            upload_folder = os.path.join(current_app.config.get('UPLOAD_FOLDER', 'uploads'), 'expenses')
            os.makedirs(upload_folder, exist_ok=True)
            import uuid
            unique_filename = f'{uuid.uuid4()}_{filename}'
            filepath = os.path.join(upload_folder, unique_filename)
            attachment.save(filepath)
            attachment_path = filepath

        expense = Expense(
            user_id=user_id,
            source='browser',
            vendor_name=vendor_name,
            description=description,
            currency=currency,
            original_currency_amount=original_currency_amount,
            exchange_rate=exchange_rate,
            ex_gst_amount=aud_amount,
            gst_amount=gst_amount,
            gst_type=gst,
            total_amount=total_amount,
            expense_date=expense_date,
            account_category_id=account_category_id if account_category_id else None,
            attachment_path=attachment_path
        )

        db.session.add(expense)
        db.session.flush()

        log = ActivityLog(
            user_id=user_id,
            action='CREATE',
            table_name='expenses',
            record_id=expense.id,
            new_values=expense.to_dict(),
            ip_address=request.remote_addr
        )
        db.session.add(log)
        db.session.commit()

        flash('Expense created successfully', 'success')
        return redirect(url_for('hmi.expenses'))

    return render_template('expense_form.html', categories=categories, expense=None)


@hmi_bp.route('/expenses/<expense_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_expense(expense_id):
    user_id = session['user_id']
    expense = Expense.query.filter_by(id=expense_id, user_id=user_id).first()

    if not expense:
        flash('Expense not found', 'error')
        return redirect(url_for('hmi.expenses'))

    categories = AccountCategory.query.order_by(AccountCategory.name).all()

    if request.method == 'POST':
        vendor_name = request.form.get('vendor_name', '').strip()
        description = request.form.get('description', '').strip()
        amount = request.form.get('amount', '')
        amount_type = request.form.get('amount_type', 'excludes')
        gst_type = request.form.get('gst_type', '0')
        currency = request.form.get('currency', 'AUD')
        expense_date_str = request.form.get('expense_date', '')
        account_category_id = request.form.get('account_category_id', '')
        new_category_name = request.form.get('new_category_name', '').strip()
        attachment = request.files.get('attachment')

        if new_category_name and not account_category_id:
            existing = AccountCategory.query.filter_by(name=new_category_name).first()
            if existing:
                account_category_id = existing.id
            else:
                new_category = AccountCategory(name=new_category_name)
                db.session.add(new_category)
                db.session.flush()
                account_category_id = new_category.id
                categories = AccountCategory.query.order_by(AccountCategory.name).all()

        errors = []

        if not vendor_name:
            errors.append('Vendor name is required')
        if not amount:
            errors.append('Amount is required')
        if not expense_date_str:
            errors.append('Expense date is required')

        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('expense_form.html',
                                   categories=categories,
                                   expense=expense,
                                   vendor_name=vendor_name,
                                   description=description,
                                   amount=amount,
                                   amount_type=amount_type,
                                   gst_type=gst_type,
                                   currency=currency,
                                   expense_date=expense_date_str,
                                   account_category_id=account_category_id)

        try:
            old_values = expense.to_dict()

            input_amount = Decimal(amount)
            gst = Decimal(gst_type)
            expense_date = date.fromisoformat(expense_date_str)

            original_currency_amount = None
            exchange_rate = None

            if currency == 'USD':
                from app.shared.currency import get_usd_to_aud_rate, convert_usd_to_aud
                exchange_rate = get_usd_to_aud_rate(expense_date_str)
                if exchange_rate:
                    original_currency_amount = input_amount
                    input_amount = convert_usd_to_aud(input_amount, exchange_rate)
                else:
                    flash('Could not fetch exchange rate. Storing as AUD.', 'warning')
                    currency = 'AUD'

            if amount_type == 'includes':
                aud_amount = (input_amount / (Decimal('1') + gst)).quantize(Decimal('0.01'))
            else:
                aud_amount = input_amount

            expense.vendor_name = vendor_name
            expense.description = description
            expense.currency = currency
            expense.original_currency_amount = original_currency_amount
            expense.exchange_rate = exchange_rate
            expense.ex_gst_amount = aud_amount
            expense.gst_type = gst
            expense.expense_date = expense_date
            expense.account_category_id = account_category_id if account_category_id else None
            expense.gst_amount = (expense.ex_gst_amount * expense.gst_type).quantize(Decimal('0.01'))
            expense.total_amount = expense.ex_gst_amount + expense.gst_amount
            expense.requires_review = False

            if attachment and attachment.filename:
                filename = secure_filename(attachment.filename)
                allowed_extensions = {'pdf', 'png', 'jpg', 'jpeg'}
                if '.' not in filename or filename.rsplit('.', 1)[1].lower() not in allowed_extensions:
                    flash('Invalid file type. Allowed: PDF, PNG, JPG', 'error')
                    return render_template('expense_form.html',
                                           categories=categories,
                                           expense=expense,
                                           vendor_name=vendor_name,
                                           description=description,
                                           amount=amount,
                                           amount_type=amount_type,
                                           gst_type=gst_type,
                                           currency=currency,
                                           expense_date=expense_date_str,
                                           account_category_id=account_category_id)

                upload_folder = os.path.join(current_app.config.get('UPLOAD_FOLDER', 'uploads'), 'expenses')
                os.makedirs(upload_folder, exist_ok=True)
                unique_filename = f'{uuid.uuid4()}_{filename}'
                filepath = os.path.join(upload_folder, unique_filename)
                attachment.save(filepath)
                expense.attachment_path = filepath

            log = ActivityLog(
                user_id=user_id,
                action='UPDATE',
                table_name='expenses',
                record_id=expense.id,
                old_values=old_values,
                new_values=expense.to_dict(),
                ip_address=request.remote_addr
            )
            db.session.add(log)
            db.session.commit()

            flash('Expense updated successfully', 'success')
            return redirect(url_for('hmi.expenses'))

        except (ValueError, Exception) as e:
            flash(f'Invalid data: {str(e)}', 'error')
            db.session.rollback()
            return render_template('expense_form.html',
                                   categories=categories,
                                   expense=expense,
                                   vendor_name=vendor_name,
                                   description=description,
                                   amount=amount,
                                   amount_type=amount_type,
                                   gst_type=gst_type,
                                   currency=currency,
                                   expense_date=expense_date_str,
                                   account_category_id=account_category_id)

    return render_template('expense_form.html', categories=categories, expense=expense)


@hmi_bp.route('/expenses/<expense_id>/delete', methods=['POST'])
@login_required
def delete_expense(expense_id):
    user_id = session['user_id']
    expense = Expense.query.filter_by(id=expense_id, user_id=user_id).first()

    if not expense:
        flash('Expense not found', 'error')
        return redirect(url_for('hmi.expenses'))

    old_values = expense.to_dict()

    log = ActivityLog(
        user_id=user_id,
        action='DELETE',
        table_name='expenses',
        record_id=expense.id,
        old_values=old_values,
        ip_address=request.remote_addr
    )
    db.session.add(log)

    db.session.delete(expense)
    db.session.commit()

    flash('Expense deleted successfully', 'success')
    return redirect(url_for('hmi.expenses'))


@hmi_bp.route('/invoices')
@login_required
def invoices():
    user_id = session['user_id']

    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    query = Invoice.query.filter_by(user_id=user_id)

    if start_date:
        try:
            start = date.fromisoformat(start_date)
            query = query.filter(Invoice.invoice_date >= start)
        except ValueError:
            pass

    if end_date:
        try:
            end = date.fromisoformat(end_date)
            query = query.filter(Invoice.invoice_date <= end)
        except ValueError:
            pass

    invoices = query.order_by(Invoice.invoice_date.desc()).all()
    return render_template('invoices.html', invoices=invoices)


@hmi_bp.route('/invoices/new', methods=['GET', 'POST'])
@login_required
def new_invoice():
    user_id = session['user_id']
    categories = AccountCategory.query.order_by(AccountCategory.name).all()

    if request.method == 'POST':
        client_name = request.form.get('client_name', '').strip()
        description = request.form.get('description', '').strip()
        amount = request.form.get('amount', '')
        amount_type = request.form.get('amount_type', 'excludes')
        gst_type = request.form.get('gst_type', '0')
        invoice_date_str = request.form.get('invoice_date', '')
        due_date_str = request.form.get('due_date', '')
        account_category_id = request.form.get('account_category_id', '')

        errors = []

        if not client_name:
            errors.append('Client name is required')
        if not amount:
            errors.append('Amount is required')
        if not invoice_date_str:
            errors.append('Invoice date is required')

        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('invoice_form.html',
                                   categories=categories,
                                   client_name=client_name,
                                   description=description,
                                   amount=amount,
                                   amount_type=amount_type,
                                   gst_type=gst_type,
                                   invoice_date=invoice_date_str,
                                   due_date=due_date_str,
                                   account_category_id=account_category_id)

        try:
            input_amount = Decimal(amount)
            gst = Decimal(gst_type)
            invoice_date = date.fromisoformat(invoice_date_str)
            due_date = date.fromisoformat(due_date_str) if due_date_str else None

            if amount_type == 'includes':
                ex_gst_amount = (input_amount / (Decimal('1') + gst)).quantize(Decimal('0.01'))
            else:
                ex_gst_amount = input_amount

            gst_amount = (ex_gst_amount * gst).quantize(Decimal('0.01'))
            total_amount = ex_gst_amount + gst_amount
        except (ValueError, Exception) as e:
            flash(f'Invalid data: {str(e)}', 'error')
            return render_template('invoice_form.html',
                                   categories=categories,
                                   client_name=client_name,
                                   description=description,
                                   amount=amount,
                                   amount_type=amount_type,
                                   gst_type=gst_type,
                                   invoice_date=invoice_date_str,
                                   due_date=due_date_str,
                                   account_category_id=account_category_id)

        invoice = Invoice(
            user_id=user_id,
            client_name=client_name,
            description=description,
            ex_gst_amount=ex_gst_amount,
            gst_amount=gst_amount,
            gst_type=gst,
            total_amount=total_amount,
            invoice_date=invoice_date,
            due_date=due_date,
            account_category_id=account_category_id if account_category_id else None
        )

        db.session.add(invoice)
        db.session.flush()

        log = ActivityLog(
            user_id=user_id,
            action='CREATE',
            table_name='invoices',
            record_id=invoice.id,
            new_values=invoice.to_dict(),
            ip_address=request.remote_addr
        )
        db.session.add(log)
        db.session.commit()

        flash('Invoice created successfully', 'success')
        return redirect(url_for('hmi.invoices'))

    return render_template('invoice_form.html', categories=categories, invoice=None)


@hmi_bp.route('/invoices/<invoice_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_invoice(invoice_id):
    user_id = session['user_id']
    invoice = Invoice.query.filter_by(id=invoice_id, user_id=user_id).first()

    if not invoice:
        flash('Invoice not found', 'error')
        return redirect(url_for('hmi.invoices'))

    categories = AccountCategory.query.order_by(AccountCategory.name).all()

    if request.method == 'POST':
        client_name = request.form.get('client_name', '').strip()
        description = request.form.get('description', '').strip()
        amount = request.form.get('amount', '')
        amount_type = request.form.get('amount_type', 'excludes')
        gst_type = request.form.get('gst_type', '0')
        invoice_date_str = request.form.get('invoice_date', '')
        due_date_str = request.form.get('due_date', '')
        account_category_id = request.form.get('account_category_id', '')

        errors = []

        if not client_name:
            errors.append('Client name is required')
        if not amount:
            errors.append('Amount is required')
        if not invoice_date_str:
            errors.append('Invoice date is required')

        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('invoice_form.html',
                                   categories=categories,
                                   invoice=invoice,
                                   client_name=client_name,
                                   description=description,
                                   amount=amount,
                                   amount_type=amount_type,
                                   gst_type=gst_type,
                                   invoice_date=invoice_date_str,
                                   due_date=due_date_str,
                                   account_category_id=account_category_id)

        try:
            old_values = invoice.to_dict()

            input_amount = Decimal(amount)
            gst = Decimal(gst_type)

            if amount_type == 'includes':
                ex_gst_amount = (input_amount / (Decimal('1') + gst)).quantize(Decimal('0.01'))
            else:
                ex_gst_amount = input_amount

            invoice.client_name = client_name
            invoice.description = description
            invoice.ex_gst_amount = ex_gst_amount
            invoice.gst_type = gst
            invoice.invoice_date = date.fromisoformat(invoice_date_str)
            invoice.due_date = date.fromisoformat(due_date_str) if due_date_str else None
            invoice.account_category_id = account_category_id if account_category_id else None
            invoice.gst_amount = (invoice.ex_gst_amount * invoice.gst_type).quantize(Decimal('0.01'))
            invoice.total_amount = invoice.ex_gst_amount + invoice.gst_amount

            log = ActivityLog(
                user_id=user_id,
                action='UPDATE',
                table_name='invoices',
                record_id=invoice.id,
                old_values=old_values,
                new_values=invoice.to_dict(),
                ip_address=request.remote_addr
            )
            db.session.add(log)
            db.session.commit()

            flash('Invoice updated successfully', 'success')
            return redirect(url_for('hmi.invoices'))

        except (ValueError, Exception) as e:
            flash(f'Invalid data: {str(e)}', 'error')
            db.session.rollback()
            return render_template('invoice_form.html',
                                   categories=categories,
                                   invoice=invoice,
                                   client_name=client_name,
                                   description=description,
                                   amount=amount,
                                   amount_type=amount_type,
                                   gst_type=gst_type,
                                   invoice_date=invoice_date_str,
                                   due_date=due_date_str,
                                   account_category_id=account_category_id)

    return render_template('invoice_form.html', categories=categories, invoice=invoice)


@hmi_bp.route('/invoices/<invoice_id>/delete', methods=['POST'])
@login_required
def delete_invoice(invoice_id):
    user_id = session['user_id']
    invoice = Invoice.query.filter_by(id=invoice_id, user_id=user_id).first()

    if not invoice:
        flash('Invoice not found', 'error')
        return redirect(url_for('hmi.invoices'))

    old_values = invoice.to_dict()

    log = ActivityLog(
        user_id=user_id,
        action='DELETE',
        table_name='invoices',
        record_id=invoice.id,
        old_values=old_values,
        ip_address=request.remote_addr
    )
    db.session.add(log)

    db.session.delete(invoice)
    db.session.commit()

    flash('Invoice deleted successfully', 'success')
    return redirect(url_for('hmi.invoices'))


@hmi_bp.route('/invoices/<invoice_id>/pdf')
@login_required
def download_invoice_pdf(invoice_id):
    user_id = session['user_id']
    invoice = Invoice.query.filter_by(id=invoice_id, user_id=user_id).first()

    if not invoice:
        flash('Invoice not found', 'error')
        return redirect(url_for('hmi.invoices'))

    user = User.query.get(user_id)

    from app.shared.pdf import generate_invoice_pdf
    buffer = generate_invoice_pdf(user, invoice)

    filename = f'invoice_{invoice.id[:8]}_{invoice.invoice_date}.pdf'
    return send_file(
        buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=filename
    )


@hmi_bp.route('/reports')
@login_required
def reports():
    return render_template('reports.html')


@hmi_bp.route('/reports/monthly', methods=['GET', 'POST'])
@login_required
def monthly_report():
    user_id = session['user_id']

    if request.method == 'POST':
        year = request.form.get('year', type=int)
        month = request.form.get('month', type=int)
    else:
        year = request.args.get('year', type=int)
        month = request.args.get('month', type=int)

    current_date = date.today()
    if not year:
        year = current_date.year
    if not month:
        month = current_date.month

    from calendar import monthrange
    _, last_day = monthrange(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)

    expenses = Expense.query.filter(
        Expense.user_id == user_id,
        Expense.expense_date >= start_date,
        Expense.expense_date <= end_date
    ).all()

    invoices = Invoice.query.filter(
        Invoice.user_id == user_id,
        Invoice.invoice_date >= start_date,
        Invoice.invoice_date <= end_date
    ).all()

    total_expenses_ex_gst = sum(float(e.ex_gst_amount) for e in expenses)
    total_expenses_gst = sum(float(e.gst_amount) for e in expenses)
    total_expenses = sum(float(e.total_amount) for e in expenses)

    total_income_ex_gst = sum(float(i.ex_gst_amount) for i in invoices)
    total_income_gst = sum(float(i.gst_amount) for i in invoices)
    total_income = sum(float(i.total_amount) for i in invoices)

    net_profit_ex_gst = total_income_ex_gst - total_expenses_ex_gst
    gst_owing = total_income_gst - total_expenses_gst

    by_category = {}
    for expense in expenses:
        cat_name = expense.account_category.name if expense.account_category else 'Uncategorized'
        if cat_name not in by_category:
            by_category[cat_name] = {'ex_gst': 0, 'gst': 0, 'total': 0, 'type': 'expense'}
        by_category[cat_name]['ex_gst'] += float(expense.ex_gst_amount)
        by_category[cat_name]['gst'] += float(expense.gst_amount)
        by_category[cat_name]['total'] += float(expense.total_amount)

    for invoice in invoices:
        cat_name = invoice.account_category.name if invoice.account_category else 'Uncategorized'
        if cat_name not in by_category:
            by_category[cat_name] = {'ex_gst': 0, 'gst': 0, 'total': 0, 'type': 'income'}
        by_category[cat_name]['ex_gst'] += float(invoice.ex_gst_amount)
        by_category[cat_name]['gst'] += float(invoice.gst_amount)
        by_category[cat_name]['total'] += float(invoice.total_amount)

    month_names = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']

    return render_template('monthly_report.html',
                           year=year,
                           month=month,
                           month_name=month_names[month],
                           start_date=start_date,
                           end_date=end_date,
                           expenses=expenses,
                           invoices=invoices,
                           total_expenses_ex_gst=total_expenses_ex_gst,
                           total_expenses_gst=total_expenses_gst,
                           total_expenses=total_expenses,
                           total_income_ex_gst=total_income_ex_gst,
                           total_income_gst=total_income_gst,
                           total_income=total_income,
                           net_profit_ex_gst=net_profit_ex_gst,
                           gst_owing=gst_owing,
                           by_category=by_category)


@hmi_bp.route('/reports/quarterly-bas', methods=['GET', 'POST'])
@login_required
def quarterly_bas():
    user_id = session['user_id']

    if request.method == 'POST':
        year = request.form.get('year', type=int)
        quarter = request.form.get('quarter', type=int)
    else:
        year = request.args.get('year', type=int)
        quarter = request.args.get('quarter', type=int)

    current_date = date.today()
    if not year:
        year = current_date.year if current_date.month >= 7 else current_date.year - 1
    if not quarter:
        if current_date.month <= 3:
            quarter = 3
        elif current_date.month <= 6:
            quarter = 4
        elif current_date.month <= 9:
            quarter = 1
        else:
            quarter = 2

    def get_quarter_dates(year, quarter):
        if quarter == 1:
            return date(year, 7, 1), date(year, 9, 30)
        elif quarter == 2:
            return date(year, 10, 1), date(year, 12, 31)
        elif quarter == 3:
            return date(year + 1, 1, 1), date(year + 1, 3, 31)
        else:
            return date(year + 1, 4, 1), date(year + 1, 6, 30)

    start_date, end_date = get_quarter_dates(year, quarter)

    expenses = Expense.query.filter(
        Expense.user_id == user_id,
        Expense.expense_date >= start_date,
        Expense.expense_date <= end_date
    ).all()

    invoices = Invoice.query.filter(
        Invoice.user_id == user_id,
        Invoice.invoice_date >= start_date,
        Invoice.invoice_date <= end_date
    ).all()

    total_gst_collected = sum(float(i.gst_amount) for i in invoices)
    total_gst_paid = sum(float(e.gst_amount) for e in expenses)
    gst_owing = total_gst_collected - total_gst_paid
    total_income_ex_gst = sum(float(i.ex_gst_amount) for i in invoices)
    total_expenses_ex_gst = sum(float(e.ex_gst_amount) for e in expenses)

    return render_template('quarterly_bas.html',
                           year=year,
                           quarter=quarter,
                           start_date=start_date,
                           end_date=end_date,
                           expenses=expenses,
                           invoices=invoices,
                           total_gst_collected=total_gst_collected,
                           total_gst_paid=total_gst_paid,
                           gst_owing=gst_owing,
                           total_income_ex_gst=total_income_ex_gst,
                           total_expenses_ex_gst=total_expenses_ex_gst)


@hmi_bp.route('/reports/yearly-pnl', methods=['GET', 'POST'])
@login_required
def yearly_pnl():
    user_id = session['user_id']

    if request.method == 'POST':
        year = request.form.get('year', type=int)
    else:
        year = request.args.get('year', type=int)

    current_date = date.today()
    if not year:
        year = current_date.year if current_date.month >= 7 else current_date.year - 1

    def get_fy_dates(year):
        return date(year, 7, 1), date(year + 1, 6, 30)

    def get_quarter_dates(year, quarter):
        if quarter == 1:
            return date(year, 7, 1), date(year, 9, 30)
        elif quarter == 2:
            return date(year, 10, 1), date(year, 12, 31)
        elif quarter == 3:
            return date(year + 1, 1, 1), date(year + 1, 3, 31)
        else:
            return date(year + 1, 4, 1), date(year + 1, 6, 30)

    start_date, end_date = get_fy_dates(year)

    expenses = Expense.query.filter(
        Expense.user_id == user_id,
        Expense.expense_date >= start_date,
        Expense.expense_date <= end_date
    ).all()

    invoices = Invoice.query.filter(
        Invoice.user_id == user_id,
        Invoice.invoice_date >= start_date,
        Invoice.invoice_date <= end_date
    ).all()

    quarterly_data = []
    for q in range(1, 5):
        q_start, q_end = get_quarter_dates(year, q)
        q_expenses = [e for e in expenses if q_start <= e.expense_date <= q_end]
        q_invoices = [i for i in invoices if q_start <= i.invoice_date <= q_end]

        quarterly_data.append({
            'quarter': q,
            'start_date': q_start,
            'end_date': q_end,
            'expenses': q_expenses,
            'invoices': q_invoices,
            'total_income_ex_gst': sum(float(i.ex_gst_amount) for i in q_invoices),
            'total_expenses_ex_gst': sum(float(e.ex_gst_amount) for e in q_expenses),
            'gst_collected': sum(float(i.gst_amount) for i in q_invoices),
            'gst_paid': sum(float(e.gst_amount) for e in q_expenses),
            'gst_owing': sum(float(i.gst_amount) for i in q_invoices) - sum(float(e.gst_amount) for e in q_expenses)
        })

    total_expenses_ex_gst = sum(float(e.ex_gst_amount) for e in expenses)
    total_expenses_gst = sum(float(e.gst_amount) for e in expenses)
    total_expenses = sum(float(e.total_amount) for e in expenses)

    total_income_ex_gst = sum(float(i.ex_gst_amount) for i in invoices)
    total_income_gst = sum(float(i.gst_amount) for i in invoices)
    total_income = sum(float(i.total_amount) for i in invoices)

    net_profit_ex_gst = total_income_ex_gst - total_expenses_ex_gst
    total_gst_owing = total_income_gst - total_expenses_gst

    return render_template('yearly_pnl.html',
                           year=year,
                           start_date=start_date,
                           end_date=end_date,
                           quarterly_data=quarterly_data,
                           expenses=expenses,
                           invoices=invoices,
                           total_income_ex_gst=total_income_ex_gst,
                           total_income_gst=total_income_gst,
                           total_income=total_income,
                           total_expenses_ex_gst=total_expenses_ex_gst,
                           total_expenses_gst=total_expenses_gst,
                           total_expenses=total_expenses,
                           net_profit_ex_gst=net_profit_ex_gst,
                           total_gst_owing=total_gst_owing)


@hmi_bp.route('/account-categories')
@login_required
def account_categories():
    categories = AccountCategory.query.order_by(AccountCategory.name).all()
    return render_template('account_categories.html', categories=categories)


@hmi_bp.route('/account-categories/new', methods=['POST'])
@login_required
def new_account_category():
    user_id = session['user_id']
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()

    if not name:
        flash('Category name is required', 'error')
        return redirect(url_for('hmi.account_categories'))

    if AccountCategory.query.filter_by(name=name).first():
        flash('Category name already exists', 'error')
        return redirect(url_for('hmi.account_categories'))

    category = AccountCategory(name=name, description=description)
    db.session.add(category)
    db.session.commit()

    flash('Account category created successfully', 'success')
    return redirect(url_for('hmi.account_categories'))


@hmi_bp.route('/account-categories/<category_id>/delete', methods=['POST'])
@login_required
def delete_account_category(category_id):
    category = AccountCategory.query.get(category_id)

    if not category:
        flash('Category not found', 'error')
        return redirect(url_for('hmi.account_categories'))

    db.session.delete(category)
    db.session.commit()

    flash('Account category deleted successfully', 'success')
    return redirect(url_for('hmi.account_categories'))


@hmi_bp.route('/activity-logs')
@login_required
def activity_logs():
    user_id = session['user_id']

    page = request.args.get('page', 1, type=int)
    per_page = 50

    logs = ActivityLog.query.filter_by(user_id=user_id).order_by(
        ActivityLog.created_at.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)

    return render_template('activity_logs.html', logs=logs)


@hmi_bp.route('/api-key')
@login_required
def api_key_management():
    user = User.query.get(session['user_id'])
    return render_template('api_key.html', user=user)


@hmi_bp.route('/api-key/generate', methods=['POST'])
@login_required
def generate_api_key():
    user = User.query.get(session['user_id'])

    # Log a prefix of the PLAINTEXT key, not the stored ciphertext, so
    # the "old" and "new" entries in the activity log are comparable.
    old_api_key = user.get_api_key() or ''
    new_api_key = user.generate_api_key()
    db.session.commit()

    log = ActivityLog(
        user_id=user.id,
        action='UPDATE',
        table_name='users',
        record_id=user.id,
        old_values={'api_key': old_api_key[:8] + '...' if old_api_key else None},
        new_values={'api_key': new_api_key[:8] + '...'},
        ip_address=request.remote_addr
    )
    db.session.add(log)
    db.session.commit()

    flash(f'New API key generated: {new_api_key}', 'success')
    return redirect(url_for('hmi.api_key_management'))


# ---------------------------------------------------------------------------
# Xero CSV export (added 2026-09-17 alongside the existing /api/exports/xero
# route — this gives the operator a one-click way to download a Xero-ready export
# for the accountant without needing to hit the API with curl).
# ---------------------------------------------------------------------------

@hmi_bp.route('/exports/xero', methods=['GET'])
@login_required
def xero_export_form():
    """Render the Xero export form with two options:
       - All time (defaults to the earliest invoice/expense we have)
       - Custom date range
    Both submit to /exports/xero/download.
    """
    user = User.query.get(session['user_id'])
    # Earliest date we have any data for — useful as the default "from" for
    # the custom range option. Falls back to start of 2024 if no data.
    from app.models.invoice import Invoice
    from app.models.expense import Expense
    from sqlalchemy import func

    earliest_invoice = db.session.query(func.min(Invoice.invoice_date)).filter(
        Invoice.user_id == user.id,
    ).scalar()
    earliest_expense = db.session.query(func.min(Expense.expense_date)).filter(
        Expense.user_id == user.id,
    ).scalar()
    candidates = [d for d in (earliest_invoice, earliest_expense) if d]
    earliest = min(candidates) if candidates else date(2024, 1, 1)
    today = date.today()

    return render_template(
        'xero_export.html',
        earliest=earliest.isoformat(),
        today=today.isoformat(),
    )


@hmi_bp.route('/exports/xero/download', methods=['POST'])
@login_required
def xero_export_download():
    """Build and return the Xero export ZIP based on form inputs.

    Form fields:
      mode            (str)   'all' for all-time, 'custom' for custom range
      from_date       (str)   ISO date — required when mode='custom'
      to_date         (str)   ISO date — required when mode='custom'
      include_drafts  (checkbox) 'on' to include draft invoices
    """
    user = User.query.get(session['user_id'])

    mode = request.form.get('mode', 'all')
    include_drafts = request.form.get('include_drafts') == 'on'

    if mode == 'all':
        # Use the API helper with empty range filtering — actually we still
        # need a date range. For "all time", use the earliest data date and
        # today. If there's no data, the resulting ZIP is still valid (just
        # empty CSVs with headers).
        from app.models.invoice import Invoice
        from app.models.expense import Expense
        from sqlalchemy import func
        earliest_inv = db.session.query(func.min(Invoice.invoice_date)).filter(
            Invoice.user_id == user.id,
        ).scalar()
        earliest_exp = db.session.query(func.min(Expense.expense_date)).filter(
            Expense.user_id == user.id,
        ).scalar()
        candidates = [d for d in (earliest_inv, earliest_exp) if d]
        from_d = min(candidates) if candidates else date(2024, 1, 1)
        to_d = date.today()
    elif mode == 'custom':
        from_str = request.form.get('from_date', '').strip()
        to_str = request.form.get('to_date', '').strip()
        try:
            from_d = date.fromisoformat(from_str)
            to_d = date.fromisoformat(to_str)
        except (ValueError, TypeError):
            flash('Please enter valid dates in YYYY-MM-DD format.', 'error')
            return redirect(url_for('hmi.xero_export_form'))
        if from_d > to_d:
            flash('From date must be on or before To date.', 'error')
            return redirect(url_for('hmi.xero_export_form'))
    else:
        flash('Unknown export mode.', 'error')
        return redirect(url_for('hmi.xero_export_form'))

    # Delegate to the shared Xero export builder (same one the API uses).
    from app.api.xero_export import build_xero_zip, log_xero_export
    period_label = f'{from_d.isoformat()}_to_{to_d.isoformat()}'

    zip_bytes, counts = build_xero_zip(user, from_d, to_d, include_drafts=include_drafts)
    log_xero_export(user, period_label, counts, include_drafts, source='hmi')

    flash(
        f'Export complete — {counts["invoices"]} invoice(s), '
        f'{counts["bills"]} bill(s), {counts["contacts"]} contact(s).',
        'success',
    )

    return send_file(
        io.BytesIO(zip_bytes),
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'peristyle-xero-export-{period_label}.zip',
    )
