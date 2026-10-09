from odoo import api, fields, models
from odoo.exceptions import ValidationError

DEFAULT_EXCLUDED_TABLES = "\n".join([
    'usn_%', 'bus_%', 'ir_logging', 'ir_profile', 'ir_cron_trigger',
    'ir_cron_progress', 'res_users_log',
])


class UsnConfig(models.Model):
    _name = 'usn.config'
    _description = 'Upgrade Safety Net Settings'

    name = fields.Char(default='Upgrade Safety Net Settings', readonly=True)
    auto_compare = fields.Boolean(
        string='Auto-compare after an upgrade', default=True,
        help="When the Odoo series differs from the one of the latest snapshot, "
             "automatically take a new snapshot and compare it with that one.")
    shrink_threshold_pct = fields.Float(
        string='Report shrinkage above (%)', default=5.0,
        help="Row counts or filled-value counts that drop by at least this percentage are reported.")
    critical_shrink_pct = fields.Float(
        string='Critical shrinkage from (%)', default=50.0)
    min_rows = fields.Integer(
        string='Minimum rows to monitor shrinkage', default=10,
        help="Tables/fields with fewer rows than this are only reported when they vanish or are wiped.")
    metric_critical_pct = fields.Float(
        string='Critical total shift from (%)', default=1.0)
    rename_hint_min_filled = fields.Integer(
        string='Minimum filled values for rename hints', default=5)
    excluded_tables = fields.Text(
        string='Excluded tables (SQL LIKE patterns, one per line)',
        default=DEFAULT_EXCLUDED_TABLES)
    excluded_columns = fields.Char(
        string='Excluded columns (comma separated)',
        default='create_uid,write_uid,create_date,write_date')
    time_budget = fields.Integer(
        string='Seconds of work per scheduler run', default=45,
        help="The snapshot is processed in slices of this duration and resumed automatically.")
    statement_timeout = fields.Integer(
        string='SQL timeout per statement (s)', default=300)

    @api.constrains('shrink_threshold_pct', 'critical_shrink_pct', 'time_budget', 'statement_timeout')
    def _check_values(self):
        for rec in self:
            if rec.shrink_threshold_pct < 0 or rec.critical_shrink_pct < rec.shrink_threshold_pct:
                raise ValidationError(
                    "Critical shrinkage must be greater than or equal to the reporting threshold.")
            if rec.time_budget < 5 or rec.statement_timeout < 1:
                raise ValidationError("Time budget must be >= 5 s and SQL timeout >= 1 s.")

    @api.model
    def _get(self):
        return self.search([], limit=1) or self.create({})

    @api.model
    def action_open(self):
        cfg = self._get()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Settings',
            'res_model': 'usn.config',
            'res_id': cfg.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def get_excluded_patterns(self):
        self.ensure_one()
        return [p.strip() for p in (self.excluded_tables or '').splitlines() if p.strip()]

    def get_excluded_columns(self):
        self.ensure_one()
        return {c.strip() for c in (self.excluded_columns or '').split(',') if c.strip()} | {'id'}
