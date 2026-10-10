# Decision Log

## D20 — Malformed upload row isolation

- CSV and XLSX headers use a case-, whitespace-, and punctuation-insensitive alias map in API configuration. The platform, link, and at least one text or headline column are required; a missing header rejects the whole file with `missing_required_columns` and names the missing fields.
- A row-number column is optional. Recognized aliases are Row, #, ad no, id, No., and row number. If present, row values must be positive integers or integral floats; if absent, valid ads use the 1-based data-row index.
- CSV records with a field count different from the header are isolated. In XLSX, a row with a non-empty cell beyond the header width is isolated. XLSX ingestion reads only the first worksheet.
- Trailing fully empty records are ignored. A fully empty record inside the data is isolated as `empty_record` and counts toward data-row indexes.
- Isolation checks row structure and required field values only. It does not judge ad quality, creative, UTMs, platform identity, client host, link scheme, or suspicious/injected text. Whitespace-only cells count as empty.
- The 200-ad limit counts valid ads only. More than 200 valid ads rejects the upload with `too_many_ads`; malformed rows do not count toward the limit.
- Each rejected row carries its 1-based data-row index, raw row number or null, reason code, human-readable reason, and raw cells as inert text truncated to 500 characters per cell. Valid rows continue. Rejected rows must be persisted with their batch.
- An unparseable CSV (including parser errors, unterminated quotes, NUL bytes, or non-UTF-8; UTF-8 BOM is allowed) rejects the whole file. Corrupt, encrypted, or macro-enabled XLSX files reject the whole file as unreadable.
- Validation against all 30 labeled sheets is a SYNTHETIC final evaluation only: isolated rows must match `malformed: true` labels exactly (41 rows), with every other row accepted. Held-out sheets must not be used for tuning.
