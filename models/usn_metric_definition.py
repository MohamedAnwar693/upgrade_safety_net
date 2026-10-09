import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .utils import existing_tables, sandbox

_SELECT_RE = re.compile(r'^\s*(select|with)\b', re.IGNORECASE)


class UsnMetricDefinition(models.Model):
    _name = 'usn.metric.definition'
    _description = 'Upgrade Safety Net Metric'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    code = fields.Char(required=True, help="Stable technical identifier used to match metrics across snapshots.")
    group = fields.Char(default='Custom')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    is_builtin = fields.Boolean(string='Built-in', readonly=True, copy=False)
    description = fields.Text()
    query = fields.Text(
        required=True,
        help="A read-only SQL SELECT returning three columns: key, value, count.\n"
             "key: text or record id identifying the row (e.g. a location id)\n"
             "value: the total to watch (quantity, balance, amount)\n"
             "count: number of records behind that total")
    required_tables = fields.Char(
        help="Comma separated tables that must exist; the metric is skipped silently when one is missing "
             "(i.e. the app is not installed).")
    label_model = fields.Char(
        help="Optional model name. When the key is a record id, its display name is stored as label.")
    tolerance = fields.Float(default=0.005, help="Absolute difference in value that is ignored.")

    @api.constrains('code')
    def _check_code_unique(self):
        for rec in self:
            if self.with_context(active_test=False).search_count(
                    [('code', '=', rec.code), ('id', '!=', rec.id)]):
                raise ValidationError("The metric code must be unique.")

    @api.constrains('query')
    def _check_query(self):
        for rec in self:
            query = (rec.query or '').strip().rstrip(';').strip()
            if not _SELECT_RE.match(query) or ';' in query:
                raise ValidationError(
                    "Metric queries must be a single SELECT (or WITH ... SELECT) statement.")

    def _required_tables_list(self):
        self.ensure_one()
        return [t.strip() for t in (self.required_tables or '').split(',') if t.strip()]

    def _is_available(self):
        self.ensure_one()
        required = self._required_tables_list()
        return not required or existing_tables(self.env.cr, required) >= set(required)

    def _execute(self, timeout_ms):
        """Run the query in a read-only rolled-back savepoint and return the rows."""
        self.ensure_one()
        query = self.query.strip().rstrip(';').strip()
        with sandbox(self.env.cr, timeout_ms, read_only=True) as cr:
            cr.execute(query)
            return cr.fetchall()

    def action_test(self):
        """Run the query once and show the first rows in a notification."""
        self.ensure_one()
        cfg = self.env['usn.config']._get()
        try:
            rows = self._execute(cfg.statement_timeout * 1000)
            msg = "%d row(s). First rows: %s" % (len(rows), rows[:5])
            kind = 'success'
        except Exception as exc:  # noqa: BLE001 - shown to the admin
            msg, kind = str(exc), 'danger'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': self.name, 'message': msg, 'type': kind, 'sticky': kind == 'danger'},
        }
