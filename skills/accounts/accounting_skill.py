"""
Accounting Skill for AI Agents

This skill provides a structured interface for AI agents to interact with the
accounting database. It wraps the REST API to provide natural language access
to expense, invoice, customer, business profile, bank transaction, BAS
lodgement, statement of account, payroll, and OCR queue management.

Usage:
    from skills.accounts.accounting_skill import AccountingSkill
    skill = AccountingSkill(api_key="your-api-key")  # base_url from ACCOUNTS_BASE_URL
    result = skill.create_expense(vendor_name="Office Supplies", ex_gst_amount=100.00)

Documentation:
    SKILL.md is a lean overview. Feature-specific workflows live in:
      - BAS.md               - Business Activity Statement lodgement tracking
      - bank-transactions.md - Recording received payments with full bank provenance
      - statements.md        - Statement of Account (PDF + JSON) for customers
      - payroll.md           - Employees, pay events, payslip delivery, super payments
      - ocr-queue.md         - OCR pipeline and manual review workflow
"""

import os
import requests
from typing import Optional, List, Dict, Any
from datetime import date, datetime
from decimal import Decimal


class AccountingSkill:
    """Thin client for the accounts REST API.

    The base URL is REQUIRED -- there is no built-in default. A silent
    fallback to http://127.0.0.1:5061 meant a misconfigured deployment
    quietly talked to nothing (or to whatever happened to be on that
    port) instead of saying so. Set ACCOUNTS_BASE_URL, or pass
    base_url= to the constructor.
    """

    BASE_URL: Optional[str] = None

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        self.api_key = api_key
        resolved = base_url or os.environ.get("ACCOUNTS_BASE_URL")
        if not resolved:
            raise RuntimeError(
                "ACCOUNTS_BASE_URL is not set and no base_url was given. "
                "Set ACCOUNTS_BASE_URL to the accounts API base URL, e.g. "
                "http://127.0.0.1:5061, or pass base_url= to AccountingSkill()."
            )
        self.BASE_URL = resolved.rstrip('/')
        self.headers = {
            'X-API-Key': api_key,
            'Content-Type': 'application/json'
        }

    def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None, files: Optional[Dict] = None, stream: bool = False):
        url = f"{self.BASE_URL}{endpoint}"

        if files:
            headers = {'X-API-Key': self.api_key}
            response = requests.request(method, url, headers=headers, files=files)
        else:
            response = requests.request(method, url, headers=self.headers, json=data)

        response.raise_for_status()

        if stream:
            return response
        return response.json()

    def download_file(self, endpoint: str, save_path: str) -> str:
        """
        Download a file from the API.

        Args:
            endpoint: API endpoint that returns a file
            save_path: Local path to save the file

        Returns:
            Path to the saved file
        """
        url = f"{self.BASE_URL}{endpoint}"
        headers = {'X-API-Key': self.api_key}
        response = requests.get(url, headers=headers, stream=True)
        response.raise_for_status()

        os.makedirs(os.path.dirname(save_path) if os.path.dirname(save_path) else '.', exist_ok=True)
        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        return save_path

    # =========================================================================
    # Business Profile Management
    # =========================================================================

    def get_business_details(self) -> Dict[str, Any]:
        """
        Get the current business profile (user details).

        Returns:
            Dictionary containing the user/business profile
        """
        result = self._make_request('GET', '/api/auth/business')
        return result['user']

    def update_business_details(
        self,
        business_name: Optional[str] = None,
        abn: Optional[str] = None,
        address: Optional[str] = None,
        contact_email: Optional[str] = None,
        contact_number: Optional[str] = None,
        bank_name: Optional[str] = None,
        account_name: Optional[str] = None,
        account_number: Optional[str] = None,
        bsb: Optional[str] = None,
        payment_terms: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Create or update business profile details. These appear on invoice PDFs.

        Bank details can be set/updated using the bank_name, account_name,
        account_number, and bsb parameters. To clear a field, pass an empty
        string ("").

        Args:
            business_name: Business name
            abn: Australian Business Number
            address: Business address (can include newlines)
            contact_email: Business contact email
            contact_number: Business contact phone number
            bank_name: Bank name
            account_name: Account holder name
            account_number: Bank account number
            bsb: Bank State Branch (BSB) number
            payment_terms: Number of days for payment (default 14). This is
                used in the "Payment terms: Net X days" line on invoice PDFs
                and should be consistent with the invoice due date.

        Returns:
            Dictionary containing the updated user/business profile
        """
        data = {}
        if business_name is not None:
            data['business_name'] = business_name
        if abn is not None:
            data['abn'] = abn
        if address is not None:
            data['address'] = address
        if contact_email is not None:
            data['contact_email'] = contact_email
        if contact_number is not None:
            data['contact_number'] = contact_number
        if bank_name is not None:
            data['bank_name'] = bank_name
        if account_name is not None:
            data['account_name'] = account_name
        if account_number is not None:
            data['account_number'] = account_number
        if bsb is not None:
            data['bsb'] = bsb
        if payment_terms is not None:
            data['payment_terms'] = payment_terms

        result = self._make_request('PUT', '/api/auth/business', data)
        return result['user']

    def upload_logo(self, file_path: str) -> Dict[str, Any]:
        """
        Upload a business logo. The logo appears on the top-left of invoice PDFs.

        Args:
            file_path: Local path to the logo file (PNG, JPG, JPEG, GIF, WEBP, SVG)

        Returns:
            Dictionary containing the updated user profile with logo path
        """
        with open(file_path, 'rb') as f:
            files = {'file': f}
            result = self._make_request('POST', '/api/auth/logo', files=files)
        return result['user']

    def delete_logo(self) -> Dict[str, Any]:
        """
        Delete the business logo.

        Returns:
            Dictionary with confirmation message
        """
        return self._make_request('DELETE', '/api/auth/logo')

    # =========================================================================
    # Expense Management
    # =========================================================================

    def create_expense(
        self,
        vendor_name: str,
        amount: float,
        expense_date: str,
        gst_type: float = 0.1,
        amount_type: str = "excludes",
        currency: str = "AUD",
        description: str = "",
        account_category_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create a new expense record.

        Args:
            vendor_name: Name of the vendor/supplier
            amount: Amount (excludes or includes GST depending on amount_type)
            expense_date: Date of expense in YYYY-MM-DD format
            gst_type: GST type (0 for no GST, 0.1 for 10% GST)
            amount_type: 'excludes' (default) or 'includes' GST
            currency: 'AUD' (default) or 'USD'
            description: Optional description of the expense
            account_category_id: Optional UUID of the account category

        Returns:
            Dictionary containing the created expense data
        """
        data = {
            'vendor_name': vendor_name,
            'amount': str(amount),
            'amount_type': amount_type,
            'currency': currency,
            'expense_date': expense_date,
            'gst_type': str(gst_type),
            'description': description
        }
        if account_category_id:
            data['account_category_id'] = account_category_id

        result = self._make_request('POST', '/api/expenses', data)
        return result['expense']

    def get_expense(self, expense_id: str) -> Dict[str, Any]:
        """Get a single expense by ID."""
        result = self._make_request('GET', f'/api/expenses/{expense_id}')
        return result['expense']

    def list_expenses(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        List all expenses, optionally filtered by date range.

        Args:
            start_date: Start date filter in YYYY-MM-DD format
            end_date: End date filter in YYYY-MM-DD format

        Returns:
            List of expense dictionaries
        """
        params = []
        if start_date:
            params.append(f'start_date={start_date}')
        if end_date:
            params.append(f'end_date={end_date}')

        query = '?' + '&'.join(params) if params else ''
        result = self._make_request('GET', f'/api/expenses{query}')
        return result['expenses']

    def update_expense(
        self,
        expense_id: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Update an existing expense.

        Args:
            expense_id: UUID of the expense to update
            **kwargs: Any expense fields to update (vendor_name, amount, amount_type, currency, gst_type, etc.)

        Returns:
            Dictionary containing the updated expense data
        """
        data = {}
        for key, value in kwargs.items():
            if key in ['vendor_name', 'description', 'expense_date', 'account_category_id', 'amount_type', 'currency']:
                data[key] = value
            elif key in ['amount', 'ex_gst_amount', 'gst_type']:
                data[key] = str(value)

        result = self._make_request('PUT', f'/api/expenses/{expense_id}', data)
        return result['expense']

    def delete_expense(self, expense_id: str) -> Dict[str, Any]:
        """Delete an expense by ID."""
        return self._make_request('DELETE', f'/api/expenses/{expense_id}')

    def upload_expense_attachment(self, expense_id: str, file_path: str) -> Dict[str, Any]:
        """
        Upload an attachment (image or PDF) to an expense.

        Args:
            expense_id: UUID of the expense
            file_path: Local path to the file to upload

        Returns:
            Dictionary containing the updated expense with attachment path
        """
        with open(file_path, 'rb') as f:
            files = {'file': f}
            result = self._make_request('POST', f'/api/expenses/{expense_id}/upload', files=files)
        return result['expense']

    # =========================================================================
    # Customer Management
    # =========================================================================

    def create_customer(
        self,
        name: str,
        contact_name: Optional[str] = None,
        address: Optional[str] = None,
        contact_email: Optional[str] = None,
        abn: Optional[str] = None,
        contact_number: Optional[str] = None,
        gst: bool = True
    ) -> Dict[str, Any]:
        """
        Create a new customer.

        Args:
            name: Customer/business name (required)
            contact_name: Contact person name
            address: Customer address
            contact_email: Contact email
            abn: Australian Business Number
            contact_number: Phone number
            gst: Whether customer is GST registered (default True)

        Returns:
            Dictionary containing the created customer
        """
        data = {
            'name': name,
            'gst': gst
        }
        if contact_name is not None:
            data['contact_name'] = contact_name
        if address is not None:
            data['address'] = address
        if contact_email is not None:
            data['contact_email'] = contact_email
        if abn is not None:
            data['abn'] = abn
        if contact_number is not None:
            data['contact_number'] = contact_number

        result = self._make_request('POST', '/api/customers', data)
        return result['customer']

    def get_customer(self, customer_id: str) -> Dict[str, Any]:
        """Get a single customer by ID."""
        result = self._make_request('GET', f'/api/customers/{customer_id}')
        return result['customer']

    def list_customers(self) -> List[Dict[str, Any]]:
        """List all customers, sorted by name."""
        result = self._make_request('GET', '/api/customers')
        return result['customers']

    def update_customer(self, customer_id: str, **kwargs) -> Dict[str, Any]:
        """
        Update an existing customer.

        Args:
            customer_id: UUID of the customer
            **kwargs: Customer fields to update (name, contact_name, address, etc.)

        Returns:
            Dictionary containing the updated customer
        """
        result = self._make_request('PUT', f'/api/customers/{customer_id}', kwargs)
        return result['customer']

    def delete_customer(self, customer_id: str) -> Dict[str, Any]:
        """
        Delete a customer. Fails if the customer has any invoices.

        Args:
            customer_id: UUID of the customer

        Returns:
            Dictionary with confirmation message
        """
        return self._make_request('DELETE', f'/api/customers/{customer_id}')

    # =========================================================================
    # Invoice Management (requires customer_id)
    # =========================================================================

    def create_invoice(
        self,
        customer_id: str,
        client_name: str,
        ex_gst_amount: float,
        invoice_date: str,
        gst_type: float = 0.1,
        description: str = "",
        due_date: Optional[str] = None,
        account_category_id: Optional[str] = None,
        status: str = "draft",
        payment_date: Optional[str] = None,
        amount_paid: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Create a new invoice. Requires an existing customer_id.

        Args:
            customer_id: UUID of the customer (REQUIRED)
            client_name: Display name for the client on the invoice
            ex_gst_amount: Amount excluding GST
            invoice_date: Date of invoice in YYYY-MM-DD format
            gst_type: GST type (0 for no GST, 0.1 for 10% GST)
            description: Optional description
            due_date: Optional payment due date in YYYY-MM-DD format
            account_category_id: Optional UUID of the account category
            status: Invoice status (default 'draft'). One of: draft, sent, paid, overdue, cancelled
            payment_date: Date payment was received (YYYY-MM-DD)
            amount_paid: Amount that was paid (for partial payments)

        Returns:
            Dictionary containing the created invoice data
        """
        data = {
            'customer_id': customer_id,
            'client_name': client_name,
            'ex_gst_amount': str(ex_gst_amount),
            'invoice_date': invoice_date,
            'gst_type': str(gst_type),
            'description': description,
            'status': status
        }
        if due_date:
            data['due_date'] = due_date
        if account_category_id:
            data['account_category_id'] = account_category_id
        if payment_date:
            data['payment_date'] = payment_date
        if amount_paid is not None:
            data['amount_paid'] = str(amount_paid)

        result = self._make_request('POST', '/api/invoices', data)
        return result['invoice']

    def get_invoice(self, invoice_id: str) -> Dict[str, Any]:
        """Get a single invoice by ID."""
        result = self._make_request('GET', f'/api/invoices/{invoice_id}')
        return result['invoice']

    def list_invoices(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        List all invoices, optionally filtered by date range.

        Args:
            start_date: Start date filter in YYYY-MM-DD format
            end_date: End date filter in YYYY-MM-DD format

        Returns:
            List of invoice dictionaries
        """
        params = []
        if start_date:
            params.append(f'start_date={start_date}')
        if end_date:
            params.append(f'end_date={end_date}')

        query = '?' + '&'.join(params) if params else ''
        result = self._make_request('GET', f'/api/invoices{query}')
        return result['invoices']

    def update_invoice(
        self,
        invoice_id: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Update an existing invoice.

        Args:
            invoice_id: UUID of the invoice to update
            **kwargs: Any invoice fields to update. Supported fields:
                customer_id, client_name, description, invoice_date, due_date,
                account_category_id, status, payment_date, amount_paid,
                sent_at, confirmed_received_at, paid_at,
                ex_gst_amount, gst_type

        Returns:
            Dictionary containing the updated invoice data
        """
        data = {}
        for key, value in kwargs.items():
            if key in ['customer_id', 'client_name', 'description', 'invoice_date', 'due_date', 'account_category_id', 'status', 'payment_date', 'sent_at', 'confirmed_received_at', 'paid_at']:
                data[key] = value
            elif key in ['ex_gst_amount', 'gst_type', 'amount_paid']:
                data[key] = str(value)

        result = self._make_request('PUT', f'/api/invoices/{invoice_id}', data)
        return result['invoice']

    def mark_invoice_paid(
        self,
        invoice_id: str,
        payment_date: Optional[str] = None,
        amount_paid: Optional[float] = None,
        payment_reference: Optional[str] = None,
        payment_method: Optional[str] = None,
        paid_at: Optional[str] = None,
        notes: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Mark an invoice as paid via POST /api/invoices/<id>/mark-paid.
        Sets status='paid', payment_date, amount_paid (defaults to total_amount),
        and paid_at (custom ISO 8601, or now UTC if omitted).

        2026-10-07 patch: the API now honours payment_reference, payment_method,
        notes, and a custom paid_at. These are persisted on the invoice row so
        the bank-side provenance (NAB ref, OSKO ID, batch context) carries
        forward into BAS/Xero exports and reports.

        Args:
            invoice_id: UUID of the invoice
            payment_date: Date payment was received (YYYY-MM-DD). Defaults to today.
            amount_paid: Amount paid. Defaults to invoice total_amount (full payment).
            payment_reference: Free-form bank-side ref (NAB ref, OSKO ID, etc).
                               Max 100 chars. Stored in invoices.payment_reference.
            payment_method:    'nab_osko' | 'transfer_credit' | 'direct_debit' | 'cheque' | …
                               Max 50 chars. Stored in invoices.payment_method.
            paid_at:           Custom ISO 8601 timestamp for when funds cleared
                               (e.g. '2026-10-07T14:28:00+10:00'). Defaults to now (UTC).
            notes:             Free-form payment context (e.g. 'OSKO batch 07 Oct 14:28 AEST').
                               Stored in invoices.notes.

        Returns:
            Dictionary containing the updated invoice data
        """
        data: Dict[str, Any] = {}
        if payment_date:
            data['payment_date'] = payment_date
        if amount_paid is not None:
            data['amount_paid'] = str(amount_paid)
        if payment_reference is not None:
            data['payment_reference'] = payment_reference
        if payment_method is not None:
            data['payment_method'] = payment_method
        if paid_at is not None:
            data['paid_at'] = paid_at
        if notes is not None:
            data['notes'] = notes

        result = self._make_request('POST', f'/api/invoices/{invoice_id}/mark-paid', data)
        return result['invoice']

    def mark_invoice_sent(
        self,
        invoice_id: str,
        sent_at: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Mark an invoice as sent. Sets status='sent' and records the sent_at
        timestamp (defaults to now).

        Args:
            invoice_id: UUID of the invoice
            sent_at: Custom ISO 8601 timestamp. Defaults to now (UTC).

        Returns:
            Dictionary containing the updated invoice data
        """
        data = {}
        if sent_at:
            data['sent_at'] = sent_at

        result = self._make_request('POST', f'/api/invoices/{invoice_id}/mark-sent', data)
        return result['invoice']

    def mark_invoice_confirmed(
        self,
        invoice_id: str,
        confirmed_received_at: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Mark an invoice as confirmed received. Sets the confirmed_received_at
        timestamp (defaults to now). Does not change the status.

        Args:
            invoice_id: UUID of the invoice
            confirmed_received_at: Custom ISO 8601 timestamp. Defaults to now (UTC).

        Returns:
            Dictionary containing the updated invoice data
        """
        data = {}
        if confirmed_received_at:
            data['confirmed_received_at'] = confirmed_received_at

        result = self._make_request('POST', f'/api/invoices/{invoice_id}/mark-confirmed', data)
        return result['invoice']

    def delete_invoice(self, invoice_id: str) -> Dict[str, Any]:
        """Delete an invoice by ID."""
        return self._make_request('DELETE', f'/api/invoices/{invoice_id}')

    def upload_invoice_attachment(self, invoice_id: str, file_path: str) -> Dict[str, Any]:
        """
        Upload an attachment (image or PDF) to an invoice.

        Args:
            invoice_id: UUID of the invoice
            file_path: Local path to the file to upload

        Returns:
            Dictionary containing the updated invoice with attachment path
        """
        with open(file_path, 'rb') as f:
            files = {'file': f}
            result = self._make_request('POST', f'/api/invoices/{invoice_id}/upload', files=files)
        return result['invoice']

    def download_invoice_pdf(self, invoice_id: str, save_path: str) -> str:
        """
        Download a generated PDF of an invoice.

        The PDF includes:
        - The user's business logo (top-left, if uploaded)
        - The user's business details (top-right)
        - Customer information (Bill To)
        - Invoice details, line items, totals
        - Payment terms footer

        Args:
            invoice_id: UUID of the invoice
            save_path: Local path to save the PDF (e.g. './invoice.pdf')

        Returns:
            Path to the saved PDF file
        """
        return self.download_file(f'/api/invoices/{invoice_id}/pdf', save_path)

    # =========================================================================
    # Account Categories
    # =========================================================================

    def create_account_category(
        self,
        name: str,
        description: str = ""
    ) -> Dict[str, Any]:
        """
        Create a new account category.

        Args:
            name: Name of the category
            description: Optional description

        Returns:
            Dictionary containing the created category data
        """
        data = {
            'name': name,
            'description': description
        }
        result = self._make_request('POST', '/api/account-categories', data)
        return result['account_category']

    def list_account_categories(self) -> List[Dict[str, Any]]:
        """List all account categories."""
        result = self._make_request('GET', '/api/account-categories')
        return result['account_categories']

    def update_account_category(self, category_id: str, **kwargs) -> Dict[str, Any]:
        """
        Update an existing account category.

        Args:
            category_id: UUID of the category
            **kwargs: Category fields to update (name, description)

        Returns:
            Dictionary containing the updated category
        """
        result = self._make_request('PUT', f'/api/account-categories/{category_id}', kwargs)
        return result['account_category']

    def delete_account_category(self, category_id: str) -> Dict[str, Any]:
        """Delete an account category by ID."""
        return self._make_request('DELETE', f'/api/account-categories/{category_id}')

    # =========================================================================
    # Reports
    # =========================================================================

    def get_profit_loss_report(
        self,
        start_date: str,
        end_date: str
    ) -> Dict[str, Any]:
        """
        Generate a Profit & Loss report for a date range.

        Args:
            start_date: Report start date in YYYY-MM-DD format
            end_date: Report end date in YYYY-MM-DD format

        Returns:
            Dictionary containing P&L data with income, expenses, GST, and category breakdown
        """
        params = f'start_date={start_date}&end_date={end_date}'
        return self._make_request('GET', f'/api/reports/profit-loss?{params}')

    def get_quarterly_bas_report(
        self,
        year: Optional[int] = None,
        quarter: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Generate a Quarterly BAS (Business Activity Statement) report.

        For Australian GST reporting.
        If year/quarter not provided, uses current period.

        Args:
            year: Financial year (e.g., 2024 for FY 2024-25)
            quarter: Quarter number (1-4)

        Returns:
            Dictionary containing GST collected, GST paid, and GST owing
        """
        params = []
        if year:
            params.append(f'year={year}')
        if quarter:
            params.append(f'quarter={quarter}')

        query = '?' + '&'.join(params) if params else ''
        return self._make_request('GET', f'/api/reports/quarterly-bas{query}')

    def get_monthly_report(
        self,
        year: Optional[int] = None,
        month: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Generate a Monthly P&L report.

        Args:
            year: Year (e.g., 2024)
            month: Month number (1-12)

        Returns:
            Dictionary containing monthly P&L data with income, expenses, GST, and category breakdown
        """
        params = []
        if year:
            params.append(f'year={year}')
        if month:
            params.append(f'month={month}')

        query = '?' + '&'.join(params) if params else ''
        return self._make_request('GET', f'/api/reports/monthly{query}')

    def get_yearly_finances_report(self, year: Optional[int] = None) -> Dict[str, Any]:
        """
        Generate a yearly financial report for the Australian financial year.

        Australian financial year runs July 1 to June 30.
        If year not provided, uses current financial year.

        Args:
            year: Start year of the financial year (e.g., 2024 for FY 2024-25)

        Returns:
            Dictionary containing yearly totals and quarterly breakdowns
        """
        query = f'?year={year}' if year else ''
        return self._make_request('GET', f'/api/reports/yearly-finances{query}')

    # =========================================================================
    # Statement of Account (see statements.md for workflow details)
    # =========================================================================

    def get_statement_json(self, customer_id: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
        """
        Get a Statement of Account for a customer as JSON.

        Returns all invoices (paid + outstanding) for the customer, plus
        totals and a 30/60/90/90+ aging breakdown of the outstanding balance.

        Args:
            customer_id: UUID of the customer
            as_of_date: Optional "statement as at" date in YYYY-MM-DD format.
                Defaults to today.

        Returns:
            Dictionary with:
              - customer: bill-to info
              - as_of_date: statement date
              - summary: {total_invoiced, total_paid, outstanding, aging{current,1_30,31_60,61_90,90_plus}}
              - invoices: list of {invoice_number, date, due_date, total, paid, outstanding, status, age_days}
              - payment_terms: net X days

        See statements.md for field-by-field reference and PDF download pattern.
        """
        params = f'?as_of={as_of_date}' if as_of_date else ''
        return self._make_request('GET', f'/api/customers/{customer_id}/statement.json{params}')

    def download_statement_pdf(self, customer_id: str, save_path: str, as_of_date: Optional[str] = None) -> str:
        """
        Download a Statement of Account PDF for a customer.

        Single-page A4 with business header (logo + business details + ABN) +
        bill-to block + summary tiles +
        5-bucket aging + invoice table with running balance + bank details +
        payment terms. Status colors: green=paid, red=overdue (sent AND past
        due), orange=sent.

        Args:
            customer_id: UUID of the customer
            save_path: Local path to save the PDF (e.g. './statement.pdf')
            as_of_date: Optional "statement as at" date in YYYY-MM-DD format

        Returns:
            Path to the saved PDF file
        """
        params = f'?as_of={as_of_date}' if as_of_date else ''
        return self.download_file(f'/api/customers/{customer_id}/statement.pdf{params}', save_path)

    # =========================================================================
    # Bank Transactions (see bank-transactions.md for full provenance fields)
    # =========================================================================

    def create_bank_transaction(
        self,
        amount: float,
        transaction_date: str,
        invoice_id: Optional[str] = None,
        transaction_id: Optional[str] = None,
        reference: Optional[str] = None,
        method: Optional[str] = None,
        payer_name: Optional[str] = None,
        payer_account: Optional[str] = None,
        currency: str = "AUD",
        settled_at: Optional[str] = None,
        notes: Optional[str] = None,
        raw_source: str = "manual",
    ) -> Dict[str, Any]:
        """
        Record a received bank transaction (with optional invoice link).

        If invoice_id is provided AND the invoice is sent/draft, the invoice
        is auto-marked paid (atomic — both succeed or both fail). If the
        invoice is already paid, just links the bank txn to it without
        mutating the invoice (backfill). If cancelled, 409.

        Args:
            amount: Amount received (always positive, e.g. 2131.80)
            transaction_date: Date the bank shows it as processed (YYYY-MM-DD).
                This is the BAS settlement date.
            invoice_id: Optional UUID of the invoice this payment is for
            transaction_id: Bank-side txn ID, e.g. 'CTBAAUSNXXXN...'
            reference: Payer-supplied reference, e.g. 'INV-XXX - SOFTWARE'
            method: One of 'osko' | 'bpay' | 'direct_credit' | 'cheque' | 'cash' | 'other'
            payer_name: Who sent the money, e.g. 'EXAMPLE CUSTOMER PTY LTD'
            payer_account: Payer-side account if known
            currency: ISO-4217 3-char, default 'AUD'
            settled_at: Precise ISO-8601 timestamp if known
            notes: Free-text notes
            raw_source: 'manual' | 'bank_feed_csv' | 'screenshot_ocr'. Default 'manual'.

        Returns:
            Dictionary with:
              - message: 'Bank transaction recorded' + (' and invoice marked paid' or ' (linked to already-paid invoice)')
              - bank_transaction: the new row
              - invoice: updated invoice (if invoice_id supplied)
        """
        data = {
            'amount': str(amount),
            'transaction_date': transaction_date,
            'currency': currency,
            'raw_source': raw_source,
        }
        if invoice_id is not None:
            data['invoice_id'] = invoice_id
        if transaction_id is not None:
            data['transaction_id'] = transaction_id
        if reference is not None:
            data['reference'] = reference
        if method is not None:
            data['method'] = method
        if payer_name is not None:
            data['payer_name'] = payer_name
        if payer_account is not None:
            data['payer_account'] = payer_account
        if settled_at is not None:
            data['settled_at'] = settled_at
        if notes is not None:
            data['notes'] = notes
        return self._make_request('POST', '/api/bank-transactions', data)

    def list_bank_transactions(
        self,
        invoice_id: Optional[str] = None,
        method: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """
        List bank transactions (filtered).

        Args:
            invoice_id: Optional - filter to one invoice
            method: Optional - filter to one method ('osko', 'bpay', etc.)
            date_from: Optional - inclusive start date (YYYY-MM-DD)
            date_to: Optional - inclusive end date (YYYY-MM-DD)
            limit: Max rows to return (default 100, max 500)

        Returns:
            {'bank_transactions': [...], 'count': N, 'limit': L}
            Sorted by transaction_date DESC, then created_at DESC.
        """
        params = [f'limit={limit}']
        if invoice_id:
            params.append(f'invoice_id={invoice_id}')
        if method:
            params.append(f'method={method}')
        if date_from:
            params.append(f'date_from={date_from}')
        if date_to:
            params.append(f'date_to={date_to}')
        return self._make_request('GET', f'/api/bank-transactions?{"&".join(params)}')

    def get_bank_transaction(self, txn_id: str) -> Dict[str, Any]:
        """Get one bank transaction by ID. Returns {'bank_transaction': {...}}."""
        return self._make_request('GET', f'/api/bank-transactions/{txn_id}')

    def update_bank_transaction(self, txn_id: str, **kwargs) -> Dict[str, Any]:
        """
        Update mutable metadata on a bank transaction.

        Mutable fields: method, notes, payer_name, payer_account.
        Immutable (returns 400 / silently ignored): amount, transaction_date,
        invoice_id, transaction_id - these are the bank's provenance. If
        wrong, DELETE and re-record.

        Args:
            txn_id: UUID of the bank transaction
            **kwargs: One or more of method/notes/payer_name/payer_account

        Returns:
            {'message': 'Bank transaction updated', 'bank_transaction': {...}}
        """
        return self._make_request('PUT', f'/api/bank-transactions/{txn_id}', kwargs)

    def delete_bank_transaction(self, txn_id: str) -> Dict[str, Any]:
        """
        Delete a bank transaction record.

        WARNING: does NOT unmark the linked invoice as paid. The invoice
        payment record is independent. If you want to undo the invoice
        payment too, do it explicitly via the invoice endpoints (no API for
        that yet — direct SQL).
        """
        return self._make_request('DELETE', f'/api/bank-transactions/{txn_id}')

    # =========================================================================
    # BAS Lodgements (see BAS.md for Australian FY context)
    # =========================================================================

    def create_bas_lodgement(
        self,
        financial_year: str,
        quarter: int,
        period_start: str,
        period_end: str,
        lodged_at: str,
        final_amount: float,
        final_amount_type: str,
        ato_receipt_id: Optional[str] = None,
        ato_account_name: Optional[str] = None,
        lodgement_method: str = 'online',
        gst_collected: Optional[float] = None,
        gst_paid: Optional[float] = None,
        computed_net_gst: Optional[float] = None,
        prior_credit_carried: Optional[float] = 0,
        other_adjustments: Optional[float] = 0,
        adjustments_note: Optional[str] = None,
        notes: Optional[str] = None,
        screenshot_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Record a new BAS (Business Activity Statement) lodgement with the ATO.

        Records the FACT of lodgement - receipt ID, timestamp, account name,
        final settled amount, and any manual adjustments. The COMPUTED net GST
        figures come from /api/reports/quarterly-bas (see get_quarterly_bas_report
        below) but the actual amount ATO settles often differs due to
        prior-period credits, instalment interest, etc.

        Args:
            financial_year: e.g. 'FY2025/26'
            quarter: 1..4 (Australian FY: Q1=Jul-Sep, Q2=Oct-Dec, Q3=Jan-Mar, Q4=Apr-Jun)
            period_start: First day of quarter, YYYY-MM-DD
            period_end: Last day of quarter, YYYY-MM-DD
            lodged_at: ISO-8601 datetime when lodgement was submitted
            final_amount: Amount actually settled with the ATO
            final_amount_type: 'credit' | 'owe' | 'zero'
            ato_receipt_id: Optional ATO receipt ID, e.g. '9021291175'
            ato_account_name: Optional ATO account name as shown on the BAS
            lodgement_method: 'online' (default) | 'paper' | 'agent'
            gst_collected: Optional BAS label 1A (snapshot at lodgement time)
            gst_paid: Optional BAS label 1B (snapshot at lodgement time)
            computed_net_gst: Optional 1A - 1B before ATO adjustments
            prior_credit_carried: Optional credit carried from prior BAS (default 0)
            other_adjustments: Optional other manual adjustments (default 0)
            adjustments_note: Optional explanation of any adjustments
            notes: Optional free-text notes
            screenshot_path: Optional path to ATO confirmation screenshot

        Returns:
            {'message': 'BAS lodgement recorded', 'lodgement': {...}}
        """
        data = {
            'financial_year': financial_year,
            'quarter': quarter,
            'period_start': period_start,
            'period_end': period_end,
            'lodged_at': lodged_at,
            'final_amount': str(final_amount),
            'final_amount_type': final_amount_type,
            'lodgement_method': lodgement_method,
            'prior_credit_carried': str(prior_credit_carried),
            'other_adjustments': str(other_adjustments),
        }
        if ato_receipt_id is not None:
            data['ato_receipt_id'] = ato_receipt_id
        if ato_account_name is not None:
            data['ato_account_name'] = ato_account_name
        if gst_collected is not None:
            data['gst_collected'] = str(gst_collected)
        if gst_paid is not None:
            data['gst_paid'] = str(gst_paid)
        if computed_net_gst is not None:
            data['computed_net_gst'] = str(computed_net_gst)
        if adjustments_note is not None:
            data['adjustments_note'] = adjustments_note
        if notes is not None:
            data['notes'] = notes
        if screenshot_path is not None:
            data['screenshot_path'] = screenshot_path
        return self._make_request('POST', '/api/bas-lodgements', data)

    def list_bas_lodgements(
        self,
        financial_year: Optional[str] = None,
        quarter: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        List BAS lodgements, optionally filtered.

        Args:
            financial_year: Optional - e.g. 'FY2025/26'
            quarter: Optional - 1..4

        Returns:
            {'lodgements': [...], 'count': N}
        """
        params = []
        if financial_year:
            params.append(f'financial_year={financial_year}')
        if quarter:
            params.append(f'quarter={quarter}')
        query = '?' + '&'.join(params) if params else ''
        return self._make_request('GET', f'/api/bas-lodgements{query}')

    def get_bas_lodgement(self, lodgement_id: str) -> Dict[str, Any]:
        """Get one BAS lodgement by ID. Returns {'lodgement': {...}}."""
        return self._make_request('GET', f'/api/bas-lodgements/{lodgement_id}')

    def update_bas_lodgement(self, lodgement_id: str, **kwargs) -> Dict[str, Any]:
        """
        Update mutable fields on a BAS lodgement.

        Mutable: notes, adjustments_note, other_adjustments, screenshot_path.
        Other fields are lodgement provenance - if wrong, delete and re-record.
        """
        return self._make_request('PUT', f'/api/bas-lodgements/{lodgement_id}', kwargs)

    def delete_bas_lodgement(self, lodgement_id: str) -> Dict[str, Any]:
        """Delete a BAS lodgement (admin only)."""
        return self._make_request('DELETE', f'/api/bas-lodgements/{lodgement_id}')

    # =========================================================================
    # Payroll: Employees (see payroll.md for full workflow)
    # =========================================================================

    def list_employees(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List employees.

        Args:
            status: Optional filter - 'active' | 'terminated' (default no filter)

        Returns:
            List of employee dicts
        """
        params = f'?status={status}' if status else ''
        result = self._make_request('GET', f'/api/payroll/employees{params}')
        return result['employees']

    def get_employee(self, employee_id: str) -> Dict[str, Any]:
        """Get one employee by ID. Returns {'employee': {...}}."""
        result = self._make_request('GET', f'/api/payroll/employees/{employee_id}')
        return result['employee']

    def create_employee(
        self,
        legal_name: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        position: Optional[str] = None,
        preferred_name: Optional[str] = None,
        email_work: Optional[str] = None,
        email_personal: Optional[str] = None,
        pay_frequency: str = 'weekly',
        default_gross_amount: float = 0,
        super_rate_pct: float = 12.0,
        bank_bsb: Optional[str] = None,
        bank_account_name: Optional[str] = None,
        bank_account_number: Optional[str] = None,
        bank_reference_prefix: Optional[str] = None,
        super_fund_name: Optional[str] = None,
        super_fund_member_no: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a new employee. PII fields (tfn, bank_account_number, bank_bsb) are
        encrypted at rest via Fernet (PAYROLL_PII_KEY in .env).

        See payroll.md for a complete worked example (weekly payroll run) and the full
        field reference.
        """
        data = {'legal_name': legal_name, 'pay_frequency': pay_frequency,
                'default_gross_amount': str(default_gross_amount),
                'super_rate_pct': str(super_rate_pct)}
        for k in ['start_date', 'end_date', 'position', 'preferred_name',
                  'email_work', 'email_personal', 'bank_bsb', 'bank_account_name',
                  'bank_account_number', 'bank_reference_prefix',
                  'super_fund_name', 'super_fund_member_no', 'notes']:
            v = locals().get(k)
            if v is not None:
                data[k] = v
        result = self._make_request('POST', '/api/payroll/employees', data)
        return result['employee']

    def update_employee(self, employee_id: str, **kwargs) -> Dict[str, Any]:
        """Update an existing employee."""
        result = self._make_request('PUT', f'/api/payroll/employees/{employee_id}', kwargs)
        return result['employee']

    def terminate_employee(self, employee_id: str, end_date: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """
        Terminate an employee. Sets employment_status='terminated' and end_date.

        Args:
            employee_id: UUID of the employee
            end_date: Last working day, YYYY-MM-DD
            reason: Optional reason string
        """
        data = {'end_date': end_date}
        if reason:
            data['reason'] = reason
        result = self._make_request('POST', f'/api/payroll/employees/{employee_id}/terminate', data)
        return result['employee']

    # =========================================================================
    # Payroll: Pay Events (the actual payslips)
    # =========================================================================

    def list_pay_events(
        self,
        employee_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List pay events (payslips), optionally filtered.

        Args:
            employee_id: Optional UUID filter
            start_date: Optional YYYY-MM-DD, filters by payment_date
            end_date: Optional YYYY-MM-DD
            status: Optional - 'draft' | 'finalized' | 'paid' | 'cancelled'

        Returns:
            List of pay event dicts (with employee name + summary amounts)
        """
        params = []
        if employee_id:
            params.append(f'employee_id={employee_id}')
        if start_date:
            params.append(f'start_date={start_date}')
        if end_date:
            params.append(f'end_date={end_date}')
        if status:
            params.append(f'status={status}')
        query = '?' + '&'.join(params) if params else ''
        result = self._make_request('GET', f'/api/payroll/pay-events{query}')
        return result['pay_events']

    def get_pay_event(self, pay_event_id: str) -> Dict[str, Any]:
        """Get one pay event with all line items. Returns {'pay_event': {...}}."""
        result = self._make_request('GET', f'/api/payroll/pay-events/{pay_event_id}')
        return result['pay_event']

    def create_pay_event(
        self,
        employee_id: str,
        payment_date: str,
        pay_period_start: str,
        pay_period_end: str,
        gross_amount: float,
        net_amount: float,
        payg_tax_amount: float,
        pay_frequency: Optional[str] = None,
        super_ote_amount: float = 0,
        super_payable_amount: float = 0,
        bank_reference: Optional[str] = None,
        notes: Optional[str] = None,
        lines: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Create a draft pay event. Headline amounts (gross/net/payg) and lines
        must match - lines are the itemised view.

        Args:
            employee_id: UUID of the employee
            payment_date: Date the employee gets paid (YYYY-MM-DD)
            pay_period_start: First day of the pay period (YYYY-MM-DD)
            pay_period_end: Last day of the pay period (YYYY-MM-DD)
            gross_amount: Total earnings before tax
            net_amount: Take-home pay (gross - tax - deductions)
            payg_tax_amount: PAYG tax withheld
            pay_frequency: 'weekly' | 'fortnightly' | 'monthly' (defaults to employee's)
            super_ote_amount: Overtime-earnings for super calc (default 0)
            super_payable_amount: Super to remit this period (default 0)
            bank_reference: Optional reference shown on employee's bank statement
            notes: Optional free-text notes
            lines: Optional list of line items, each with:
                line_type, description, quantity, rate, amount, is_taxable, sort_order
                (see payroll.md for line_type values)

        Returns:
            {'pay_event': {...}} with status='draft'
        """
        data = {
            'employee_id': employee_id,
            'payment_date': payment_date,
            'pay_period_start': pay_period_start,
            'pay_period_end': pay_period_end,
            'gross_amount': str(gross_amount),
            'net_amount': str(net_amount),
            'payg_tax_amount': str(payg_tax_amount),
            'super_ote_amount': str(super_ote_amount),
            'super_payable_amount': str(super_payable_amount),
        }
        if pay_frequency:
            data['pay_frequency'] = pay_frequency
        if bank_reference:
            data['bank_reference'] = bank_reference
        if notes:
            data['notes'] = notes
        if lines:
            data['lines'] = lines
        result = self._make_request('POST', '/api/payroll/pay-events', data)
        return result['pay_event']

    def finalize_pay_event(self, pay_event_id: str) -> Dict[str, Any]:
        """
        Finalize a draft pay event. Locks the amounts - no more edits.

        Status: draft -> finalized. After this, the only valid actions are
        mark_paid, cancel, generate_pdf, send_payslip.
        """
        result = self._make_request('POST', f'/api/payroll/pay-events/{pay_event_id}/finalize')
        return result['pay_event']

    def mark_pay_event_paid(self, pay_event_id: str, paid_date: Optional[str] = None) -> Dict[str, Any]:
        """
        Mark a finalized pay event as paid.

        Args:
            pay_event_id: UUID
            paid_date: Date the bank debit settled (YYYY-MM-DD). Defaults to payment_date.
        """
        data = {'paid_date': paid_date} if paid_date else {}
        result = self._make_request('POST', f'/api/payroll/pay-events/{pay_event_id}/mark-paid', data)
        return result['pay_event']

    def cancel_pay_event(self, pay_event_id: str) -> Dict[str, Any]:
        """Cancel a pay event (any status except already-cancelled)."""
        result = self._make_request('POST', f'/api/payroll/pay-events/{pay_event_id}/cancel')
        return result['pay_event']

    def generate_payslip_pdf(self, pay_event_id: str, save_path: str) -> str:
        """
        Generate + download the payslip PDF for a finalized pay event.

        Uses the server-side generate_payslip_pdf() in app/shared/pdf.py.
        Returns the path to the saved PDF.
        """
        return self.download_file(f'/api/payroll/pay-events/{pay_event_id}/pdf', save_path)

    def send_payslip(
        self,
        pay_event_id: str,
        recipient_email: Optional[str] = None,
        recipient_kind: str = 'work',
    ) -> Dict[str, Any]:
        """
        Send the payslip PDF via email and record the delivery in
        payslip_deliveries (audit trail).

        Args:
            pay_event_id: UUID
            recipient_email: Optional override. Defaults to employee.email_work.
            recipient_kind: 'work' | 'personal' (which email address on file to use)

        Returns:
            {'message': ..., 'sent_count': N, 'failed_count': N,
             'deliveries': [...]}

        Raises:
            requests.HTTPError: on HTTP 502 (a delivery failed) or 503
                (the mail server is not configured). The response body
                carries 'errors[]' / the configuration message.
        """
        data = {'recipient_kind': recipient_kind}
        if recipient_email:
            data['recipient_email'] = recipient_email
        return self._make_request('POST', f'/api/payroll/pay-events/{pay_event_id}/send-payslip', data)

    # =========================================================================
    # Payroll: Super Payments
    # =========================================================================

    def list_super_payments(
        self,
        employee_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List superannuation remittances.

        Args:
            employee_id: Optional filter
            status: Optional - 'pending' | 'paid' | 'reconciled'

        Returns:
            List of super payment dicts
        """
        params = []
        if employee_id:
            params.append(f'employee_id={employee_id}')
        if status:
            params.append(f'status={status}')
        query = '?' + '&'.join(params) if params else ''
        result = self._make_request('GET', f'/api/payroll/super-payments{query}')
        return result['super_payments']

    def create_super_payment(
        self,
        employee_id: str,
        remittance_date: str,
        amount: float,
        pay_event_ids: List[str],
        fund_name_snapshot: Optional[str] = None,
        fund_member_snapshot: Optional[str] = None,
        payment_reference: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Record a superannuation remittance. One row per actual fund payment,
        covering one or more pay_events.

        Snapshots the fund_name + fund_member_no at remittance time (so even
        if the employee switches funds later, this remittance still shows the
        fund they were on when paid).
        """
        data = {
            'employee_id': employee_id,
            'remittance_date': remittance_date,
            'amount': str(amount),
            'pay_event_ids': pay_event_ids,
        }
        if fund_name_snapshot:
            data['fund_name_snapshot'] = fund_name_snapshot
        if fund_member_snapshot:
            data['fund_member_snapshot'] = fund_member_snapshot
        if payment_reference:
            data['payment_reference'] = payment_reference
        if notes:
            data['notes'] = notes
        result = self._make_request('POST', '/api/payroll/super-payments', data)
        return result['super_payment']

    def mark_super_paid(self, sp_id: str) -> Dict[str, Any]:
        """Mark a super payment as paid (status -> paid)."""
        return self._make_request('POST', f'/api/payroll/super-payments/{sp_id}/mark-paid')

    def reconcile_super(self, sp_id: str) -> Dict[str, Any]:
        """
        Mark a super payment as reconciled (status -> reconciled). Final state.
        """
        return self._make_request('POST', f'/api/payroll/super-payments/{sp_id}/reconcile')

    # =========================================================================
    # Payroll: Summary / Reports
    # =========================================================================

    def payroll_summary(self) -> Dict[str, Any]:
        """Top-level payroll dashboard summary."""
        return self._make_request('GET', '/api/payroll/summary')

    def payroll_ytd(self) -> Dict[str, Any]:
        """Year-to-date payroll totals (Australian FY)."""
        return self._make_request('GET', '/api/payroll/summary/ytd')

    def super_owing(self) -> Dict[str, Any]:
        """Outstanding super payments per employee."""
        return self._make_request('GET', '/api/payroll/summary/super-owing')

    def payg_withheld(self) -> Dict[str, Any]:
        """PAYG tax withheld summary (for BAS reporting)."""
        return self._make_request('GET', '/api/payroll/summary/payg-withheld')

    def recent_payslip_deliveries(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Most recent payslip email deliveries (audit trail)."""
        result = self._make_request('GET', f'/api/payroll/summary/recent-deliveries?limit={limit}')
        return result['deliveries']

    # =========================================================================
    # OCR Queue (see ocr-queue.md for the manual-review workflow)
    # =========================================================================

    def list_ocr_jobs(self, needs_review: bool = False, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List OCR jobs (receipts waiting to be processed or reviewed).

        Args:
            needs_review: If True, only show jobs that need manual review
                (i.e. tesseract couldn't confidently parse the receipt)
            status: Optional - 'pending' | 'processing' | 'completed' | 'failed'

        Returns:
            List of OCR job dicts
        """
        params = []
        if needs_review:
            params.append('needs_review=1')
        if status:
            params.append(f'status={status}')
        query = '?' + '&'.join(params) if params else ''
        result = self._make_request('GET', f'/api/ocr-queue{query}')
        return result['jobs']

    def get_ocr_job(self, job_id: str) -> Dict[str, Any]:
        """Get one OCR job (with raw_text + extracted_data + image_path)."""
        result = self._make_request('GET', f'/api/ocr-queue/{job_id}')
        return result['job']

    def mark_ocr_job_completed(self, job_id: str) -> Dict[str, Any]:
        """
        Mark an OCR job as completed (after manual review + expense edit).

        See ocr-queue.md for the full workflow.
        """
        return self._make_request('POST', f'/api/ocr-queue/{job_id}/mark-completed')

