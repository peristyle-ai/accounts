"""Payroll HMI routes — payroll_expansion.md §7.

Gated by `@login_required` (same convention as the rest of hmi/routes.py).
Templates live under `app/hmi/templates/payroll/`.

Routes:
    /payroll                                  — payroll dashboard (the headline ask)
    /payroll/summary                          — YTD + super owing + PAYG card view
    /payroll/employees                        — list
    /payroll/employees/new                    — create form
    /payroll/employees/<id>                  — detail (pay events + super remittances)
    /payroll/employees/<id>/edit              — edit form
    /payroll/employees/<id>/terminate         — POST confirmation
    /payroll/pay-events                       — list, filterable
    /payroll/pay-events/new                   — create form
    /payroll/pay-events/<id>                  — detail
    /payroll/pay-events/<id>/edit             — edit (only when draft)
    /payroll/super-payments                   — list
    /payroll/super-payments/new               — create form
    /payroll/super-payments/<id>              — detail
"""

from datetime import date
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, session

from app.hmi.routes import login_required
from app.models import db
from app.models.employee import Employee
from app.models.pay_event import PayEvent, PAY_EVENT_STATUSES
from app.models.super_payment import SuperPayment


payroll_hmi_bp = Blueprint('payroll_hmi', __name__, url_prefix='/payroll')


def _australian_fy_bounds(fy_year):
    return date(fy_year - 1, 7, 1), date(fy_year, 6, 30)


def _infer_fy_year(today=None):
    """Return the FY year-label for `today` under the convention that
    fy_year=Y means the FY ending 30 Jun Y (i.e. runs 1 Jul Y-1 .. 30 Jun Y).

    So Sep 2026 → FY2027, Jan 2027 → FY2027, Jul 2027 → FY2028.
    """
    if today is None:
        today = date.today()
    return today.year + 1 if today.month >= 7 else today.year


def _employee_or_404(user_id, employee_id):
    return Employee.query.filter_by(id=employee_id, user_id=user_id).first()


def _pay_event_or_404(user_id, pe_id):
    return PayEvent.query.filter_by(id=pe_id, user_id=user_id).first()


def _super_payment_or_404(user_id, sp_id):
    return SuperPayment.query.filter_by(id=sp_id, user_id=user_id).first()


# ===========================================================================
# Dashboard — the headline ask
# ===========================================================================

@payroll_hmi_bp.route('/')
@payroll_hmi_bp.route('/summary')
@login_required
def payroll_summary():
    user_id = session['user_id']
    today = date.today()
    fy_year = _infer_fy_year(today)
    fy_start, fy_end = _australian_fy_bounds(fy_year)

    pay_events = (PayEvent.query
                  .filter(PayEvent.user_id == user_id,
                          PayEvent.payment_date >= fy_start,
                          PayEvent.payment_date <= fy_end,
                          PayEvent.status != 'cancelled')
                  .all())

    ytd_gross = sum((Decimal(str(pe.gross_amount)) for pe in pay_events), Decimal('0'))
    ytd_payg = sum((Decimal(str(pe.payg_tax_amount)) for pe in pay_events), Decimal('0'))
    ytd_net = sum((Decimal(str(pe.net_amount)) for pe in pay_events), Decimal('0'))
    super_payable = sum((Decimal(str(pe.super_payable_amount)) for pe in pay_events), Decimal('0'))
    super_paid = sum((Decimal(str(pe.super_paid_amount or 0)) for pe in pay_events), Decimal('0'))
    super_outstanding = super_payable - super_paid

    # Per-employee summary
    employees = Employee.query.filter_by(user_id=user_id).order_by(Employee.legal_name).all()
    by_employee = []
    for emp in employees:
        emp_events = [pe for pe in pay_events if pe.employee_id == emp.id]
        if not emp_events:
            continue
        emp_gross = sum((Decimal(str(pe.gross_amount)) for pe in emp_events), Decimal('0'))
        emp_paid = sum((Decimal(str(pe.super_paid_amount or 0)) for pe in emp_events), Decimal('0'))
        by_employee.append({
            'employee': emp,
            'ytd_gross': emp_gross,
            'ytd_super_paid': emp_paid,
            'pay_event_count': len(emp_events),
            'last_payment_date': max((pe.payment_date for pe in emp_events), default=None),
        })

    # Recent pay events (last 5 across all employees)
    recent_events = (PayEvent.query
                     .filter_by(user_id=user_id)
                     .filter(PayEvent.status != 'cancelled')
                     .order_by(PayEvent.payment_date.desc())
                     .limit(5).all())
    emp_lookup = {e.id: e for e in employees}
    for pe in recent_events:
        pe._employee = emp_lookup.get(pe.employee_id)

    # Recent deliveries (last 5 across all employees)
    from app.models.payslip_delivery import PayslipDelivery
    deliveries = (PayslipDelivery.query
                  .join(PayEvent, PayslipDelivery.pay_event_id == PayEvent.id)
                  .filter(PayEvent.user_id == user_id)
                  .order_by(PayslipDelivery.sent_at.desc())
                  .limit(5).all())
    pe_lookup = {pe.id: pe for pe in PayEvent.query.filter(
        PayEvent.id.in_([d.pay_event_id for d in deliveries])).all()}
    for d in deliveries:
        pe = pe_lookup.get(d.pay_event_id)
        d._employee = (pe and emp_lookup.get(pe.employee_id))

    return render_template(
        'payroll/payroll_summary.html',
        fy_year=fy_year,
        fy_start=fy_start,
        fy_end=fy_end,
        ytd_gross=ytd_gross,
        ytd_payg=ytd_payg,
        ytd_net=ytd_net,
        super_payable=super_payable,
        super_paid=super_paid,
        super_outstanding=super_outstanding,
        by_employee=by_employee,
        recent_events=recent_events,
        recent_deliveries=deliveries,
    )


# Keep the historical endpoint too (some bookmarks point at /payroll-dashboard).
@payroll_hmi_bp.route('/dashboard')
@login_required
def payroll_dashboard():
    return redirect(url_for('payroll_hmi.payroll_summary'))


# ===========================================================================
# Employees
# ===========================================================================

@payroll_hmi_bp.route('/employees')
@login_required
def payroll_employees_list():
    user_id = session['user_id']
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
        from sqlalchemy import or_
        q = q.filter(or_(Employee.legal_name.ilike(like),
                          Employee.preferred_name.ilike(like)))

    employees = q.order_by(Employee.legal_name.asc()).all()
    return render_template('payroll/payroll_employees.html',
                           employees=employees,
                           status_filter=status,
                           position_filter=position,
                           search=search)


@payroll_hmi_bp.route('/employees/new', methods=['GET', 'POST'])
@login_required
def payroll_employee_new():
    user_id = session['user_id']

    if request.method == 'POST':
        legal_name = (request.form.get('legal_name') or '').strip()
        if not legal_name:
            flash('Legal name is required', 'error')
            return render_template('payroll/payroll_employee_form.html', employee=None)

        e = Employee(
            user_id=user_id,
            legal_name=legal_name,
            preferred_name=(request.form.get('preferred_name') or '').strip() or None,
            position=(request.form.get('position') or '').strip() or None,
            email_work=(request.form.get('email_work') or '').strip() or None,
            email_personal=(request.form.get('email_personal') or '').strip() or None,
            employment_status=(request.form.get('employment_status') or 'active'),
            pay_frequency=(request.form.get('pay_frequency') or 'weekly'),
            super_fund_name=(request.form.get('super_fund_name') or '').strip() or None,
            super_fund_member_no=(request.form.get('super_fund_member_no') or '').strip() or None,
            bank_account_name=(request.form.get('bank_account_name') or '').strip() or None,
            bank_reference_prefix=(request.form.get('bank_reference_prefix') or '').strip() or None,
            notes=(request.form.get('notes') or '').strip() or None,
        )
        for attr, val, minimum in (
            ('default_gross_amount', request.form.get('default_gross_amount'), Decimal('0')),
            ('super_rate_pct', request.form.get('super_rate_pct'), Decimal('12.00')),
        ):
            try:
                setattr(e, attr, Decimal(val) if val else minimum)
            except Exception:
                setattr(e, attr, minimum)
        for attr, parser in (
            ('start_date', date.fromisoformat),
            ('end_date', date.fromisoformat),
        ):
            v = request.form.get(attr)
            if v:
                try:
                    setattr(e, attr, parser(v))
                except ValueError:
                    pass

        # PII — encrypted via the *_plain setters
        try:
            if request.form.get('tfn'):
                e.tfn_plain = request.form['tfn']
            if request.form.get('bank_bsb'):
                e.bank_bsb_plain = request.form['bank_bsb']
            if request.form.get('bank_account_number'):
                e.bank_account_number_plain = request.form['bank_account_number']
        except Exception as exc:
            flash(f'PII encryption failed: {exc}', 'error')
            return render_template('payroll/payroll_employee_form.html', employee=None)

        db.session.add(e)
        db.session.commit()
        flash('Employee created', 'success')
        return redirect(url_for('payroll_hmi.payroll_employee_detail',
                                 employee_id=e.id))

    return render_template('payroll/payroll_employee_form.html', employee=None)


@payroll_hmi_bp.route('/employees/<employee_id>')
@login_required
def payroll_employee_detail(employee_id):
    user_id = session['user_id']
    e = _employee_or_404(user_id, employee_id)
    if e is None:
        flash('Employee not found', 'error')
        return redirect(url_for('payroll_hmi.payroll_employees_list'))

    pay_events = (PayEvent.query
                  .filter_by(user_id=user_id, employee_id=e.id)
                  .order_by(PayEvent.payment_date.desc())
                  .limit(12).all())
    supers = (SuperPayment.query
              .filter_by(user_id=user_id, employee_id=e.id)
              .order_by(SuperPayment.remittance_date.desc())
              .limit(12).all())

    return render_template('payroll/payroll_employee_detail.html',
                           employee=e, pay_events=pay_events,
                           super_payments=supers)


@payroll_hmi_bp.route('/employees/<employee_id>/edit', methods=['GET', 'POST'])
@login_required
def payroll_employee_edit(employee_id):
    user_id = session['user_id']
    e = _employee_or_404(user_id, employee_id)
    if e is None:
        flash('Employee not found', 'error')
        return redirect(url_for('payroll_hmi.payroll_employees_list'))

    if request.method == 'POST':
        e.legal_name = (request.form.get('legal_name') or '').strip() or e.legal_name
        for attr in ('preferred_name', 'position', 'email_work', 'email_personal',
                     'employment_status', 'pay_frequency', 'super_fund_name',
                     'super_fund_member_no', 'bank_account_name',
                     'bank_reference_prefix', 'notes'):
            v = request.form.get(attr)
            if v is not None:
                setattr(e, attr, (v.strip() or None))
        for attr, minimum in (
            ('default_gross_amount', Decimal('0')),
            ('super_rate_pct', Decimal('12.00')),
        ):
            v = request.form.get(attr)
            try:
                setattr(e, attr, Decimal(v) if v else minimum)
            except Exception:
                pass
        for attr, parser in (
            ('start_date', date.fromisoformat),
            ('end_date', date.fromisoformat),
        ):
            v = request.form.get(attr)
            if v:
                try:
                    setattr(e, attr, parser(v))
                except ValueError:
                    pass

        try:
            if request.form.get('tfn') is not None:
                e.tfn_plain = (request.form['tfn'].strip() or None)
            if request.form.get('bank_bsb') is not None:
                e.bank_bsb_plain = (request.form['bank_bsb'].strip() or None)
            if request.form.get('bank_account_number') is not None:
                e.bank_account_number_plain = (request.form['bank_account_number'].strip() or None)
        except Exception as exc:
            flash(f'PII encryption failed: {exc}', 'error')
            return render_template('payroll/payroll_employee_form.html', employee=e)
        db.session.commit()
        flash('Employee updated', 'success')
        return redirect(url_for('payroll_hmi.payroll_employee_detail',
                                 employee_id=e.id))

    return render_template('payroll/payroll_employee_form.html', employee=e)


@payroll_hmi_bp.route('/employees/<employee_id>/terminate', methods=['POST'])
@login_required
def payroll_employee_terminate(employee_id):
    user_id = session['user_id']
    e = _employee_or_404(user_id, employee_id)
    if e is None:
        flash('Employee not found', 'error')
        return redirect(url_for('payroll_hmi.payroll_employees_list'))
    e.employment_status = 'terminated'
    if not e.end_date:
        e.end_date = date.today()
    db.session.commit()
    flash('Employee terminated', 'success')
    return redirect(url_for('payroll_hmi.payroll_employee_detail',
                             employee_id=e.id))


# ===========================================================================
# Pay events
# ===========================================================================

@payroll_hmi_bp.route('/pay-events')
@login_required
def payroll_pay_events_list():
    user_id = session['user_id']
    q = PayEvent.query.filter_by(user_id=user_id)

    employee_id = (request.args.get('employee_id') or '').strip()
    if employee_id:
        q = q.filter(PayEvent.employee_id == employee_id)
    if request.args.get('start_date'):
        try:
            q = q.filter(PayEvent.payment_date >= date.fromisoformat(request.args['start_date']))
        except ValueError:
            pass
    if request.args.get('end_date'):
        try:
            q = q.filter(PayEvent.payment_date <= date.fromisoformat(request.args['end_date']))
        except ValueError:
            pass
    if request.args.get('status'):
        q = q.filter(PayEvent.status == request.args['status'])

    events = q.order_by(PayEvent.payment_date.desc()).all()
    employees = Employee.query.filter_by(user_id=user_id).order_by(Employee.legal_name).all()
    emp_lookup = {e.id: e for e in employees}
    for pe in events:
        pe._employee = emp_lookup.get(pe.employee_id)
    return render_template('payroll/payroll_pay_events.html',
                           events=events, employees=employees,
                           employee_filter=employee_id,
                           status_filter=request.args.get('status', ''))


@payroll_hmi_bp.route('/pay-events/new', methods=['GET', 'POST'])
@login_required
def payroll_pay_event_new():
    user_id = session['user_id']
    employees = Employee.query.filter_by(user_id=user_id, employment_status='active') \
                              .order_by(Employee.legal_name).all()
    employee_id = request.args.get('employee_id') or request.form.get('employee_id') or ''
    employee = None
    if employee_id:
        employee = next((e for e in employees if e.id == employee_id), None)

    if request.method == 'POST':
        employee_id = request.form.get('employee_id') or ''
        employee = next((e for e in employees if e.id == employee_id), None)
        if employee is None:
            flash('Employee is required', 'error')
            return render_template('payroll/payroll_pay_event_form.html',
                                   employees=employees, employee=None, event=None)

        try:
            payment_date = date.fromisoformat(request.form['payment_date'])
            pay_period_start = date.fromisoformat(request.form['pay_period_start'])
            pay_period_end = date.fromisoformat(request.form['pay_period_end'])
            gross_amount = Decimal(request.form.get('gross_amount') or '0')
            payg_tax_amount = Decimal(request.form.get('payg_tax_amount') or '0')
            net_amount = Decimal(request.form.get('net_amount') or '0')
            super_ote_amount = Decimal(request.form.get('super_ote_amount') or gross_amount)
        except (KeyError, ValueError) as exc:
            flash(f'Invalid input: {exc}', 'error')
            return render_template('payroll/payroll_pay_event_form.html',
                                   employees=employees, employee=employee, event=None)

        if pay_period_end < pay_period_start:
            flash('Period end must be on/after period start', 'error')
            return render_template('payroll/payroll_pay_event_form.html',
                                   employees=employees, employee=employee, event=None)

        super_payable_amount = PayEvent.compute_super_payable(
            super_ote_amount, employee.super_rate_pct)

        pe = PayEvent(
            user_id=user_id,
            employee_id=employee.id,
            payment_date=payment_date,
            pay_period_start=pay_period_start,
            pay_period_end=pay_period_end,
            pay_frequency=employee.pay_frequency or 'weekly',
            position_snapshot=employee.position,
            gross_amount=gross_amount,
            payg_tax_amount=payg_tax_amount,
            net_amount=net_amount,
            super_ote_amount=super_ote_amount,
            super_payable_amount=super_payable_amount,
            bank_reference=(request.form.get('bank_reference') or '').strip() or None,
            notes=(request.form.get('notes') or '').strip() or None,
            status='draft',
        )
        db.session.add(pe)
        db.session.commit()
        flash('Pay event created (draft)', 'success')
        return redirect(url_for('payroll_hmi.payroll_pay_event_detail',
                                 pay_event_id=pe.id))

    return render_template('payroll/payroll_pay_event_form.html',
                           employees=employees, employee=employee, event=None)


@payroll_hmi_bp.route('/pay-events/<pay_event_id>')
@login_required
def payroll_pay_event_detail(pay_event_id):
    user_id = session['user_id']
    pe = _pay_event_or_404(user_id, pay_event_id)
    if pe is None:
        flash('Pay event not found', 'error')
        return redirect(url_for('payroll_hmi.payroll_pay_events_list'))

    from app.models.pay_event_line import PayEventLine
    from app.models.payslip_delivery import PayslipDelivery
    lines = (PayEventLine.query
             .filter_by(pay_event_id=pe.id)
             .order_by(PayEventLine.sort_order.asc()).all())
    deliveries = (PayslipDelivery.query
                  .filter_by(pay_event_id=pe.id)
                  .order_by(PayslipDelivery.sent_at.desc()).all())
    employee = Employee.query.get(pe.employee_id)

    return render_template('payroll/payroll_pay_event_detail.html',
                           event=pe, employee=employee,
                           lines=lines, deliveries=deliveries)


@payroll_hmi_bp.route('/pay-events/<pay_event_id>/edit', methods=['GET', 'POST'])
@login_required
def payroll_pay_event_edit(pay_event_id):
    user_id = session['user_id']
    pe = _pay_event_or_404(user_id, pay_event_id)
    if pe is None:
        flash('Pay event not found', 'error')
        return redirect(url_for('payroll_hmi.payroll_pay_events_list'))
    if pe.status != 'draft':
        flash(f'Only draft events can be edited (status={pe.status})', 'error')
        return redirect(url_for('payroll_hmi.payroll_pay_event_detail',
                                 pay_event_id=pe.id))
    employee = Employee.query.get(pe.employee_id)
    employees = Employee.query.filter_by(user_id=user_id).order_by(Employee.legal_name).all()

    if request.method == 'POST':
        for attr in ('payment_date', 'pay_period_start', 'pay_period_end'):
            v = request.form.get(attr)
            if v:
                try:
                    setattr(pe, attr, date.fromisoformat(v))
                except ValueError:
                    pass
        for attr in ('gross_amount', 'payg_tax_amount', 'net_amount',
                     'super_ote_amount', 'super_payable_amount'):
            v = request.form.get(attr)
            if v:
                try:
                    setattr(pe, attr, Decimal(v))
                except Exception:
                    pass
        for attr in ('pay_frequency', 'position_snapshot', 'bank_reference', 'notes'):
            v = request.form.get(attr)
            if v is not None:
                setattr(pe, attr, (v.strip() or None))
        db.session.commit()
        flash('Pay event updated', 'success')
        return redirect(url_for('payroll_hmi.payroll_pay_event_detail',
                                 pay_event_id=pe.id))

    return render_template('payroll/payroll_pay_event_form.html',
                           employees=employees, employee=employee, event=pe)


# ===========================================================================
# Super payments
# ===========================================================================

@payroll_hmi_bp.route('/super-payments')
@login_required
def payroll_super_payments_list():
    user_id = session['user_id']
    rows = (SuperPayment.query
            .filter_by(user_id=user_id)
            .order_by(SuperPayment.remittance_date.desc()).all())
    employees = Employee.query.filter_by(user_id=user_id).all()
    emp_lookup = {e.id: e for e in employees}
    for sp in rows:
        sp._employee = emp_lookup.get(sp.employee_id)
    return render_template('payroll/payroll_super_payments.html',
                           super_payments=rows)


@payroll_hmi_bp.route('/super-payments/new', methods=['GET', 'POST'])
@login_required
def payroll_super_payment_new():
    user_id = session['user_id']
    employees = Employee.query.filter_by(user_id=user_id).order_by(Employee.legal_name).all()

    if request.method == 'POST':
        employee_id = request.form.get('employee_id') or ''
        employee = next((e for e in employees if e.id == employee_id), None)
        if employee is None:
            flash('Employee is required', 'error')
            return render_template('payroll/payroll_super_payment_form.html',
                                   employees=employees, super_payment=None,
                                   pay_events=[])
        try:
            remittance_date = date.fromisoformat(request.form['remittance_date'])
            amount = Decimal(request.form.get('amount') or '0')
        except (KeyError, ValueError) as exc:
            flash(f'Invalid input: {exc}', 'error')
            return render_template('payroll/payroll_super_payment_form.html',
                                   employees=employees, super_payment=None,
                                   pay_events=[])

        pay_event_ids = request.form.getlist('pay_event_ids')

        sp = SuperPayment(
            user_id=user_id,
            employee_id=employee.id,
            remittance_date=remittance_date,
            amount=amount,
            pay_event_ids=pay_event_ids,
            fund_name_snapshot=employee.super_fund_name,
            fund_member_snapshot=employee.super_fund_member_no,
            payment_reference=(request.form.get('payment_reference') or '').strip() or None,
            notes=(request.form.get('notes') or '').strip() or None,
            status='pending',
        )
        db.session.add(sp)
        db.session.commit()
        flash('Super payment recorded', 'success')
        return redirect(url_for('payroll_hmi.payroll_super_payment_detail',
                                 super_payment_id=sp.id))

    pay_events = []
    employee_id = request.args.get('employee_id')
    if employee_id:
        pay_events = (PayEvent.query
                      .filter_by(user_id=user_id, employee_id=employee_id)
                      .order_by(PayEvent.payment_date.desc())
                      .limit(20).all())
    return render_template('payroll/payroll_super_payment_form.html',
                           employees=employees, super_payment=None,
                           pay_events=pay_events)


@payroll_hmi_bp.route('/super-payments/<super_payment_id>')
@login_required
def payroll_super_payment_detail(super_payment_id):
    user_id = session['user_id']
    sp = _super_payment_or_404(user_id, super_payment_id)
    if sp is None:
        flash('Super payment not found', 'error')
        return redirect(url_for('payroll_hmi.payroll_super_payments_list'))
    employee = Employee.query.get(sp.employee_id)
    covered_events = []
    if sp.pay_event_ids:
        covered_events = (PayEvent.query
                          .filter(PayEvent.id.in_(sp.pay_event_ids))
                          .all())
    return render_template('payroll/payroll_super_payment_detail.html',
                           super_payment=sp, employee=employee,
                           covered_events=covered_events)


@payroll_hmi_bp.route('/super-payments/<super_payment_id>/mark-paid', methods=['POST'])
@login_required
def payroll_super_payment_mark_paid(super_payment_id):
    user_id = session['user_id']
    sp = _super_payment_or_404(user_id, super_payment_id)
    if sp is None:
        flash('Super payment not found', 'error')
        return redirect(url_for('payroll_hmi.payroll_super_payments_list'))
    if sp.status != 'pending':
        flash(f'Cannot mark paid (status={sp.status})', 'error')
        return redirect(url_for('payroll_hmi.payroll_super_payment_detail',
                                 super_payment_id=sp.id))
    sp.status = 'paid'
    if sp.pay_event_ids:
        events = (PayEvent.query
                  .filter(PayEvent.user_id == user_id,
                          PayEvent.id.in_(sp.pay_event_ids))
                  .all())
        if events:
            share = (sp.amount / Decimal(len(events))).quantize(Decimal('0.01'))
            for ev in events:
                ev.super_paid_amount = share
                ev.super_paid_date = sp.remittance_date
    db.session.commit()
    flash('Super payment marked paid', 'success')
    return redirect(url_for('payroll_hmi.payroll_super_payment_detail',
                             super_payment_id=sp.id))

# ---------------------------------------------------------------------------
# Payroll exports (added 2026-09-17)
#
# Two downloads that hit the API endpoints at /api/exports/payroll/*.
# Kept as GET forms (not POST) because each "button" is just a filtered
# download — there's no state mutation, and a GET URL is shareable for
# troubleshooting. The form just builds the right query string.
# ---------------------------------------------------------------------------

@payroll_hmi_bp.route('/exports', methods=['GET'])
@login_required
def payroll_exports():
    """Render the payroll exports page with two download buttons.

    The page shows a status filter (defaults to 'finalized' — i.e. pending
    payroll), an optional date range, and links to the two API endpoints.
    Each link embeds the current filter selection.

    Download links are constructed as GET requests to:
      /api/exports/payroll/aba?status=...&from_date=...&to_date=...&process_date=...
      /api/exports/payroll/super-csv?status=...&from_date=...&to_date=...
    """
    user_id = session['user_id']

    # Read current filter selection from the query string
    statuses_raw = request.args.get('status', 'finalized')
    from_str = request.args.get('from_date', '').strip()
    to_str = request.args.get('to_date', '').strip()
    process_str = request.args.get('process_date', '').strip() or date.today().isoformat()

    statuses = [s.strip() for s in statuses_raw.split(',') if s.strip()]

    # Build the live preview (counts + totals) for the selected filter.
    # We replicate the API's filter logic here in a single query rather than
    # calling the API endpoint (which would need an API key).
    q = PayEvent.query.filter(
        PayEvent.user_id == user_id,
        PayEvent.status.in_(statuses),
    )
    if from_str:
        try:
            q = q.filter(PayEvent.payment_date >= date.fromisoformat(from_str))
        except ValueError:
            pass
    if to_str:
        try:
            q = q.filter(PayEvent.payment_date <= date.fromisoformat(to_str))
        except ValueError:
            pass
    events = q.order_by(PayEvent.payment_date.asc()).all()

    employees = {e.id: e for e in Employee.query.all()}
    total_net = sum((Decimal(str(ev.net_amount)) for ev in events), Decimal('0'))
    total_gross = sum((Decimal(str(ev.gross_amount)) for ev in events), Decimal('0'))
    total_super = sum((Decimal(str(ev.super_payable_amount)) for ev in events), Decimal('0'))

    # Check whether the user can actually export anything (bank details +
    # super fund on each employee). The HMI shows the validation upfront so
    # users don't download an empty file.
    user = Employee.query.first()  # placeholder — we need the User row
    from app.models.user import User
    user_row = User.query.get(user_id)
    has_business_bank = bool(user_row.bsb_plain and user_row.account_number_plain)

    employees_missing_bank = sum(
        1 for ev in events
        if not employees.get(ev.employee_id)
        or not employees[ev.employee_id].bank_bsb_plain
    )
    employees_missing_super = sum(
        1 for ev in events
        if not employees.get(ev.employee_id)
        or not employees[ev.employee_id].super_fund_member_no
    )

    # Build the download URLs (relative to the app root, not the payroll prefix).
    aba_query = {'status': statuses_raw}
    if from_str:
        aba_query['from_date'] = from_str
    if to_str:
        aba_query['to_date'] = to_str
    if process_str:
        aba_query['process_date'] = process_str
    super_query = {'status': statuses_raw}
    if from_str:
        super_query['from_date'] = from_str
    if to_str:
        super_query['to_date'] = to_str
    aba_query_string = '&'.join(f'{k}={v}' for k, v in aba_query.items())
    super_query_string = '&'.join(f'{k}={v}' for k, v in super_query.items())

    return render_template(
        'payroll/payroll_exports.html',
        statuses_raw=statuses_raw,
        from_date=from_str,
        to_date=to_str,
        process_date=process_str,
        pay_event_count=len(events),
        distinct_employees=len({ev.employee_id for ev in events}),
        total_net=total_net,
        total_gross=total_gross,
        total_super=total_super,
        has_business_bank=has_business_bank,
        employees_missing_bank=employees_missing_bank,
        employees_missing_super=employees_missing_super,
        aba_query_string=aba_query_string,
        super_query_string=super_query_string,
    )
