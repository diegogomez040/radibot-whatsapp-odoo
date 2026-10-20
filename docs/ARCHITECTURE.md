# Architecture Notes

How Radibot is organised, why it is built this way, and what went wrong along the way. Everything here is drawn from real use of the bot in a store.

---

## 1. Request Flow

```
WhatsApp (text / voice note)
        │  POST /webhook
        ▼
 signature check (X-Hub-Signature-256, HMAC-SHA256)   → 403 if invalid
        ▼
 sender allowlist (fail-closed: empty list ignores everyone)
        ▼
 audio? → Whisper → normalise dictated codes
        ▼
 deterministic shortcuts  ── answered here when the meaning is certain ──►  reply
        ▼
 OpenAI chat with 11 tools  → tool call(s)
        ▼
 guards fix the arguments (codes, cost vs price, option numbers, measures)
        ▼
 handlers: search / preview / pending action / Odoo write + read-back
        ▼
 reply (text, and photos if asked)
```

State is deliberately simple: `conversations.json` (chat history), `pending.json` (one pending action per user, 10-minute lifetime), `sales_log.json` (so any of the last 500 sales can be returned) and a small SQLite database for the supplier order list.

All timestamps are stored in UTC and converted to the store's timezone only for display.

---

## 2. The Model Proposes, Code Decides

| Layer | Role |
|---|---|
| **Shortcuts** | "sí"/"no" with a pending action, "3" after a list, "foto de la 2", a bare product code, "se vendió 5490, 5492, 5491". These never reach the model. |
| **Model** | Understands free text and chooses one of 11 tools: `buscar`, `vender`, `confirmar`, `crear`, `actualizar`, `deshacer_ultima_venta`, `resumen_ventas`, `ayuda`, `registrar_sin_stock`, `reporte_sin_stock`, `devolver`. |
| **Guards** | Repair the model's arguments before any handler runs (see below). |
| **Handlers** | Build a *pending action* and a preview. Only an explicit confirmation executes it. |

### Guards that exist because the model actually failed

- **Typed codes win.** A code the user typed (`8-97129-735-0`) replaces any near-identical code the model produced; a code that does not exist is reported as not found, never swapped for a neighbour.
- **Codes are not option numbers.** `la 5490` is a product, `la 3` is the third item of a list.
- **Cost is not price.** If the message speaks about cost and the model filled the price field, the value is moved.
- **No unrequested confirmations.** A `confirmar` call is ignored when the user's text has no affirmation, or when the same turn also proposed a new change (the new preview is shown instead).
- **Split codes.** "88 2606" is glued to `882606` only if that product exists and no piece is itself a code.
- **A bare number is an option.** After "Encontré varios productos…", "3" selects item 3 without asking the model.
- **Typos are suggested, not guessed.** If a code does not exist but exactly one product matches after deleting one digit, the bot asks "¿Quisiste decir…?" and still previews before any sale.

### One pending action, many tool calls

A single message can make several tool calls ("vendí 2 de la 5490 y 1 de la 5492"). Each call used to overwrite the previous pending action, so only the last product was saved. Now every call merges into one batch (replacing the quantity of a product that is already there) and one combined preview is shown, so one "sí" applies exactly what was displayed.

---

## 3. Modelling Odoo Correctly

- **Stock changes use Odoo's own mechanisms.** Direct writes on stock quantities changed the number but left no movement, so the history had gaps. Stock edits now go through the inventory-adjustment field, and sales validate the delivery so Odoo itself deducts stock. Returns use the standard return wizard. The result is read back and compared; a mismatch is reported, never hidden.
- **Variants.** In Odoo the name and base price belong to the template, while code, cost and stock belong to each variant, and a variant's price is `base price + sum of attribute extras`. A product with two shared attributes (diameter × length) cannot have an individual price per variant, so those products were restructured to **one attribute whose values are the full measure** (`"2 1/2, 20"`), with a base price of 0 and the price held in each value's extra.
- **Migrations are reversible.** Each restructuring creates the new product, moves codes, costs and stock, archives (never deletes) the old one and keeps an old→new id map so sales made before the change can still be returned. Every script supports a dry run, refuses to run without a fresh backup, verifies every variant afterwards and compares global totals (stock, accounting entries, open operations) before and after.
- **Old sales** keep pointing to the archived variants; the bot resolves them to the current ones when a return arrives.

---

## 4. Search

The search is a ladder, from most to least precise: category filter, then an ordered pattern over the product name (`inferior aveo` matches `inferior - Chevrolet Aveo`), then all words in any order, then any word. Results are scored and cut to five, with a "there are more" notice. Codes (including dictated ones such as `54.90`) are searched as typed first and as digits only second, because real codes with dashes exist. Measures travel as attributes, not as text, and a small parser understands both digits and words ("dos y media por veinte").

When the search only finds "the closest thing", the bot says so and offers to note the missing product for the supplier instead of presenting the wrong one as a match.

---

## 5. Testing Strategy

Writing to a real inventory makes ordinary tests dangerous: validated deliveries and returns create stock movements that cannot be deleted. So:

1. **A throw-away Odoo on a restored backup** (`pruebas/copia_odoo.sh`) runs the destructive suites: deliveries and returns, inventory adjustments, cost and price changes, variants and each migration.
2. **Model-in-the-loop checks with writes blocked.** The real model and real reads, with every write replaced by an exception, are run several times per scenario because a model is not deterministic. A change is only accepted when a scenario passes every run.
3. **Regression of the search** compares the old and new implementation on real queries and expects zero differences.
4. **A read-only audit** (`pruebas/auditar_modo_odoo.py`) checks that Odoo's books agree with what the bot did: order state, delivered quantities, stock after each sale, open or reserved operations, negative stock and unexpected accounting entries.

Because the model suites spend API credit, they should use a separate key with its own budget.

---

## 6. Operations

- **Backups:** a daily database dump and a weekly archive (database, attachments, code) with checksums. The archive is also *pulled* to a PC by a restricted, key-only, download-only SFTP account, because a copy on the same disk does not protect against losing the server (`pc-respaldo/`).
- **Hardening:** key-only SSH, a default-deny firewall, Odoo's database administration disabled and blocked at the proxy, Odoo listening only on localhost, webhook signature verification on, and a sender allowlist.
- **Disk and logs:** the journal is size-capped and the Odoo log is rotated daily; the disk is small, so each change keeps one rollback copy of `bot.py`.
- **Failure visibility:** when OpenAI is unreachable or out of credit, the user receives a clear message instead of silence, and failed WhatsApp deliveries are logged.

---

## 7. Lessons Learned

1. **A confirmed number is not a confirmed history.** Stock that matches but has no movement trail is a bug waiting for an audit.
2. **Make shortcuts for what is certain.** Every "sí" or option number that reached the model was eventually misread by it once.
3. **Merge, do not overwrite.** The single pending slot lost data until a turn's tool calls became one batch.
4. **Treat code-like strings as identifiers, never as text.** A language model happily "corrects" a part number into another real part number.
5. **Test the unhappy path with the real model.** Prompt edits that looked harmless broke a simple question (`tienes corrugada`) in four of ten runs; deterministic handling fixed it.
6. **Keep a way back.** Archive, do not delete; keep a map of old to new; take a backup right before every change.
