# Personal Assistant — Beverley Fleet ops

Tools Claude uses to run Beverley Fleet's back office. Everything is draft-first: nothing sends, pays or submits without Cristin's go-ahead.

| Folder | What it is |
|---|---|
| `mcp/ctio/` | CTIO (Colorado express-lane) MCP server: look up notices by plate or notice number, match to renters, prepare/submit rental declarations of non-liability, prepare hearing requests. Payments stay manual. |
| `mcp/fleet-telematics/` | Vehicle data MCP server via Smartcar (Ford, Audi, Mercedes, Chevrolet): odometer, fuel, location, lock state; gated lock/unlock with audit log. |
| `turo-templates/` | Welcome / check-in / checkout templates per vehicle for Turo Scheduled Messages (published as a claude.ai artifact). |
| `welcome/` | Generator for per-booking welcome messages. |
| `guides/` | Rental Guide & FAQ PDFs: originals and east-side-only (Nov 3, 2026) updates. |

Setup for each MCP server is in its own README. Credentials come from 1Password (`op`) and are never committed.
