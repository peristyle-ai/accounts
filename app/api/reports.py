from flask import Blueprint, request, jsonify
from datetime import date, datetime, timezone
from calendar import monthrange

from app.models import db
from app.models.expense import Expense
from app.models.invoice import Invoice
from app.models.account_category import AccountCategory
from app.shared.decorators import api_key_required

reports_bp = Blueprint('reports', __name__, url_prefix='/api/reports')


def get_aus_financial_year_dates(year):
    start_date = date(year, 7, 1)
    end_date = date(year + 1, 6, 30)
    return start_date, end_date


def get_quarter_dates(year, quarter):
    if quarter == 1:
        return date(year, 7, 1), date(year, 9, 30)
    elif quarter == 2:
        return date(year, 10, 1), date(year, 12, 31)
    elif quarter == 3:
        return date(year + 1, 1, 1), date(year + 1, 3, 31)
    else:
        return date(year + 1, 4, 1), date(year + 1, 6, 30)


@reports_bp.route('/profit-loss', methods=['GET'])
@api_key_required
def profit_loss_report():
    user = request.current_user

    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')

    if not start_date_str or not end_date_str:
        return jsonify({'error': 'start_date and end_date are required'}), 400

    try:
        start_date = date.fromisoformat(start_date_str)
        end_date = date.fromisoformat(end_date_str)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400

    expenses = Expense.query.filter(
        Expense.user_id == user.id,
        Expense.expense_date >= start_date,
        Expense.expense_date <= end_date
    ).all()

    invoices = Invoice.query.filter(
        Invoice.user_id == user.id,
        Invoice.invoice_date >= start_date,
        Invoice.invoice_date <= end_date
    ).all()

    total_expenses_ex_gst = sum(e.ex_gst_amount for e in expenses)
    total_expenses_gst = sum(e.gst_amount for e in expenses)
    total_expenses = sum(e.total_amount for e in expenses)

    total_income_ex_gst = sum(i.ex_gst_amount for i in invoices)
    total_income_gst = sum(i.gst_amount for i in invoices)
    total_income = sum(i.total_amount for i in invoices)

    net_profit_ex_gst = total_income_ex_gst - total_expenses_ex_gst
    net_gst_collected = total_income_gst - total_expenses_gst

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

    return jsonify({
        'report_period': {
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat()
        },
        'expenses': {
            'ex_gst': str(total_expenses_ex_gst),
            'gst': str(total_expenses_gst),
            'total': str(total_expenses)
        },
        'income': {
            'ex_gst': str(total_income_ex_gst),
            'gst': str(total_income_gst),
            'total': str(total_income)
        },
        'summary': {
            'net_profit_ex_gst': str(net_profit_ex_gst),
            'gst_collected_less_paid': str(net_gst_collected),
            'total_income': str(total_income),
            'total_expenses': str(total_expenses)
        },
        'by_category': by_category
    }), 200


@reports_bp.route('/quarterly-bas', methods=['GET'])
@api_key_required
def quarterly_bas_report():
    user = request.current_user

    year = request.args.get('year', type=int)
    quarter = request.args.get('quarter', type=int)

    if not year or not quarter:
        current_date = date.today()
        if current_date.month >= 7:
            year = current_date.year
        else:
            year = current_date.year - 1
        if current_date.month <= 3:
            quarter = 3
        elif current_date.month <= 6:
            quarter = 4
        elif current_date.month <= 9:
            quarter = 1
        else:
            quarter = 2

    if quarter < 1 or quarter > 4:
        return jsonify({'error': 'Quarter must be 1-4'}), 400

    start_date, end_date = get_quarter_dates(year, quarter)

    expenses = Expense.query.filter(
        Expense.user_id == user.id,
        Expense.expense_date >= start_date,
        Expense.expense_date <= end_date
    ).all()

    invoices = Invoice.query.filter(
        Invoice.user_id == user.id,
        Invoice.invoice_date >= start_date,
        Invoice.invoice_date <= end_date
    ).all()

    total_gst_collected = sum(float(i.gst_amount) for i in invoices)
    total_gst_paid = sum(float(e.gst_amount) for e in expenses)
    gst_owing = total_gst_collected - total_gst_paid

    total_income_ex_gst = sum(float(i.ex_gst_amount) for i in invoices)
    total_expenses_ex_gst = sum(float(e.ex_gst_amount) for e in expenses)

    return jsonify({
        'bas_period': {
            'year': year,
            'quarter': quarter,
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat()
        },
        'gst_collected': str(total_gst_collected),
        'gst_paid': str(total_gst_paid),
        'gst_owing': str(gst_owing),
        'total_income_ex_gst': str(total_income_ex_gst),
        'total_expenses_ex_gst': str(total_expenses_ex_gst),
        'net_gst': str(gst_owing)
    }), 200


@reports_bp.route('/monthly', methods=['GET'])
@api_key_required
def monthly_report():
    user = request.current_user

    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)

    if not year or not month:
        current_date = date.today()
        year = current_date.year
        month = current_date.month

    if month < 1 or month > 12:
        return jsonify({'error': 'Month must be 1-12'}), 400

    _, last_day = monthrange(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)

    expenses = Expense.query.filter(
        Expense.user_id == user.id,
        Expense.expense_date >= start_date,
        Expense.expense_date <= end_date
    ).all()

    invoices = Invoice.query.filter(
        Invoice.user_id == user.id,
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

    return jsonify({
        'report_period': {
            'year': year,
            'month': month,
            'month_name': month_names[month],
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat()
        },
        'expenses': {
            'ex_gst': str(total_expenses_ex_gst),
            'gst': str(total_expenses_gst),
            'total': str(total_expenses)
        },
        'income': {
            'ex_gst': str(total_income_ex_gst),
            'gst': str(total_income_gst),
            'total': str(total_income)
        },
        'summary': {
            'net_profit_ex_gst': str(net_profit_ex_gst),
            'gst_owing': str(gst_owing),
            'total_income': str(total_income),
            'total_expenses': str(total_expenses)
        },
        'by_category': by_category
    }), 200


@reports_bp.route('/yearly-finances', methods=['GET'])
@api_key_required
def yearly_finances_report():
    user = request.current_user

    year = request.args.get('year', type=int)

    if not year:
        current_date = date.today()
        if current_date.month >= 7:
            year = current_date.year
        else:
            year = current_date.year - 1

    start_date, end_date = get_aus_financial_year_dates(year)

    expenses = Expense.query.filter(
        Expense.user_id == user.id,
        Expense.expense_date >= start_date,
        Expense.expense_date <= end_date
    ).all()

    invoices = Invoice.query.filter(
        Invoice.user_id == user.id,
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
            'start_date': q_start.isoformat(),
            'end_date': q_end.isoformat(),
            'total_income_ex_gst': str(sum(float(i.ex_gst_amount) for i in q_invoices)),
            'total_expenses_ex_gst': str(sum(float(e.ex_gst_amount) for e in q_expenses)),
            'gst_collected': str(sum(float(i.gst_amount) for i in q_invoices)),
            'gst_paid': str(sum(float(e.gst_amount) for e in q_expenses)),
            'gst_owing': str(sum(float(i.gst_amount) for i in q_invoices) - sum(float(e.gst_amount) for e in q_expenses))
        })

    total_expenses_ex_gst = sum(float(e.ex_gst_amount) for e in expenses)
    total_expenses_gst = sum(float(e.gst_amount) for e in expenses)
    total_expenses = sum(float(e.total_amount) for e in expenses)

    total_income_ex_gst = sum(float(i.ex_gst_amount) for i in invoices)
    total_income_gst = sum(float(i.gst_amount) for i in invoices)
    total_income = sum(float(i.total_amount) for i in invoices)

    net_profit_ex_gst = total_income_ex_gst - total_expenses_ex_gst
    total_gst_owing = total_income_gst - total_expenses_gst

    return jsonify({
        'financial_year': {
            'year': year,
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat()
        },
        'totals': {
            'total_income_ex_gst': str(total_income_ex_gst),
            'total_income_gst': str(total_income_gst),
            'total_income': str(total_income),
            'total_expenses_ex_gst': str(total_expenses_ex_gst),
            'total_expenses_gst': str(total_expenses_gst),
            'total_expenses': str(total_expenses),
            'net_profit_ex_gst': str(net_profit_ex_gst),
            'total_gst_owing': str(total_gst_owing)
        },
        'quarterly': quarterly_data
    }), 200
