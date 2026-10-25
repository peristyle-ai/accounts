from decimal import Decimal, InvalidOperation
from datetime import date


def validate_uuid(value):
    import uuid
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError):
        return False


def validate_decimal(value, required=False, min_value=None, max_value=None):
    if value is None or value == '':
        if required:
            raise ValueError('Value is required')
        return None

    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f'Invalid decimal value: {value}')

    if min_value is not None and decimal_value < Decimal(str(min_value)):
        raise ValueError(f'Value must be at least {min_value}')

    if max_value is not None and decimal_value > Decimal(str(max_value)):
        raise ValueError(f'Value must be at most {max_value}')

    return decimal_value


def validate_date_string(value, required=False):
    if value is None or value == '':
        if required:
            raise ValueError('Date is required')
        return None

    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError(f'Invalid date format: {value}. Use YYYY-MM-DD')


def validate_gst_type(value):
    if value is None:
        return Decimal('0')

    valid_gst_types = [Decimal('0'), Decimal('0.1')]
    gst = Decimal(str(value))

    if gst not in valid_gst_types:
        raise ValueError(f'Invalid GST type: {value}. Must be 0 or 0.1')

    return gst
