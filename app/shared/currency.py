import requests
import os
from datetime import date
from decimal import Decimal


def get_usd_to_aud_rate(expense_date_str):
    """
    Get USD to AUD exchange rate for a specific date using Twelve Data API.

    Args:
        expense_date_str: Date string in YYYY-MM-DD format

    Returns:
        Decimal exchange rate (USD to AUD)
        None if the API call fails
    """
    api_key = os.environ.get('TWELVE_DATA_API_KEY')
    if not api_key:
        return None

    try:
        expense_date = date.fromisoformat(expense_date_str)
        formatted_date = expense_date.strftime('%Y-%m-%d')

        url = "https://api.twelvedata.com/forex_pairs"
        params = {
            "symbol": "USD/AUD",
            "format": "JSON",
            "date": formatted_date,
            "apikey": api_key
        }

        response = requests.get(url, params=params, timeout=10)

        if response.status_code == 200:
            data = response.json()
            if 'data' in data and len(data['data']) > 0:
                rate = data['data'][0].get('close') or data['data'][0].get('open')
                if rate:
                    return Decimal(str(rate))
    except Exception as e:
        print(f"Error fetching exchange rate: {e}")

    return None


def convert_usd_to_aud(amount, exchange_rate):
    """
    Convert USD amount to AUD using the given exchange rate.

    Args:
        amount: Decimal amount in USD
        exchange_rate: Decimal exchange rate (USD to AUD)

    Returns:
        Decimal amount in AUD
    """
    return (Decimal(str(amount)) * exchange_rate).quantize(Decimal('0.01'))
