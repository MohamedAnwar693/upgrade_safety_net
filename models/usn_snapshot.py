import json
import logging
import time

from odoo import _, api, fields, models, release

from .utils import (
    commit, existing_tables, filled_expression, quote_ident, sandbox,
)

_logger = logging.getLogger(__name__)

# Customisations worth tracking. Only records WITHOUT an xmlid from a regular
# module (i.e. created through the UI / Studio / imports) are captured.
CUSTOM_MODELS = [
    'ir.ui.view', 'ir.ui.menu', 'ir.actions.act_window', 'ir.actions.server',
    'ir.actions.report', 'ir.cron', 'ir.rule', 'mail.template', 'base.automation',
]
# Row keys that legitimately change on their own and must not alter the hash.
VOLATILE_KEYS = [
    'write_date', 'write_uid', 'create_date', 'create_uid', 'nextcall', 'lastcall',
    'numbercall', 'failure_count', 'first_failure_date', 'last_failure_date',
]
COLUMNS_PER_QUERY = 150
MAX_METRIC_ROWS = 5000
EXPORT_FORMAT = 'usn-snapshot'
EXPORT_VERSION = 1


class UsnSnapshot(models.Model):
    _name = 'usn.snapshot'
    _description = 'Upgrade Safety Net Snapshot'
    _order = 'taken_on desc, id desc'

    name = fields.Char(
        required=True,
        default=lambda self: _('Snapshot %s') % fields.Datetime.now())
    state = fields.Selection(
        [('queued', 'Queued'), ('running', 'Running'), ('done', 'Done'), ('failed', 'Failed')],
        default='queued', readonly=True, required=True, copy=False)
    taken_on = fields.Datetime(default=fields.Datetime.now, readonly=True, copy=False)
    user_id = fields.Many2one('res.users', string='Taken by', default=lambda self: self.env.user, readonly=True)
    odoo_series = fields.Char(string='Odoo series', readonly=True, copy=False)
    odoo_version = fields.Char(string='Odoo version', readonly=True, copy=False)
    db_name = fields.Char(string='Database', readonly=True, copy=False)
    imported = fields.Boolean(readonly=True, copy=False)
    note = fields.Text()
    error = fields.Text(readonly=True, copy=False)

    # work-in-progress bookkeeping
    meta_done = fields.Boolean(readonly=True, copy=False)
    pending_tables = fields.Text(readonly=True, copy=False)
    tables_total = fields.Integer(readonly=True, copy=False)
    tables_done = fields.Integer(readonly=True, copy=False)
    progress = fields.Float(compute='_compute_progress')

    table_ids = fields.One2many('usn.snapshot.table', 'snapshot_id')
    metric_ids = fields.One2many('usn.snapshot.metric', 'snapshot_id')
    custom_ids = fields.One2many('usn.snapshot.custom', 'snapshot_id')
    module_ids = fields.One2many('usn.snapshot.module', 'snapshot_id')
    comparison_ids = fields.One2many('usn.comparison', 'before_id', string='Comparisons')

    table_count = fields.Integer(compute='_compute_counts')
    metric_count = fields.Integer(compute='_compute_counts')
    custom_count = fields.Integer(compute='_compute_counts')
    module_count = fields.Integer(compute='_compute_counts')
    needs_comparison = fields.Boolean(compute='_compute_needs_comparison')

    # ------------------------------------------------------------------
    # computes
    # ------------------------------------------------------------------
    @api.depends('state', 'tables_total', 'tables_done')
    def _compute_progress(self):
        for rec in self:
            if rec.state == 'done':
                rec.progress = 100.0
            elif rec.tables_total:
                rec.progress = rec.tables_done * 100.0 / rec.tables_total
            else:
                rec.progress = 0.0

    def _compute_counts(self):
        for rec in self:
            rec.table_count = self.env['usn.snapshot.table'].search_count([('snapshot_id', '=', rec.id)])
            rec.metric_count = self.env['usn.snapshot.metric'].search_count([('snapshot_id', '=', rec.id)])
            rec.custom_count = self.env['usn.snapshot.custom'].search_count([('snapshot_id', '=', rec.id)])
            rec.module_count = self.env['usn.snapshot.module'].search_count([('snapshot_id', '=', rec.id)])

    def _compute_needs_comparison(self):
        for rec in self:
            rec.needs_comparison = bool(
                rec.state == 'done' and rec.odoo_series
                and rec.odoo_series != release.series and not rec.comparison_ids)

    # ------------------------------------------------------------------
    # CRUD / user actions
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if any(r.state == 'queued' for r in records):
            self._trigger_cron()
        return records

    @api.model
    def _trigger_cron(self):
        cron = self.env.ref('upgrade_safety_net.ir_cron_usn_process', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    @api.model
    def action_take_snapshot(self):
        snap = self.create({})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'usn.snapshot',
            'res_id': snap.id,
            'view_mode': 'form',
        }

    def action_retry(self):
        self.filtered(lambda s: s.state == 'failed').write({'state': 'queued', 'error': False})
        self._trigger_cron()

    def action_compare_now(self):
        """Take a fresh snapshot of the current database and compare it with this one."""
        self.ensure_one()
        comparison = self.env['usn.comparison'].create({'before_id': self.id})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'usn.comparison',
            'res_id': comparison.id,
            'view_mode': 'form',
        }

    def action_export(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': '/usn/snapshot/%d/export' % self.id,
            'target': 'self',
        }

    def _open_children(self, model, name):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': model,
            'view_mode': 'list,form',
            'domain': [('snapshot_id', '=', self.id)],
            'context': {'default_snapshot_id': self.id},
        }

    def action_view_tables(self):
        return self._open_children('usn.snapshot.table', _('Tables'))

    def action_view_metrics(self):
        return self._open_children('usn.snapshot.metric', _('Totals'))

    def action_view_custom(self):
        return self._open_children('usn.snapshot.custom', _('Customisations'))

    def action_view_modules(self):
        return self._open_children('usn.snapshot.module', _('Modules'))

    # ------------------------------------------------------------------
    # scheduler
    # ------------------------------------------------------------------
    @api.model
    def _cron_process(self):
        cfg = self.env['usn.config']._get()
        self._detect_upgrade(cfg)
        deadline = time.monotonic() + cfg.time_budget
        for snap in self.search([('state', 'in', ('queued', 'running'))], order='id'):
            snap._process(deadline, cfg)
            if time.monotonic() >= deadline:
                break
        if self.search_count([('state', 'in', ('queued', 'running'))]):
            self._trigger_cron()  # keep going with the next slice
        self.env['usn.comparison']._process_waiting()

    @api.model
    def _detect_upgrade(self, cfg):
        """After an upgrade, compare the last snapshot with the current database."""
        if not cfg.auto_compare:
            return
        if self.search_count([('state', 'in', ('queued', 'running'))]):
            return
        last = self.search([('state', '=', 'done')], limit=1)
        if not last or not last.odoo_series or last.odoo_series == release.series or last.comparison_ids:
            return
        _logger.info("Upgrade detected (%s -> %s): starting comparison", last.odoo_series, release.series)
        self.env['usn.comparison'].create({'before_id': last.id, 'auto_created': True})
        commit(self.env)

    def _process(self, deadline, cfg):
        self.ensure_one()
        try:
            if self.state == 'queued':
                self._start(cfg)
            if self.state == 'running':
                if not self.meta_done:
                    self._collect_meta(cfg)
                    self.meta_done = True
                    commit(self.env)
                self._scan_tables(deadline, cfg)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Snapshot %s failed", self.id)
            self.env.cr.rollback()
            self.env.invalidate_all()
            self.write({'state': 'failed', 'error': str(exc)})
            commit(self.env)

    def _start(self, cfg):
        cr = self.env.cr
        cr.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
            " AND NOT (table_name LIKE ANY(%s::text[])) ORDER BY table_name",
            [cfg.get_excluded_patterns()],
        )
        tables = [r[0] for r in cr.fetchall()]
        self.write({
            'state': 'running',
            'odoo_series': release.series,
            'odoo_version': release.version,
            'db_name': cr.dbname,
            'taken_on': fields.Datetime.now(),
            'pending_tables': json.dumps(tables),
            'tables_total': len(tables),
            'tables_done': 0,
            'meta_done': False,
            'error': False,
        })
        # restart from scratch if the snapshot was retried
        self.table_ids.unlink()
        self.metric_ids.unlink()
        self.custom_ids.unlink()
        self.module_ids.unlink()
        commit(self.env)

    # ------------------------------------------------------------------
    # collectors: modules, customisations, totals
    # ------------------------------------------------------------------
    def _collect_meta(self, cfg):
        self._collect_modules()
        self._collect_custom(cfg)
        self._collect_metrics(cfg)

    def _collect_modules(self):
        cr = self.env.cr
        cr.execute("SELECT name, state, latest_version FROM ir_module_module ORDER BY name")
        self.env['usn.snapshot.module'].create([
            {'snapshot_id': self.id, 'name': n, 'state': s, 'version': v or ''}
            for n, s, v in cr.fetchall()
        ])

    def _collect_custom(self, cfg):
        cr = self.env.cr
        timeout = cfg.statement_timeout * 1000
        vals = []
        for model in CUSTOM_MODELS:
            if model not in self.env:
                continue
            table = self.env[model]._table
            if not existing_tables(cr, [table]):
                continue
            try:
                with sandbox(cr, timeout) as scr:
                    scr.execute(
                        "SELECT t.id, COALESCE((to_jsonb(t) ->> 'active')::boolean, TRUE),"
                        " md5((to_jsonb(t) - %%s::text[])::text)"
                        " FROM %s t WHERE NOT EXISTS ("
                        "   SELECT 1 FROM ir_model_data d"
                        "   WHERE d.model = %%s AND d.res_id = t.id"
                        "   AND d.module NOT IN ('__export__', 'studio_customization'))" % quote_ident(table),
                        [VOLATILE_KEYS, model],
                    )
                    rows = scr.fetchall()
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Custom scan of %s failed: %s", model, exc)
                continue
            labels = self._labels_for(model, [r[0] for r in rows])
            for res_id, active, digest in rows:
                vals.append({
                    'snapshot_id': self.id, 'kind': model, 'res_id': res_id,
                    'label': labels.get(res_id) or '%s,%s' % (model, res_id),
                    'active': bool(active), 'hash': digest,
                })
        self.env['usn.snapshot.custom'].create(vals)

    def _labels_for(self, model, ids):
        """Display names for record ids; never raises."""
        ids = [int(i) for i in ids]
        if not ids or model not in self.env:
            return {}
        try:
            records = self.env[model].with_context(active_test=False).browse(ids).exists()
            return {r.id: r.display_name for r in records}
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Could not resolve labels for %s: %s", model, exc)
            return {}

    def _collect_metrics(self, cfg):
        timeout = cfg.statement_timeout * 1000
        definitions = self.env['usn.metric.definition'].search([])
        out = []
        for definition in definitions:
            base = {
                'snapshot_id': self.id, 'definition_id': definition.id, 'code': definition.code,
                'group': definition.group, 'name': definition.name, 'tolerance': definition.tolerance,
            }
            if not definition._is_available():
                continue
            try:
                rows = definition._execute(timeout)[:MAX_METRIC_ROWS]
            except Exception as exc:  # noqa: BLE001
                out.append(dict(base, key='', error=str(exc).strip()[:500]))
                continue
            keys = [str(r[0]) for r in rows]
            labels = {}
            if definition.label_model:
                numeric = [k for k in keys if k.isdigit()]
                labels = {str(k): v for k, v in self._labels_for(definition.label_model, numeric).items()}
            for row in rows:
                row = tuple(row) + (0, 0)
                key = str(row[0])
                out.append(dict(
                    base, key=key, label=labels.get(key, ''),
                    value=float(row[1] or 0), count=int(row[2] or 0)))
        self.env['usn.snapshot.metric'].create(out)

    # ------------------------------------------------------------------
    # collector: tables / columns
    # ------------------------------------------------------------------
    def _scan_tables(self, deadline, cfg):
        cr = self.env.cr
        pending = json.loads(self.pending_tables or '[]')
        if pending:
            cr.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = ANY(%s)"
                " ORDER BY table_name, ordinal_position", [pending])
            columns = {}
            for table, column, dtype in cr.fetchall():
                columns.setdefault(table, []).append((column, dtype))
            model_by_table = {
                cls._table: name for name, cls in self.env.registry.models.items()
                if getattr(cls, '_table', None) and not cls._abstract
            }
            excluded = cfg.get_excluded_columns()
            timeout = cfg.statement_timeout * 1000
            batch = []
            while pending and time.monotonic() < deadline:
                table = pending.pop(0)
                batch.append(self._scan_table(
                    table, columns.get(table, []), excluded, timeout, model_by_table.get(table)))
                if len(batch) >= 25:
                    self._flush_tables(batch, pending)
                    batch = []
            self._flush_tables(batch, pending)
        if not pending:
            self.write({'state': 'done', 'tables_done': self.tables_total, 'pending_tables': '[]'})
        commit(self.env)

    def _flush_tables(self, batch, pending):
        if batch:
            self.env['usn.snapshot.table'].create(batch)
        self.write({
            'pending_tables': json.dumps(pending),
            'tables_done': self.tables_total - len(pending),
        })
        commit(self.env)

    def _scan_table(self, table, columns, excluded, timeout, model):
        """Count rows and filled values per column with one scan per 150 columns."""
        cols = [(c, t) for c, t in columns if c not in excluded]
        filled, rows, error = {}, 0, False
        try:
            with sandbox(self.env.cr, timeout) as cr:
                chunks = [cols[i:i + COLUMNS_PER_QUERY] for i in range(0, len(cols), COLUMNS_PER_QUERY)] or [[]]
                for chunk in chunks:
                    exprs = ''.join(', ' + filled_expression(c, t) for c, t in chunk)
                    cr.execute('SELECT COUNT(*)%s FROM %s' % (exprs, quote_ident(table)))
                    result = cr.fetchone()
                    rows = result[0]
                    for (c, t), value in zip(chunk, result[1:]):
                        filled[c] = [value, t]
        except Exception as exc:  # noqa: BLE001
            error = str(exc).strip()[:500]
            filled, rows = {}, -1
        return {
            'snapshot_id': self.id,
            'name': table,
            'model': model or False,
            'row_count': rows,
            'column_count': len(filled),
            'filled_column_count': sum(1 for v in filled.values() if v[0] > 0),
            'columns_json': json.dumps(filled, separators=(',', ':')),
            'error': error,
        }

    # ------------------------------------------------------------------
    # export / import
    # ------------------------------------------------------------------
    def _export_dict(self):
        self.ensure_one()
        return {
            'format': EXPORT_FORMAT,
            'format_version': EXPORT_VERSION,
            'name': self.name,
            'taken_on': fields.Datetime.to_string(self.taken_on),
            'odoo_series': self.odoo_series,
            'odoo_version': self.odoo_version,
            'db_name': self.db_name,
            'note': self.note or '',
            'modules': [
                {'name': m.name, 'state': m.state, 'version': m.version} for m in self.module_ids],
            'tables': [{
                'name': t.name, 'model': t.model or '', 'row_count': t.row_count,
                'column_count': t.column_count, 'filled_column_count': t.filled_column_count,
                'columns_json': t.columns_json, 'error': t.error or '',
            } for t in self.table_ids],
            'metrics': [{
                'code': m.code, 'group': m.group, 'name': m.name, 'key': m.key, 'label': m.label or '',
                'value': m.value, 'count': m.count, 'tolerance': m.tolerance, 'error': m.error or '',
            } for m in self.metric_ids],
            'custom': [{
                'kind': c.kind, 'res_id': c.res_id, 'label': c.label, 'active': c.active, 'hash': c.hash,
            } for c in self.custom_ids],
        }

    @api.model
    def _import_dict(self, data):
        if not isinstance(data, dict) or data.get('format') != EXPORT_FORMAT:
            raise ValueError(_("This file is not an Upgrade Safety Net snapshot export."))
        if data.get('format_version', 0) > EXPORT_VERSION:
            raise ValueError(_("This export was created by a newer version of the module."))
        snap = self.create({
            'name': _('%s (imported)') % data.get('name', 'Snapshot'),
            'state': 'done', 'imported': True,
            'taken_on': data.get('taken_on') or fields.Datetime.now(),
            'odoo_series': data.get('odoo_series'), 'odoo_version': data.get('odoo_version'),
            'db_name': data.get('db_name'), 'note': data.get('note'),
            'meta_done': True, 'pending_tables': '[]',
            'tables_total': len(data.get('tables', [])), 'tables_done': len(data.get('tables', [])),
        })
        sid = snap.id
        self.env['usn.snapshot.module'].create([dict(m, snapshot_id=sid) for m in data.get('modules', [])])
        self.env['usn.snapshot.table'].create([dict(t, snapshot_id=sid) for t in data.get('tables', [])])
        self.env['usn.snapshot.metric'].create([dict(m, snapshot_id=sid) for m in data.get('metrics', [])])
        self.env['usn.snapshot.custom'].create([dict(c, snapshot_id=sid) for c in data.get('custom', [])])
        return snap


class UsnSnapshotTable(models.Model):
    _name = 'usn.snapshot.table'
    _description = 'Snapshot: table inventory'
    _order = 'name'

    snapshot_id = fields.Many2one('usn.snapshot', required=True, ondelete='cascade', index=True)
    name = fields.Char(string='Table', required=True)
    model = fields.Char(string='Model')
    row_count = fields.Integer(string='Rows')
    column_count = fields.Integer(string='Columns')
    filled_column_count = fields.Integer(string='Columns holding data')
    columns_json = fields.Text(string='Columns (JSON: name -> [filled, type])')
    error = fields.Char()


class UsnSnapshotMetric(models.Model):
    _name = 'usn.snapshot.metric'
    _description = 'Snapshot: key total'
    _order = 'group, name, key'

    snapshot_id = fields.Many2one('usn.snapshot', required=True, ondelete='cascade', index=True)
    definition_id = fields.Many2one('usn.metric.definition', ondelete='set null')
    code = fields.Char(required=True)
    group = fields.Char()
    name = fields.Char()
    key = fields.Char()
    label = fields.Char()
    value = fields.Float(digits=(20, 4))
    count = fields.Integer()
    tolerance = fields.Float(default=0.005)
    error = fields.Char()


class UsnSnapshotCustom(models.Model):
    _name = 'usn.snapshot.custom'
    _description = 'Snapshot: customised record'
    _order = 'kind, label'

    snapshot_id = fields.Many2one('usn.snapshot', required=True, ondelete='cascade', index=True)
    kind = fields.Char(string='Model', required=True)
    res_id = fields.Integer(string='Record ID')
    label = fields.Char()
    active = fields.Boolean()
    hash = fields.Char(string='Content hash')


class UsnSnapshotModule(models.Model):
    _name = 'usn.snapshot.module'
    _description = 'Snapshot: module state'
    _order = 'name'

    snapshot_id = fields.Many2one('usn.snapshot', required=True, ondelete='cascade', index=True)
    name = fields.Char(required=True)
    state = fields.Char()
    version = fields.Char()
