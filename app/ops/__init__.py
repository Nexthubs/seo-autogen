"""P9-C operations tooling: cleanup + backup (spec P9 "Cleanup / Backup").

Both are manual CLI tools (run from the web/worker container or host):

* ``python3 -m app.ops.cleanup  [--dry-run] [--days N] [--job-id UUID]``
* ``python3 -m app.ops.backup   [--restore ARCHIVE] [--apply]``

They are deliberately NOT background tasks: nothing deletes data on a
timer. Scheduling (e.g. cron / docker exec) is an operator decision.
"""
