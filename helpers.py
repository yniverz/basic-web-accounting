from datetime import date, datetime
import locale


def format_currency(value):
    """Format a number as Euro currency string."""
    if value is None:
        return '0,00 €'
    return f'{value:,.2f} €'.replace(',', 'X').replace('.', ',').replace('X', '.')


def format_date(d):
    """Format a date as DD.MM.YYYY."""
    if isinstance(d, str):
        d = datetime.strptime(d, '%Y-%m-%d').date()
    if d is None:
        return ''
    return d.strftime('%d.%m.%Y')


def parse_date(date_str):
    """Parse a date string from HTML date input (YYYY-MM-DD)."""
    if not date_str:
        return date.today()
    return datetime.strptime(date_str, '%Y-%m-%d').date()


def parse_amount(amount_str):
    """Parse a monetary amount string, handling both comma and dot decimals."""
    if not amount_str:
        return 0.0
    # Replace comma with dot for parsing
    amount_str = str(amount_str).strip().replace(',', '.')
    return float(amount_str)


def calculate_tax(gross_amount, tax_rate):
    """Calculate net amount and tax from gross amount."""
    if tax_rate <= 0:
        return gross_amount, 0.0
    net = gross_amount / (1 + tax_rate / 100)
    tax = gross_amount - net
    return round(net, 2), round(tax, 2)


def get_year_choices():
    """Return a list of years for filter dropdowns."""
    current_year = date.today().year
    return list(range(current_year, current_year - 10, -1))


def get_month_names():
    """Return German month names."""
    return {
        1: 'Januar', 2: 'Februar', 3: 'März', 4: 'April',
        5: 'Mai', 6: 'Juni', 7: 'Juli', 8: 'August',
        9: 'September', 10: 'Oktober', 11: 'November', 12: 'Dezember'
    }
