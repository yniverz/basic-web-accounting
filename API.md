# Accounting REST API Documentation

Base URL: `https://<your-host>/api/v1`

## Authentication

All endpoints require an API key passed via the `Authorization` header:

```
Authorization: Bearer <API_KEY>
```

The API key is configured via the `API_KEY` environment variable (or in Portainer / docker-compose).

**Responses on auth failure:**
- `401 Unauthorized` – missing or invalid API key
- `503 Service Unavailable` – `API_KEY` env var not set

---

## Common Conventions

| Convention | Detail |
|---|---|
| Format | All request/response bodies are JSON |
| Monetary values | `float`, in EUR (e.g. `119.00` = 119,00 €) |
| Amounts | Always **gross (brutto)** – tax is calculated server-side |
| Dates | ISO 8601 `YYYY-MM-DD` (e.g. `"2026-01-15"`) |
| Content-Type | `application/json` for all POST/PUT/PATCH requests |

---

## Tax Treatment

The `tax_treatment` field controls how VAT/USt is calculated. Valid values:

| Value | Meaning | Effective Rate |
|---|---|---|
| `none` | Keine USt (default, Kleinunternehmer) | 0% |
| `standard` | Regelsteuersatz | Site setting (default 19%) |
| `reduced` | Ermäßigter Satz | Site setting (default 7%) |
| `tax_free` | Steuerfrei (§4 UStG) | 0% |
| `reverse_charge` | Reverse Charge (§13b UStG) | 0% |
| `intra_eu` | Innergemeinschaftlich | 0% |
| `custom` | Benutzerdefiniert | `custom_tax_rate` field |

> **Note:** If the system is in `kleinunternehmer` mode, all tax treatments are forced to `none`.

You can query valid values via `GET /api/v1/tax-treatments`.

---

## Endpoints

### Settings

#### `GET /settings`

Returns current business/tax settings (read-only via API).

**Response:**
```json
{
  "business_name": "Meine Firma",
  "tax_mode": "kleinunternehmer",
  "tax_rate": 19.0,
  "tax_rate_reduced": 7.0,
  "tax_number": "12/345/67890",
  "vat_id": "DE123456789"
}
```

---

#### `GET /tax-treatments`

Returns all valid `tax_treatment` values with German labels.

**Response:**
```json
{
  "tax_treatments": [
    { "value": "none", "label": "Keine USt" },
    { "value": "standard", "label": "Regelsteuersatz" },
    { "value": "reduced", "label": "Ermäßigter Satz" },
    { "value": "tax_free", "label": "Steuerfrei (0%)" },
    { "value": "reverse_charge", "label": "Reverse Charge (§13b)" },
    { "value": "intra_eu", "label": "Innergemeinschaftlich" },
    { "value": "custom", "label": "Benutzerdefiniert" }
  ]
}
```

---

### Categories

#### `GET /categories`

List all categories (Buchungskategorien).

**Query Parameters:**
| Param | Type | Description |
|---|---|---|
| `type` | string | Filter: `income` or `expense` |

**Response:**
```json
{
  "categories": [
    {
      "id": 1,
      "name": "Mieteinnahmen",
      "type": "income",
      "description": "Einnahmen aus Vermietung",
      "sort_order": 0
    }
  ]
}
```

---

#### `POST /categories`

Create a new category.

**Request Body:**
```json
{
  "name": "Mieteinnahmen",
  "type": "income",
  "description": "Einnahmen aus Vermietung",
  "sort_order": 0
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | ✅ | Category name |
| `type` | string | ✅ | `income` or `expense` |
| `description` | string | ❌ | Optional description |
| `sort_order` | int | ❌ | Sort order (default 0) |

**Response:** `201 Created`
```json
{
  "category": { "id": 5, "name": "Mieteinnahmen", "type": "income", ... }
}
```

---

#### `GET /categories/:id`

Get a single category by ID.

**Response:**
```json
{
  "category": { "id": 5, "name": "Mieteinnahmen", "type": "income", ... }
}
```

---

#### `PUT /categories/:id`

Update a category. Only provided fields are changed.

**Request Body:** (all fields optional)
```json
{
  "name": "Neue Bezeichnung",
  "type": "expense",
  "description": "Updated description",
  "sort_order": 10
}
```

**Response:** `200 OK` with updated category.

---

#### `DELETE /categories/:id`

Delete a category. Transactions linked to this category will have their `category_id` set to `null`.

**Response:**
```json
{ "deleted": true, "id": 5 }
```

---

### Transactions

#### `GET /transactions`

List transactions with filtering, search, and pagination.

**Query Parameters:**
| Param | Type | Default | Description |
|---|---|---|---|
| `year` | int | — | Filter by year |
| `month` | int | — | Filter by month (1–12) |
| `type` | string | — | `income` or `expense` |
| `category_id` | int | — | Filter by category |
| `search` | string | — | Text search in description & notes |
| `sort` | string | `date_desc` | `date_desc` or `date_asc` |
| `limit` | int | 100 | Max results (max 1000) |
| `offset` | int | 0 | Pagination offset |

**Response:**
```json
{
  "transactions": [
    {
      "id": 42,
      "date": "2026-01-15",
      "type": "income",
      "description": "Miete Januar 2026",
      "amount": 1190.00,
      "net_amount": 1000.00,
      "tax_amount": 190.00,
      "tax_treatment": "standard",
      "tax_rate": 19.0,
      "category_id": 1,
      "category_name": "Mieteinnahmen",
      "notes": "Wohnung A",
      "document_filename": null,
      "created_at": "2026-01-15T10:30:00",
      "updated_at": "2026-01-15T10:30:00"
    }
  ],
  "total": 150,
  "limit": 100,
  "offset": 0
}
```

---

#### `POST /transactions`

Create a single transaction.

**Request Body:**
```json
{
  "date": "2026-01-15",
  "type": "income",
  "description": "Miete Januar 2026",
  "amount": 1190.00,
  "category_id": 1,
  "tax_treatment": "standard",
  "notes": "Wohnung A"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `date` | string | ✅ | `YYYY-MM-DD` |
| `type` | string | ✅ | `income` or `expense` |
| `description` | string | ✅ | Transaction description |
| `amount` | number | ✅ | Gross amount (brutto) in EUR, must be > 0 |
| `category_id` | int | ❌ | Link to a category |
| `tax_treatment` | string | ❌ | See [Tax Treatment](#tax-treatment). Default: `none` |
| `custom_tax_rate` | number | ❌ | Only if `tax_treatment` = `custom` |
| `notes` | string | ❌ | Additional notes |

**Response:** `201 Created`
```json
{
  "transaction": {
    "id": 42,
    "date": "2026-01-15",
    "type": "income",
    "description": "Miete Januar 2026",
    "amount": 1190.00,
    "net_amount": 1000.00,
    "tax_amount": 190.00,
    "tax_treatment": "standard",
    "tax_rate": 19.0,
    "category_id": 1,
    "category_name": "Mieteinnahmen",
    "notes": "Wohnung A",
    "document_filename": null,
    "created_at": "2026-01-15T10:30:00",
    "updated_at": "2026-01-15T10:30:00"
  }
}
```

---

#### `POST /transactions/bulk`

Create multiple transactions at once (max 500 per request).

**Request Body:**
```json
{
  "transactions": [
    {
      "date": "2026-01-15",
      "type": "income",
      "description": "Miete Januar",
      "amount": 1190.00,
      "category_id": 1,
      "tax_treatment": "standard"
    },
    {
      "date": "2026-01-15",
      "type": "expense",
      "description": "Hausverwaltung",
      "amount": 200.00,
      "category_id": 3,
      "tax_treatment": "standard"
    }
  ]
}
```

Each item in the array follows the same schema as `POST /transactions`.

**Response:** `201 Created`
```json
{
  "created": [ { "id": 42, ... }, { "id": 43, ... } ],
  "errors": [],
  "count": 2
}
```

If some entries fail validation, they are reported in `errors` by index, while valid ones are still created:
```json
{
  "created": [ { "id": 42, ... } ],
  "errors": [ { "index": 1, "error": "amount must be positive" } ],
  "count": 1
}
```

If **all** entries fail, nothing is committed and you get `400`.

---

#### `GET /transactions/:id`

Get a single transaction by ID.

**Response:**
```json
{
  "transaction": { "id": 42, "date": "2026-01-15", ... }
}
```

---

#### `PUT /transactions/:id`

Update a transaction. Only provided fields are changed. Tax is recalculated automatically.

**Request Body:** (all fields optional)
```json
{
  "date": "2026-02-01",
  "amount": 1200.00,
  "description": "Miete Februar 2026",
  "tax_treatment": "standard",
  "notes": "Updated"
}
```

**Response:** `200 OK` with updated transaction.

---

#### `DELETE /transactions/:id`

Delete a transaction. Associated document files are also removed.

**Response:**
```json
{ "deleted": true, "id": 42 }
```

---

### Summary

#### `GET /summary`

Financial summary for a given year.

**Query Parameters:**
| Param | Type | Default | Description |
|---|---|---|---|
| `year` | int | current year | Year to summarize |

**Response:**
```json
{
  "year": 2026,
  "total_income": 14280.00,
  "total_expenses": 5400.00,
  "profit": 8880.00,
  "total_income_net": 12000.00,
  "total_expenses_net": 4537.82,
  "vat_collected": 2280.00,
  "vat_paid": 862.18,
  "vat_payable": 1417.82,
  "transaction_count": 24,
  "monthly": {
    "1": { "income": 1190.00, "expenses": 450.00, "profit": 740.00 },
    "2": { "income": 1190.00, "expenses": 450.00, "profit": 740.00 },
    "...": "..."
  }
}
```

---

## Error Responses

All errors follow this format:

```json
{ "error": "Description of the problem" }
```

Or for validation with multiple issues:

```json
{ "errors": ["date is required (YYYY-MM-DD)", "amount is required (gross/brutto)"] }
```

**HTTP Status Codes:**
| Code | Meaning |
|---|---|
| `200` | Success |
| `201` | Created |
| `400` | Bad request / validation error |
| `401` | Unauthorized (invalid API key) |
| `404` | Resource not found |
| `503` | API not configured |

---

## Example: Monthly Rent Booking (cURL)

```bash
# Create a single monthly rent income entry
curl -X POST https://your-host/api/v1/transactions \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "date": "2026-01-15",
    "type": "income",
    "description": "Miete Januar 2026 – Wohnung A",
    "amount": 1190.00,
    "category_id": 1,
    "tax_treatment": "standard",
    "notes": "Kaltmiete 1000€ + 19% USt"
  }'
```

```bash
# Bulk-create 3 months of rent
curl -X POST https://your-host/api/v1/transactions/bulk \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "transactions": [
      {"date": "2026-01-15", "type": "income", "description": "Miete Jan", "amount": 1190, "category_id": 1, "tax_treatment": "standard"},
      {"date": "2026-02-15", "type": "income", "description": "Miete Feb", "amount": 1190, "category_id": 1, "tax_treatment": "standard"},
      {"date": "2026-03-15", "type": "income", "description": "Miete Mär", "amount": 1190, "category_id": 1, "tax_treatment": "standard"}
    ]
  }'
```

```bash
# Look up categories first to find the right category_id
curl https://your-host/api/v1/categories?type=income \
  -H "Authorization: Bearer your-api-key"
```

```bash
# Get yearly summary
curl https://your-host/api/v1/summary?year=2026 \
  -H "Authorization: Bearer your-api-key"
```
