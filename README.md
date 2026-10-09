# Upgrade Safety Net (Odoo 19.0)

An in-database, no-code safety check for Odoo upgrades (written for 19 → 20, works for any series jump).

**Author:** Mohamed Anwar
## Workflow
1. **Before the upgrade** – *Upgrade Safety Net ▸ Take Snapshot Now*. It runs in the background (sliced by the scheduler, resumable, safe on big databases).
2. **Export it** (*Export (JSON)* on the snapshot). Keep the file outside the database.
3. **Upgrade.** The module's tables travel with the database if the module is ported to the new series.
   If the upgrade platform drops the module, install it again and use *Configuration ▸ Import Snapshot*.
4. **After the upgrade** the scheduler notices the series changed and automatically snapshots + compares
   (disable in Settings). Or click *Compare with current database* on the old snapshot.
5. Review **Findings** (critical / warning / info), acknowledge what is expected, print the PDF report.

## What is captured
* Every table (excluding configurable patterns): row count and, per column, how many values are non-empty
  (booleans: true, numbers: non-zero, text: non-empty, others: not NULL).
* 23 built-in totals (users, contacts, stock by location/product, balances by account, open receivables/payables,
  invoices, payments, sales, purchases, CRM, MRP, POS, HR, attachments, chatter...). Add your own under
  *Configuration ▸ Watched Totals* (single read-only SELECT returning `key, value, count`).
* Customisations without a module xmlid: views, menus, window/server/report actions, crons, record rules,
  mail templates, automations (content hash + active flag).
* Installed modules and their states.

## What is reported
| Finding | Default severity |
|---|---|
| Table vanished / emptied while holding data | critical (warning if a table with the same row count appeared = rename hint) |
| Column vanished or wiped while holding data | critical (warning if same-count new column exists or field became non-stored) |
| Rows / filled values shrank beyond threshold | warning, critical from 50 % |
| Total shifted, vanished or record count dropped | warning / critical (≥ 1 % or fewer records) |
| Custom record missing / deactivated / changed | critical / warning / info (views: warning) |
| Module no longer installed or stuck `to upgrade` | warning |

## Safety
* Admin-only (`base.group_system`). All SQL runs inside a savepoint that is always rolled back, read-only, with a statement timeout.
* Metric queries must be a single SELECT/WITH statement.

## Porting
Manifest version is `19.0.1.0.0`. Before moving to 20.0 you do not need to change anything for the snapshot
to remain readable; for the new series bump the version and re-check view syntax and the `res.users`/`res.groups` field names.


## Notes
* Tested successfully on Odoo 19.0 in a working Odoo environment (verified installation, snapshots, and comparison workflow).
* Implementation is based on Odoo 19.0 sources and checked against relevant APIs and fields (including `ir.cron`, user/group fields, and the registry API). **However**, for production upgrades, it is still recommended to test the module and the upgrade process on a staging database first:
`odoo -d test -i upgrade_safety_net --test-tags /upgrade_safety_net --stop-after-init`
* Comparisons are schema-based: legitimately removed or changed data (for example, an obsolete column removed by Odoo during an upgrade) may appear as a finding. Such findings should be reviewed and acknowledged when the change is expected.