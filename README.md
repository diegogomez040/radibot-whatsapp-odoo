# Radibot — WhatsApp Inventory & Sales Assistant for an Odoo Store

> A WhatsApp bot that lets the staff of an auto-parts store search stock, register sales and returns, and update products in **Odoo 19** using plain-language messages (typed or voice notes). A language model understands the request; deterministic code decides what is allowed to touch the inventory.

**Context:** built for and used daily by a Colombian store that sells vehicle cooling-system parts (radiators, hoses, water pumps, thermostats, fan clutches).
**Catalogue size:** roughly 800 product variants (hoses by diameter and length, elbows by colour and size), with photos stored in Odoo.
**Stack:** `Python 3.10` · `FastAPI` / `uvicorn` · `OpenAI gpt-4o-mini` + `Whisper` · `Odoo 19 (XML-RPC)` · `WhatsApp Cloud API` · `SQLite` · `systemd` + `Nginx`
**Size:** one ~4,100-line `bot.py`, plus 13 test, audit and migration scripts in `pruebas/`.
**Language note:** the people who use it speak Spanish, so the bot's replies and most code comments are in Spanish.

---

## Central Design Question

> How do you let a language model operate on a **live inventory** without ever letting it register a wrong or unconfirmed change?

The answer used here: the model only *proposes*. Every write goes through a typed tool, is shown to the user as a preview, waits for an explicit "sí", and is read back from Odoo afterwards. Around the model sit deterministic shortcuts and guards that fix the mistakes a small model actually makes (swapped product codes, a cost sent as a price, an unrequested confirmation).

---

## What It Does

- **Search** products by name, vehicle, code or measure (`"manguera superior aveo"`, `"corrugada de 2 por 26"`, `"5490"`), with photos on request.
- **Register sales** (one message can carry several products), with a confirmation preview and an undo.
- **Returns** of any earlier sale, using Odoo's own return flow, so the stock history stays correct.
- **Update** price, cost, name, code and stock, one entry per product, merged and previewed before applying.
- **Create** products with the right Odoo settings (storable, no tax).
- **Supplier order list:** when a customer asks for something that is missing, the bot offers to note it, and later shows what to order.
- **Voice notes** are transcribed with Whisper and normalised (dictated codes that arrive as `54.90` or `5 4 9 0` become `5490`).

---

## Design Highlights

| Problem seen in real use | What the code does |
|---|---|
| The model confirms in the same turn it proposes | A guard ignores any `confirmar` call without an explicit affirmation in the user's text |
| The model changes a digit of a typed product code | The code the user typed is restored before searching |
| A cost is sent as a sale price | A guard moves the value to the cost field when the message talks about cost |
| Several tool calls overwrite the single pending action | All calls of a turn are merged into one batch and one preview |
| "sí", "3" and "el segundo" are ambiguous for a small model | Deterministic shortcuts handle them before the model is called |
| Stock edits left no movement in Odoo's history | Stock is changed through Odoo's inventory-adjustment mechanism and deliveries are validated by Odoo |
| Variant prices could not be set one by one | Products were restructured to one attribute per variant so each price is independent |
| OpenAI balance ran out and the bot went silent | The user gets a clear WhatsApp message instead of silence |

More detail in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Repository Structure

```
bot.py                      the whole service: webhook, tools, shortcuts, guards, Odoo access
requirements.txt            Python dependencies
.env.example                every setting the bot reads (no real values)
docs/ARCHITECTURE.md        request flow, safety rails, Odoo modelling notes, testing, lessons learned
pruebas/                    tests, audits and data migrations
  copia_odoo.sh               throw-away Odoo on a restored backup, for risky tests
  probar_*_en_copia.py        suites that run against that copy (deliveries, stock, costs, variants...)
  probar_lista_pedidos.py     supplier-list tests with the real model, Odoo writes blocked
  auditar_modo_odoo.py        read-only audit: do Odoo's books agree with what the bot did?
  migrar_*.py                 one-off, reversible catalogue restructurings (dry-run, verify, backup first)
pc-respaldo/                off-server backup pulled by a Windows PC (PowerShell)
```

---

## How It Works

**1. Request path.** Meta calls `POST /webhook`. The signature (`X-Hub-Signature-256`) is verified, the sender must be on an allowlist (an empty allowlist ignores everyone), and audio is transcribed.
**2. Shortcuts first.** Messages whose meaning is certain ("sí" with a pending sale, "3" after a list, a bare product code, "se vendió la 5490, 5492, 5491") are answered by plain code and never reach the model.
**3. The model, with tools.** Everything else goes to `gpt-4o-mini` with 11 tools (`buscar`, `vender`, `actualizar`, `devolver`, `confirmar`...). Guards then correct the arguments before any handler runs.
**4. Preview and confirmation.** Writes become a *pending action* with a 10-minute lifetime. Nothing reaches Odoo until the user confirms.
**5. Odoo.** The handler writes through XML-RPC and reads the result back; on any mismatch it reports the failure instead of claiming success.
**6. Tests that cost money.** The model-dependent suites run against a copy of the production database, or against production with every write blocked.

---

## How to Run

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env     # then fill in your own values; never commit .env
```

You need an Odoo 19 instance, a WhatsApp Cloud API app and an OpenAI key.

### 3. Start the service

```bash
uvicorn bot:app --host 127.0.0.1 --port 8000
```

Put it behind HTTPS (for example Nginx with Let's Encrypt) and point the Meta webhook to `https://<your-domain>/webhook` with the same `WA_VERIFY_TOKEN`.

### 4. Tests

The suites in `pruebas/` need a reachable Odoo (ideally a restored copy, see `copia_odoo.sh`) and an OpenAI key. Run one with `python3 pruebas/probar_costo_en_copia.py`.

---

## Important Caveats

- **Single pending action per user.** A new search replaces a sale waiting for confirmation; this is a known simplification.
- **It depends on a language model.** Guards and shortcuts removed the failures seen so far, but a model can always surprise; that is why every write is confirmed.
- **Store-specific.** Category ids, product families (corrugated hoses, elbows) and several scripts assume the author's Odoo data.
- **Test scripts hit real services.** They are written for one deployment and use the real model, which costs money.
- **XML-RPC is deprecated in Odoo 19** and will be removed in Odoo 20; all Odoo access goes through a few helper functions to make that migration contained.

---

## What's *Not* in This Project (Honest Scope)

- No web interface, multi-store support or user management beyond the number allowlist.
- No invoicing: tax documents are handled by a separate tool.
- No continuous integration; tests are run by hand against a copy of the data.
- No sample data: the code expects an Odoo database with the store's products.

---

## Author

**Diego Gómez** — Data Analyst
[diegoagomez.040@gmail.com](mailto:diegoagomez.040@gmail.com)

Released under the MIT License (see `LICENSE`).
