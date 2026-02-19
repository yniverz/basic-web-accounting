"""
Depreciation calculation module for German tax law (EStG).

All depreciation rules are configured in RULES dict for easy updates
when tax law changes. Calculation functions are pure and stateless.

References:
- § 6 Abs. 2 EStG: GWG Sofortabschreibung (immediate write-off ≤ 800€ net)
- § 6 Abs. 2a EStG: Sammelposten (pool depreciation 250-1000€ net, 5 years)
- § 7 Abs. 1 EStG: Lineare AfA (straight-line, pro-rata temporis)
- § 7 Abs. 2 EStG: Degressive AfA (declining balance, 07/2025-12/2027)
- BMF-Schreiben 2021: Digital assets (1 year useful life)
"""

from datetime import date
from math import ceil


# =============================================================================
# RULES CONFIGURATION — update these when tax law changes
# =============================================================================

RULES = {
    # § 6 Abs. 2 EStG — GWG (geringwertige Wirtschaftsgüter)
    'gwg_sofort_limit': 800.00,       # Max net price for immediate deduction
    'gwg_register_limit': 250.00,     # Above this: must be recorded in register

    # § 6 Abs. 2a EStG — Sammelposten
    'sammelposten_min': 250.01,       # Minimum net price for Sammelposten
    'sammelposten_max': 1000.00,      # Maximum net price for Sammelposten
    'sammelposten_years': 5,          # Fixed depreciation period

    # § 7 Abs. 2 EStG — Degressive AfA (valid for acquisitions 01.07.2025 - 31.12.2027)
    'degressive_max_factor': 3.0,     # Max multiple of linear rate
    'degressive_max_rate': 30.0,      # Max percentage per year
    'degressive_valid_from': date(2025, 7, 1),
    'degressive_valid_until': date(2027, 12, 31),

    # BMF-Schreiben — Digital assets
    'digital_useful_life_months': 12, # Computer hardware & software
}

# Depreciation method choices for UI
DEPRECIATION_METHODS = {
    'sofort': 'Sofortabschreibung (GWG)',
    'linear': 'Lineare AfA (§ 7 Abs. 1)',
    'sammelposten': 'Sammelposten (§ 6 Abs. 2a)',
    'degressive': 'Degressive AfA (§ 7 Abs. 2)',
}

# Common useful life presets (years) based on AfA-Tabellen
USEFUL_LIFE_PRESETS = {
    'Computer / IT-Hardware': 3,
    'Software': 3,
    'Digitale Wirtschaftsgüter': 1,
    'Büromöbel': 13,
    'Schreibtisch': 13,
    'Bürostuhl': 13,
    'Drucker / Scanner': 6,
    'Smartphone / Tablet': 5,
    'Pkw': 6,
    'Lkw / Transporter': 9,
    'Fahrrad / E-Bike': 7,
    'Werkzeuge': 5,
    'Maschinen (allgemein)': 10,
    'Fotografie-Ausrüstung': 7,
    'Kühlschrank / Haushaltsgerät': 10,
}


# =============================================================================
# CALCULATION FUNCTIONS
# =============================================================================

def get_depreciable_amount(asset):
    """Return the total amount to be depreciated (Abschreibungsvolumen)."""
    return max(asset.purchase_price_net - (asset.salvage_value or 0), 0)


def get_depreciation_schedule(asset):
    """
    Generate the full yearly depreciation schedule for an asset.

    Returns a list of dicts:
        [{'year': int, 'amount': float, 'cumulative': float, 'book_value': float}, ...]

    Handles:
    - Sofortabschreibung: full amount in purchase year
    - Sammelposten: 1/5 per year, 5 years, continues after disposal
    - Lineare AfA: equal amounts, pro-rata first year, stops at disposal
    - Degressive AfA: declining balance, pro-rata first year, stops at disposal
    """
    method = asset.depreciation_method
    depreciable = get_depreciable_amount(asset)

    if depreciable <= 0:
        return []

    if method == 'sofort':
        return _schedule_sofort(asset, depreciable)
    elif method == 'sammelposten':
        return _schedule_sammelposten(asset, depreciable)
    elif method == 'linear':
        return _schedule_linear(asset, depreciable)
    elif method == 'degressive':
        return _schedule_degressive(asset, depreciable)
    else:
        return _schedule_linear(asset, depreciable)  # fallback


def get_depreciation_for_year(asset, year):
    """Return the depreciation amount for a specific year."""
    schedule = get_depreciation_schedule(asset)
    for entry in schedule:
        if entry['year'] == year:
            return entry['amount']
    return 0.0


def get_book_value(asset, as_of_date=None):
    """
    Calculate the current book value (Restbuchwert) of an asset.

    If as_of_date is provided, calculates book value as of that date.
    Otherwise uses today.
    """
    if as_of_date is None:
        as_of_date = date.today()

    schedule = get_depreciation_schedule(asset)
    cumulative = 0
    for entry in schedule:
        if entry['year'] <= as_of_date.year:
            cumulative = entry['cumulative']
        else:
            break

    return max(asset.purchase_price_net - cumulative, asset.salvage_value or 0)


def get_disposal_result(asset):
    """
    Calculate the gain/loss from disposing an asset.

    Returns:
        {'book_value_at_disposal': float, 'disposal_price': float,
         'gain_or_loss': float, 'is_gain': bool}

    For Sammelposten: book value at disposal is 0 (depreciation continues
    regardless), so disposal price is pure income.
    """
    if not asset.disposal_date:
        return None

    disposal_price = asset.disposal_price or 0

    if asset.depreciation_method == 'sammelposten':
        # § 6 Abs. 2a Satz 3: Sammelposten is not reduced on disposal
        # The full schedule continues. Disposal price is simply income.
        return {
            'book_value_at_disposal': 0,  # effectively irrelevant
            'disposal_price': disposal_price,
            'gain_or_loss': disposal_price,
            'is_gain': disposal_price > 0,
            'is_sammelposten': True,
        }

    book_value = get_book_value(asset, asset.disposal_date)

    gain_or_loss = disposal_price - book_value

    return {
        'book_value_at_disposal': round(book_value, 2),
        'disposal_price': disposal_price,
        'gain_or_loss': round(gain_or_loss, 2),
        'is_gain': gain_or_loss >= 0,
        'is_sammelposten': False,
    }


def suggest_method(net_price):
    """
    Suggest the appropriate depreciation method based on net purchase price.

    Returns a list of valid methods with recommendations.
    """
    suggestions = []

    if net_price <= RULES['gwg_sofort_limit']:
        suggestions.append({
            'method': 'sofort',
            'label': DEPRECIATION_METHODS['sofort'],
            'recommended': True,
            'note': f'GWG bis {RULES["gwg_sofort_limit"]:.0f} € netto',
        })

    if RULES['sammelposten_min'] <= net_price <= RULES['sammelposten_max']:
        suggestions.append({
            'method': 'sammelposten',
            'label': DEPRECIATION_METHODS['sammelposten'],
            'recommended': False,
            'note': f'Pool-Abschreibung über {RULES["sammelposten_years"]} Jahre',
        })

    if net_price > RULES['gwg_sofort_limit']:
        suggestions.append({
            'method': 'linear',
            'label': DEPRECIATION_METHODS['linear'],
            'recommended': net_price > RULES['sammelposten_max'],
            'note': 'Gleichmäßige Verteilung über Nutzungsdauer',
        })
        suggestions.append({
            'method': 'degressive',
            'label': DEPRECIATION_METHODS['degressive'],
            'recommended': False,
            'note': 'Fallende Jahresbeträge (§ 7 Abs. 2, ab 07/2025)',
        })

    return suggestions


# =============================================================================
# INTERNAL SCHEDULE GENERATORS
# =============================================================================

def _schedule_sofort(asset, depreciable):
    """GWG Sofortabschreibung: full deduction in purchase year."""
    return [{
        'year': asset.purchase_date.year,
        'amount': round(depreciable, 2),
        'cumulative': round(depreciable, 2),
        'book_value': round(asset.salvage_value or 0, 2),
    }]


def _schedule_sammelposten(asset, depreciable):
    """
    Sammelposten (§ 6 Abs. 2a EStG):
    - 1/5 per year for 5 years
    - No pro-rata temporis
    - Continues AFTER disposal (§ 6 Abs. 2a Satz 3)
    """
    years = RULES['sammelposten_years']
    annual = depreciable / years
    schedule = []
    cumulative = 0.0

    for i in range(years):
        year = asset.purchase_date.year + i
        amount = min(annual, depreciable - cumulative)
        amount = round(amount, 2)
        cumulative += amount
        cumulative = round(cumulative, 2)
        book_value = round(asset.purchase_price_net - cumulative, 2)

        schedule.append({
            'year': year,
            'amount': amount,
            'cumulative': cumulative,
            'book_value': max(book_value, asset.salvage_value or 0),
        })

    return schedule


def _schedule_linear(asset, depreciable):
    """
    Lineare AfA (§ 7 Abs. 1 EStG):
    - Equal annual amounts over useful life
    - Pro-rata temporis in first year (§ 7 Abs. 1 Satz 4)
    - Stops at disposal (pro-rata in disposal year)
    """
    useful_months = asset.useful_life_months or 12
    monthly = depreciable / useful_months
    purchase_year = asset.purchase_date.year
    purchase_month = asset.purchase_date.month

    schedule = []
    cumulative = 0.0
    year = purchase_year

    while cumulative < depreciable - 0.005:
        # Determine months to depreciate this year
        if year == purchase_year:
            # Pro-rata: month of acquisition counts as full month
            # § 7 Abs. 1 Satz 4: reduce by 1/12 for each full month before acquisition
            months = 13 - purchase_month
        else:
            months = 12

        # Check for disposal (not for sammelposten — handled separately)
        if asset.disposal_date and asset.depreciation_method != 'sammelposten':
            if year > asset.disposal_date.year:
                break
            if year == asset.disposal_date.year:
                if year == purchase_year:
                    months = asset.disposal_date.month - purchase_month + 1
                else:
                    months = asset.disposal_date.month

        amount = monthly * months
        amount = min(amount, depreciable - cumulative)
        amount = round(amount, 2)

        if amount <= 0:
            break

        cumulative += amount
        cumulative = round(cumulative, 2)
        book_value = round(asset.purchase_price_net - cumulative, 2)

        schedule.append({
            'year': year,
            'amount': amount,
            'cumulative': cumulative,
            'book_value': max(book_value, asset.salvage_value or 0),
        })

        year += 1

    return schedule


def _schedule_degressive(asset, depreciable):
    """
    Degressive AfA (§ 7 Abs. 2 EStG):
    - Declining balance from book value
    - Rate: min(3 × linear rate, 30%)
    - Pro-rata in first year (§ 7 Abs. 2 Satz 3 → § 7 Abs. 1 Satz 4)
    - Automatic switch to linear when linear becomes more advantageous
    - Stops at disposal
    """
    useful_months = asset.useful_life_months or 12
    useful_years = useful_months / 12
    linear_rate = 100 / useful_years
    degressive_rate = min(
        linear_rate * RULES['degressive_max_factor'],
        RULES['degressive_max_rate']
    )

    purchase_year = asset.purchase_date.year
    purchase_month = asset.purchase_date.month
    salvage = asset.salvage_value or 0

    schedule = []
    cumulative = 0.0
    book_value = asset.purchase_price_net
    year = purchase_year

    while book_value > salvage + 0.005:
        # Calculate remaining useful life for potential linear switch
        remaining_cumulative_months = useful_months - (
            (year - purchase_year) * 12 + (purchase_month - 1)
            if year == purchase_year else
            (year - purchase_year) * 12 - (purchase_month - 1)
        )

        # Degressive amount (from current book value)
        annual_degressive = book_value * degressive_rate / 100

        # Pro-rata in first year
        if year == purchase_year:
            months_factor = (13 - purchase_month) / 12
            amount_degressive = annual_degressive * months_factor
        else:
            amount_degressive = annual_degressive

        # Linear amount on remaining value over remaining time
        elapsed_months = _months_elapsed(purchase_year, purchase_month, year)
        remaining_months = max(useful_months - elapsed_months, 1)
        remaining_years = remaining_months / 12
        amount_linear = (book_value - salvage) / remaining_years if remaining_years > 0 else book_value - salvage

        if year == purchase_year:
            amount_linear *= (13 - purchase_month) / 12

        # Switch to linear if it's more advantageous
        amount = max(amount_degressive, amount_linear)

        # Check disposal
        if asset.disposal_date:
            if year > asset.disposal_date.year:
                break
            if year == asset.disposal_date.year:
                if year == purchase_year:
                    month_fraction = (asset.disposal_date.month - purchase_month + 1) / (13 - purchase_month)
                else:
                    month_fraction = asset.disposal_date.month / 12
                amount *= month_fraction

        # Cap at remaining depreciable amount
        amount = min(amount, book_value - salvage)
        amount = round(amount, 2)

        if amount <= 0:
            break

        cumulative += amount
        cumulative = round(cumulative, 2)
        book_value = round(asset.purchase_price_net - cumulative, 2)

        schedule.append({
            'year': year,
            'amount': amount,
            'cumulative': cumulative,
            'book_value': max(book_value, salvage),
        })

        year += 1

    return schedule


def _months_elapsed(start_year, start_month, current_year):
    """Calculate months elapsed from start to beginning of current_year."""
    if current_year <= start_year:
        return 0
    return (current_year - start_year) * 12 - (start_month - 1)
