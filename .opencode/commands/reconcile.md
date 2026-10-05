---
description: Reconcile roster (universe) and apply safely
---
Load stocks-repo. Run `uv run stocks universe`. If errors present, refuse apply and list findings. If warnings only, confirm and run `uv run stocks universe --apply`. Report demotions/re-entries, membership_as_of vs added_on. Never delete rows.
