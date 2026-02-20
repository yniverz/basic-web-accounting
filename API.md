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

### Accounts

Accounts (Konten) represent Bank, Bargeld, PayPal, etc. Every transaction must be assigned to an account. Account balances are computed from `initial_balance` plus all transaction movements.

#### `GET /accounts`

List all accounts with current balances.

**Response:**
```json
{
  "accounts": [
    {
      "id": 1,
      "name": "Bank",
      "description": "Geschäftskonto",
      "initial_balance": 1000.00,
      "current_balance": 3210.50,
      "sort_order": 1
    },
    {
      "id": 2,
      "name": "Bargeld",
      "description": null,
      "initial_balance": 0.00,
      "current_balance": 150.00,
      "sort_order": 2
    }
  ]
}
```

---

#### `POST /accounts`

Create a new account.

**Request Body:**
```json
{
  "name": "PayPal",
  "description": "PayPal-Geschäftskonto",
  "initial_balance": 500.00,
  "sort_order": 3
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | ✅ | Account name |
| `description` | string | ❌ | Optional description |
| `initial_balance` | number | ❌ | Starting balance (default 0) |
| `sort_order` | int | ❌ | Sort order (default 0) |

**Response:** `201 Created`
```json
{
  "account": { "id": 3, "name": "PayPal", "current_balance": 500.00, ... }
}
```

---

#### `GET /accounts/:id`

Get a single account by ID with current balance.

**Response:**
```json
{
  "account": { "id": 1, "name": "Bank", "current_balance": 3210.50, ... }
}
```

---

#### `PUT /accounts/:id`

Update an account. Only provided fields are changed.

**Request Body:** (all fields optional)
```json
{
  "name": "Neuer Name",
  "description": "Updated description",
  "initial_balance": 2000.00,
  "sort_order": 5
}
```

**Response:** `200 OK` with updated account.

---

#### `DELETE /accounts/:id`

Delete an account. Fails with `409` if any transactions reference it.

**Response:**
```json
{ "deleted": true, "id": 3 }
```

**Error (409):**
```json
{ "error": "Cannot delete account with 12 linked transaction(s). Move or delete them first." }
```

---

### Transfers

Transfers (Umbuchungen) move money between accounts. They are stored as transactions with `type: "transfer"` and are **not counted in the EÜR**.

#### `POST /transfers`

Create a transfer between two accounts.

**Request Body:**
```json
{
  "date": "2026-01-20",
  "amount": 500.00,
  "from_account_id": 1,
  "to_account_id": 2,
  "description": "Bargeldabhebung",
  "notes": "Für Bürobedarf"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `date` | string | ✅ | `YYYY-MM-DD` |
| `amount` | number | ✅ | Transfer amount, > 0 |
| `from_account_id` | int | ✅ | Source account ID |
| `to_account_id` | int | ✅ | Destination account ID |
| `description` | string | ❌ | Description (auto-generated if empty) |
| `notes` | string | ❌ | Additional notes |

**Response:** `201 Created`
```json
{
  "transaction": {
    "id": 55,
    "date": "2026-01-20",
    "type": "transfer",
    "description": "Bargeldabhebung",
    "amount": 500.00,
    "account_id": 1,
    "account_name": "Bank",
    "transfer_to_account_id": 2,
    "transfer_to_account_name": "Bargeld",
    ...
  }
}
```

> **Note:** Transfer transactions cannot be edited via `PUT /transactions/:id`. Delete and recreate them instead.

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
| `type` | string | — | `income`, `expense`, or `transfer` |
| `category_id` | int | — | Filter by category |
| `account_id` | int | — | Filter by account (includes transfers from/to) |
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
      "account_id": 1,
      "account_name": "Bank",
      "notes": "Wohnung A",
      "documents": [],
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

Transfer transactions additionally include `transfer_to_account_id` and `transfer_to_account_name`.
Transactions linked to an asset include `linked_asset_id`.

---

#### `POST /transactions`

Create a single transaction. For transfers between accounts, use `POST /transfers` instead.

**Request Body:**
```json
{
  "date": "2026-01-15",
  "type": "income",
  "description": "Miete Januar 2026",
  "amount": 1190.00,
  "account_id": 1,
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
| `account_id` | int | ✅ | Account to book to |
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
    "account_id": 1,
    "account_name": "Bank",
    "notes": "Wohnung A",
    "documents": [],
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
      "account_id": 1,
      "category_id": 1,
      "tax_treatment": "standard"
    },
    {
      "date": "2026-01-15",
      "type": "expense",
      "description": "Hausverwaltung",
      "amount": 200.00,
      "account_id": 1,
      "category_id": 3,
      "tax_treatment": "standard"
    }
  ]
}
```

Each item follows the same schema as `POST /transactions` (including required `account_id`).

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
  "transaction": { "id": 42, "date": "2026-01-15", "account_id": 1, ... }
}
```

---

#### `PUT /transactions/:id`

Update a transaction. Only provided fields are changed. Tax is recalculated automatically.

> **Restrictions:**
> - Transactions linked to an asset (`linked_asset_id` is set) cannot be edited via this endpoint → `409`
> - Transfer transactions (`type: "transfer"`) cannot be edited → `409`. Delete and recreate instead.

**Request Body:** (all fields optional)
```json
{
  "date": "2026-02-01",
  "amount": 1200.00,
  "description": "Miete Februar 2026",
  "account_id": 2,
  "tax_treatment": "standard",
  "notes": "Updated"
}
```

**Response:** `200 OK` with updated transaction.

**Error (409):**
```json
{ "error": "Cannot edit a transaction linked to an asset. Manage it via the asset." }
```

---

#### `DELETE /transactions/:id`

Delete a transaction. Associated document files are also removed.

> **Restriction:** Transactions linked to an asset cannot be deleted via this endpoint → `409`. Manage them via the asset detail page.

**Response:**
```json
{ "deleted": true, "id": 42 }
```

---

### Transaction Documents

Attach, download, or remove document files (receipts, invoices, etc.) from transactions. Multiple documents can be attached to a single transaction. Allowed file types: **pdf, png, jpg, jpeg, gif, webp**. Max upload size: **16 MB**.

#### `POST /transactions/:id/documents`

Upload one or more documents to a transaction. Uses `multipart/form-data` (not JSON). New documents are **appended** to any existing ones.

**Request:** `Content-Type: multipart/form-data`

| Field | Type | Required | Description |
|---|---|---|---|
| `documents` | file(s) | ✅ | One or more files to attach |

**Response:** `201 Created`
```json
{
  "transaction_id": 42,
  "documents": [
    { "id": 1, "filename": "20260115_103000_rechnung.pdf", "original_filename": "rechnung.pdf" },
    { "id": 2, "filename": "20260115_103000_foto.jpg", "original_filename": "foto.jpg" }
  ],
  "errors": []
}
```

If some files fail validation, valid ones are still saved and failures are reported in `errors`:
```json
{
  "transaction_id": 42,
  "documents": [ { "id": 1, "filename": "...", "original_filename": "rechnung.pdf" } ],
  "errors": [ { "index": 1, "filename": "data.exe", "error": "File type not allowed. Allowed: gif, jpeg, jpg, pdf, png, webp" } ]
}
```

**Example (cURL) – single file:**
```bash
curl -X POST https://your-host/api/v1/transactions/42/documents \
  -H "Authorization: Bearer your-api-key" \
  -F "documents=@/path/to/rechnung.pdf"
```

**Example (cURL) – multiple files:**
```bash
curl -X POST https://your-host/api/v1/transactions/42/documents \
  -H "Authorization: Bearer your-api-key" \
  -F "documents=@/path/to/rechnung.pdf" \
  -F "documents=@/path/to/foto.jpg"
```

---

#### `GET /transactions/:id/documents`

List all documents attached to a transaction.

**Response:**
```json
{
  "transaction_id": 42,
  "documents": [
    { "id": 1, "filename": "20260115_103000_rechnung.pdf", "original_filename": "rechnung.pdf", "created_at": "2026-01-15T10:30:00" },
    { "id": 2, "filename": "20260115_103000_foto.jpg", "original_filename": "foto.jpg", "created_at": "2026-01-15T10:30:05" }
  ]
}
```

---

#### `GET /transactions/:id/documents/:doc_id`

Download a specific document by its ID. Returns the raw file with the appropriate content type.

**Response:** Binary file content with matching `Content-Type`, or `404` if not found.

**Example (cURL):**
```bash
curl https://your-host/api/v1/transactions/42/documents/1 \
  -H "Authorization: Bearer your-api-key" \
  -o rechnung.pdf
```

---

#### `DELETE /transactions/:id/documents/:doc_id`

Remove a specific document from a transaction. The file is deleted from disk.

**Response:**
```json
{ "deleted": true, "document_id": 1, "transaction_id": 42 }
```

**Error (404):** Transaction or document not found.

---

### Summary

#### `GET /summary`

Financial summary for a given year. EÜR figures exclude transfers and asset-linked transactions.

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
  },
  "accounts": [
    {
      "id": 1,
      "name": "Bank",
      "description": "Geschäftskonto",
      "initial_balance": 1000.00,
      "current_balance": 8400.00,
      "sort_order": 1
    }
  ]
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
| `409` | Conflict (e.g. linked transaction, account has transactions) |
| `503` | API not configured |

---

## Example: Monthly Rent Booking (cURL)

```bash
# List accounts to find the right account_id
curl https://your-host/api/v1/accounts \
  -H "Authorization: Bearer your-api-key"
```

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
    "account_id": 1,
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
      {"date": "2026-01-15", "type": "income", "description": "Miete Jan", "amount": 1190, "account_id": 1, "category_id": 1, "tax_treatment": "standard"},
      {"date": "2026-02-15", "type": "income", "description": "Miete Feb", "amount": 1190, "account_id": 1, "category_id": 1, "tax_treatment": "standard"},
      {"date": "2026-03-15", "type": "income", "description": "Miete Mär", "amount": 1190, "account_id": 1, "category_id": 1, "tax_treatment": "standard"}
    ]
  }'
```

```bash
# Transfer money between accounts
curl -X POST https://your-host/api/v1/transfers \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "date": "2026-01-20",
    "amount": 200.00,
    "from_account_id": 1,
    "to_account_id": 2,
    "description": "Bargeldabhebung"
  }'
```

```bash
# Look up categories first to find the right category_id
curl https://your-host/api/v1/categories?type=income \
  -H "Authorization: Bearer your-api-key"
```

```bash
# Get yearly summary (includes account balances)
curl https://your-host/api/v1/summary?year=2026 \
  -H "Authorization: Bearer your-api-key"
```

```bash
# Filter transactions by account
curl "https://your-host/api/v1/transactions?account_id=1&year=2026" \
  -H "Authorization: Bearer your-api-key"
```
