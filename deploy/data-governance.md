# Data governance

Status: written for the current release. Every row marked **gap** needs a decision or work before the system
handles real customer data.

## What is stored, and where

| Data | Where | Why it is kept | Who can read it | Retention |
|---|---|---|---|---|
| Test-run transcripts (every caller and agent line, heard text, timings) | `runs`, `turns` tables (SQLite file) | Evaluation evidence and reports | Any role, including `viewer` (**gap**: transcripts are visible to all roles) | `voiceshop prune --older-than-days N` (manual or scheduled; **gap**: no schedule is set) |
| Audit trail (who started a suite, changed settings, cleared history, approved refunds) | `audit_events` table | Compliance record | Read through the API by any role (**gap**: restrict to admins) | Kept indefinitely; the database refuses edits and deletes |
| Approved refunds | `refunds` table | Prevents a second approval for the same order | Internal only | Kept with the audit trail |
| Storefront sessions (carts, orders, saved address, test card) | `shop_sessions` table | Lets a shopper resume | Shoppers' own session only (by id) | Removed after 24 hours without change |
| API keys | Key file (hashes only) | Authentication | Operators with file access | Until revoked |
| Logs | stderr (JSON) | Operations | Operators | Set by the log collector (**gap**: no collector configured) |

## What the data contains

- Test data only: the customers, orders, addresses, emails and test card are **fictional**. The test card is a
  published test number. Nothing in the repository is a real person's data.
- Transcripts can contain whatever a caller says. In production they would contain personal data.
- Logs are redacted for email addresses, phone-like numbers and key-like values, including extra fields and
  exception text. Redaction is pattern-based: it is a safety net, not a guarantee.

## Controls in place

- Named keys with roles; keys stored only as SHA-256 hashes; keys shown once.
- Changes are audited against the person who made them; the audit trail is append-only in the database.
- Storefront state and refunds are durable and survive restarts.
- Secrets come from the environment only; `.env` files are git-ignored; `check-config` never prints values.

## Gaps

1. **Transcript access:** every role can read transcripts. Restrict transcript reading to the operator and admin roles, or add a `viewer` that sees metrics only.
2. **Audit access:** restrict the audit endpoint to admins.
3. **Encryption at rest:** the database and key file are not encrypted by the application. Use encrypted disks.
4. **Backups:** no backup is automated. Back up the database file while the server is stopped, or use SQLite's online backup.
5. **Data subject requests:** there is no per-customer search or deletion. A real deployment needs one.
6. **Lawful basis and privacy assessment:** not done. Required before any real customer data is used.
7. **Retention schedule:** the prune command exists; no schedule is set and no period is chosen.
8. **Logs in a collector:** no central log store is configured.
