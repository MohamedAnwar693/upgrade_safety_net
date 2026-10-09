import json
import logging
from collections import defaultdict

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .utils import commit

_logger = logging.getLogger(__name__)

SEVERITY_RANK = {'critical': 3, 'warning': 2, 'info': 1}

KINDS = [
    ('table_dropped', 'Table vanished while holding data'),
    ('table_emptied', 'Table emptied'),
    ('rows_reduced', 'Rows reduced'),
    ('column_dropped', 'Field vanished while holding data'),
    ('data_wiped', 'Field data wiped'),
    ('data_reduced', 'Field data reduced'),
    ('metric_changed', 'Total shifted'),
    ('metric_missing', 'Total no longer available'),
    ('metric_error', 'Total could not be computed'),
    ('metric_new', 'New total row'),
    ('custom_missing', 'Customisation vanished'),
    ('custom_deactivated', 'Customisation deactivated'),
    ('custom_changed', 'Customisation changed'),
    ('module_missing', 'Module no longer installed'),
    ('module_state', 'Module in unexpected state'),
]
CATEGORY_OF = {
    'table_dropped': 'data', 'table_emptied': 'data', 'rows_reduced': 'data',
    'column_dropped': 'data', 'data_wiped': 'data', 'data_reduced': 'data',
    'metric_changed': 'totals', 'metric_missing': 'totals', 'metric_error': 'totals', 'metric_new': 'totals',
    'custom_missing': 'custom', 'custom_deactivated': 'custom', 'custom_changed': 'custom',
    'module_missing': 'modules', 'module_state': 'modules',
}


def _pct(before, after):
    """Signed percentage change; 0 when both are 0."""
    if not before:
        return 0.0 if not after else 100.0
    return (after - before) * 100.0 / abs(before)


class UsnComparison(models.Model):
    _name = 'usn.comparison'
    _description = 'Upgrade Safety Net Comparison'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(compute='_compute_name', store=True)
    before_id = fields.Many2one('usn.snapshot', string='Before (reference)', required=True, ondelete='restrict')
    after_id = fields.Many2one(
        'usn.snapshot', string='After', ondelete='restrict', copy=False,
        help="Leave empty to take a new snapshot of the current database.")
    state = fields.Selection(
        [('waiting', 'Waiting for snapshots'), ('done', 'Done'), ('failed', 'Failed')],
        default='waiting', readonly=True, copy=False)
    auto_created = fields.Boolean(readonly=True, copy=False)
    ran_on = fields.Datetime(readonly=True, copy=False)
    error = fields.Text(readonly=True, copy=False)
    before_series = fields.Char(related='before_id.odoo_series')
    after_series = fields.Char(related='after_id.odoo_series')
    after_state = fields.Selection(related='after_id.state')
    after_progress = fields.Float(related='after_id.progress')

    line_ids = fields.One2many('usn.comparison.line', 'comparison_id', string='Findings')
    critical_count = fields.Integer(compute='_compute_counts', store=True)
    warning_count = fields.Integer(compute='_compute_counts', store=True)
    info_count = fields.Integer(compute='_compute_counts', store=True)
    verdict = fields.Selection(
        [('clean', 'No differences found'), ('attention', 'Needs attention'), ('critical', 'Possible data loss')],
        compute='_compute_counts', store=True)

    @api.depends('before_id.name', 'after_id.name')
    def _compute_name(self):
        for rec in self:
            rec.name = '%s → %s' % (rec.before_id.name or '?', rec.after_id.name or _('current database'))

    @api.depends('line_ids.severity', 'line_ids.state')
    def _compute_counts(self):
        for rec in self:
            lines = rec.line_ids.filtered(lambda l: l.state == 'open')
            rec.critical_count = len(lines.filtered(lambda l: l.severity == 'critical'))
            rec.warning_count = len(lines.filtered(lambda l: l.severity == 'warning'))
            rec.info_count = len(lines.filtered(lambda l: l.severity == 'info'))
            if rec.critical_count:
                rec.verdict = 'critical'
            elif rec.warning_count:
                rec.verdict = 'attention'
            else:
                rec.verdict = 'clean'

    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        snapshots = self.env['usn.snapshot']
        for vals in vals_list:
            if not vals.get('after_id'):
                before = snapshots.browse(vals['before_id'])
                vals['after_id'] = snapshots.create({'name': _('After: %s') % before.name}).id
        return super().create(vals_list)

    @api.constrains('before_id', 'after_id')
    def _check_different(self):
        for rec in self:
            if rec.before_id == rec.after_id:
                raise ValidationError(_("Choose two different snapshots."))

    def _action_lines(self, severity):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Findings'),
            'res_model': 'usn.comparison.line',
            'view_mode': 'list,form',
            'domain': [('comparison_id', '=', self.id), ('severity', '=', severity)],
            'context': {'default_comparison_id': self.id},
        }

    def action_view_critical(self):
        return self._action_lines('critical')

    def action_view_warning(self):
        return self._action_lines('warning')

    def action_view_info(self):
        return self._action_lines('info')

    def action_rerun(self):
        for rec in self:
            if rec.before_id.state == 'done' and rec.after_id.state == 'done':
                rec._run()
        return True

    # ------------------------------------------------------------------
    @api.model
    def _process_waiting(self):
        for comp in self.search([('state', '=', 'waiting')]):
            before, after = comp.before_id, comp.after_id
            if 'failed' in (before.state, after.state):
                comp.write({'state': 'failed', 'error': _("A snapshot failed; see the snapshot for details.")})
                commit(self.env)
            elif before.state == 'done' and after.state == 'done':
                try:
                    comp._run()
                    commit(self.env)
                except Exception as exc:  # noqa: BLE001
                    _logger.exception("Comparison %s failed", comp.id)
                    self.env.cr.rollback()
                    self.env.invalidate_all()
                    comp.write({'state': 'failed', 'error': str(exc)})
                    commit(self.env)

    def _run(self):
        self.ensure_one()
        cfg = self.env['usn.config']._get()
        before, after = self.before_id, self.after_id
        self.line_ids.unlink()
        vals = []
        vals += self._diff_tables(before, after, cfg)
        vals += self._diff_metrics(before, after, cfg)
        vals += self._diff_custom(before, after)
        vals += self._diff_modules(before, after)
        for v in vals:
            v['comparison_id'] = self.id
        self.env['usn.comparison.line'].create(vals)
        self.write({'state': 'done', 'ran_on': fields.Datetime.now(), 'error': False})
        self._notify()

    def _notify(self):
        self.ensure_one()
        partners = self.create_uid.partner_id
        if self.auto_created:
            group = self.env.ref('base.group_system')
            for fname in ('all_user_ids', 'user_ids', 'users'):
                if fname in group._fields:
                    partners |= group.sudo()[fname].partner_id
                    break
        text = _(
            "Upgrade comparison finished: %(c)s critical, %(w)s warning and %(i)s informational findings.",
        ) % {'c': self.critical_count, 'w': self.warning_count, 'i': self.info_count}
        self.message_post(
            body=Markup('<p>%s</p>') % text, partner_ids=partners.ids,
            message_type='notification', subtype_xmlid='mail.mt_comment')

    # ------------------------------------------------------------------
    # diff: tables & fields
    # ------------------------------------------------------------------
    @staticmethod
    def _line(kind, severity, subject, **kw):
        vals = {'kind': kind, 'severity': severity, 'subject': subject}
        vals.update(kw)
        return vals

    def _diff_tables(self, before, after, cfg):
        out = []
        thr, crit = cfg.shrink_threshold_pct, cfg.critical_shrink_pct
        min_rows, hint_min = cfg.min_rows, cfg.rename_hint_min_filled
        tb = {t.name: t for t in before.table_ids}
        ta = {t.name: t for t in after.table_ids}
        cols_b = {n: json.loads(t.columns_json or '{}') for n, t in tb.items() if not t.error}
        cols_a = {n: json.loads(t.columns_json or '{}') for n, t in ta.items() if not t.error}

        new_tables_by_rows = defaultdict(list)
        for name, t in ta.items():
            if name not in tb and t.row_count >= hint_min:
                new_tables_by_rows[t.row_count].append(name)
        new_cols = defaultdict(list)  # (filled, type) -> ["table.column"]
        for name, cols in cols_a.items():
            old = cols_b.get(name, {})
            for col, (filled, dtype) in cols.items():
                if filled >= hint_min and col not in old:
                    new_cols[(filled, dtype)].append('%s.%s' % (name, col))

        orm_fields = {}

        def orm_state(model, col):
            """None: unknown model, 'missing', True (stored) or False (not stored)."""
            if not model:
                return None
            if not orm_fields:
                for rec in self.env['ir.model.fields'].sudo().search_read([], ['model', 'name', 'store']):
                    orm_fields[(rec['model'], rec['name'])] = rec['store']
                orm_fields[('', '')] = True  # sentinel: cache loaded
            return orm_fields.get((model, col), 'missing')

        for name, t in sorted(tb.items()):
            if t.error or name not in cols_b:
                continue
            a = ta.get(name)
            if a is None:
                if t.row_count > 0:
                    cand = new_tables_by_rows.get(t.row_count, [])
                    out.append(self._line(
                        'table_dropped', 'warning' if cand else 'critical', name,
                        model=t.model, table=name, before=str(t.row_count), after='—',
                        delta=-t.row_count, delta_pct=-100.0,
                        hint=_("A new table has exactly the same number of rows: %s. This may be a rename.")
                        % ', '.join(cand[:3]) if cand else _("The table no longer exists.")))
                continue
            if a.error or name not in cols_a:
                continue

            rows_b, rows_a = t.row_count, a.row_count
            if rows_b > 0 and rows_a == 0:
                out.append(self._line(
                    'table_emptied', 'critical', name, model=t.model, table=name,
                    before=str(rows_b), after='0', delta=-rows_b, delta_pct=-100.0,
                    hint=_("The table still exists but all rows are gone.")))
                continue
            if rows_b >= min_rows and rows_a < rows_b:
                pct = _pct(rows_b, rows_a)
                if -pct >= thr:
                    out.append(self._line(
                        'rows_reduced', 'critical' if -pct >= crit else 'warning', name,
                        model=t.model, table=name, before=str(rows_b), after=str(rows_a),
                        delta=rows_a - rows_b, delta_pct=pct))

            for col, (filled_b, dtype) in cols_b[name].items():
                if filled_b <= 0:
                    continue
                subject = '%s.%s' % (name, col)
                entry = cols_a[name].get(col)
                if entry is None:
                    hints = []
                    cand = new_cols.get((filled_b, dtype), [])
                    if cand:
                        hints.append(_("A new column holds exactly the same number of values: %s. "
                                       "This may be a rename or move.") % ', '.join(cand[:3]))
                    state = orm_state(t.model, col)
                    if state == 'missing':
                        hints.append(_("The field is no longer defined in the ORM."))
                    elif state is False:
                        hints.append(_("The field still exists but is no longer stored."))
                    out.append(self._line(
                        'column_dropped', 'warning' if (cand or state is False) else 'critical', subject,
                        model=t.model, table=name, column=col, before=str(filled_b), after='—',
                        delta=-filled_b, delta_pct=-100.0, hint=' '.join(hints) or False))
                    continue
                filled_a = entry[0]
                if filled_a == 0:
                    out.append(self._line(
                        'data_wiped', 'critical', subject, model=t.model, table=name, column=col,
                        before=str(filled_b), after='0', delta=-filled_b, delta_pct=-100.0,
                        hint=_("The column exists but no longer holds any value.")))
                elif filled_a < filled_b and filled_b >= min_rows:
                    # ignore shrinkage that is merely explained by fewer rows
                    ratio_b = filled_b / rows_b if rows_b else 0
                    ratio_a = filled_a / rows_a if rows_a else 0
                    pct = _pct(filled_b, filled_a)
                    if -pct >= thr and ratio_a < ratio_b * (1 - thr / 100.0):
                        out.append(self._line(
                            'data_reduced', 'critical' if -pct >= crit else 'warning', subject,
                            model=t.model, table=name, column=col, before=str(filled_b),
                            after=str(filled_a), delta=filled_a - filled_b, delta_pct=pct))
        return out

    # ------------------------------------------------------------------
    # diff: totals
    # ------------------------------------------------------------------
    def _diff_metrics(self, before, after, cfg):
        out = []
        mb = {(m.code, m.key): m for m in before.metric_ids}
        ma = {(m.code, m.key): m for m in after.metric_ids}
        codes_after = {m.code for m in after.metric_ids}
        rows_per_code = defaultdict(int)
        for (code, _key), m in mb.items():
            if not m.error:
                rows_per_code[code] += 1
        reported_missing = set()

        for (code, key), m in sorted(mb.items()):
            if m.error:
                continue
            a = ma.get((code, key))
            subject = '%s › %s' % (m.name, m.label or m.key)
            before_txt = '%.2f / %d' % (m.value, m.count)
            if a is None:
                if code not in codes_after:
                    if code not in reported_missing:
                        reported_missing.add(code)
                        out.append(self._line(
                            'metric_missing', 'critical', m.name, before=_('%d row(s)') % rows_per_code[code],
                            after='—', hint=_("This total no longer returns any data after the upgrade.")))
                else:
                    out.append(self._line(
                        'metric_missing', 'critical', subject, before=before_txt, after='—',
                        delta=-m.value, delta_pct=-100.0, hint=_("This row no longer exists.")))
                continue
            if a.error:
                out.append(self._line(
                    'metric_error', 'warning', subject, before=before_txt, after=a.error[:200],
                    hint=_("The query failed after the upgrade (a table or column may have changed).")))
                continue
            dv, dc = a.value - m.value, a.count - m.count
            if abs(dv) > m.tolerance or dc:
                pct = _pct(m.value, a.value) if abs(dv) > m.tolerance else 0.0
                critical = dc < 0 or (abs(dv) > m.tolerance and abs(pct) >= cfg.metric_critical_pct)
                out.append(self._line(
                    'metric_changed', 'critical' if critical else 'warning', subject,
                    before=before_txt, after='%.2f / %d' % (a.value, a.count),
                    delta=dv, delta_pct=pct,
                    hint=_("Record count changed by %+d.") % dc if dc else False))
        for (code, key), a in sorted(ma.items()):
            if a.error or (code, key) in mb or code not in {c for c, _k in mb}:
                continue
            out.append(self._line(
                'metric_new', 'info', '%s › %s' % (a.name, a.label or a.key),
                before='—', after='%.2f / %d' % (a.value, a.count)))
        return out

    # ------------------------------------------------------------------
    # diff: customisations & modules
    # ------------------------------------------------------------------
    def _diff_custom(self, before, after):
        out = []
        ca = {(c.kind, c.res_id): c for c in after.custom_ids}
        for c in before.custom_ids:
            a = ca.get((c.kind, c.res_id))
            subject = '%s › %s' % (c.kind, c.label)
            if a is None:
                out.append(self._line(
                    'custom_missing', 'critical', subject, model=c.kind, before=_('present'), after='—',
                    hint=_("This customised record no longer exists (or now belongs to a module).")))
            elif c.active and not a.active:
                out.append(self._line(
                    'custom_deactivated', 'warning', subject, model=c.kind,
                    before=_('active'), after=_('inactive'),
                    hint=_("Upgrades often deactivate custom records they cannot validate.")))
            elif c.hash != a.hash:
                out.append(self._line(
                    'custom_changed', 'warning' if c.kind == 'ir.ui.view' else 'info', subject,
                    model=c.kind, before=c.hash[:8], after=a.hash[:8],
                    hint=_("The definition was modified during the upgrade.")))
        return out

    def _diff_modules(self, before, after):
        out = []
        ma = {m.name: m for m in after.module_ids}
        for m in before.module_ids:
            if m.state != 'installed':
                continue
            a = ma.get(m.name)
            if a is None or a.state != 'installed':
                out.append(self._line(
                    'module_missing', 'warning', m.name, before=m.version or m.state,
                    after=(a.state if a else _('absent')),
                    hint=_("Standard modules are sometimes merged or renamed between versions; "
                           "custom modules must be ported.")))
        for a in after.module_ids:
            if a.state in ('to upgrade', 'to install', 'to remove'):
                out.append(self._line(
                    'module_state', 'warning', a.name, before='—', after=a.state,
                    hint=_("The module is stuck in this state; the upgrade may not have completed.")))
        return out


class UsnComparisonLine(models.Model):
    _name = 'usn.comparison.line'
    _description = 'Upgrade Safety Net Finding'
    _order = 'rank desc, kind, id'

    comparison_id = fields.Many2one('usn.comparison', required=True, ondelete='cascade', index=True)
    kind = fields.Selection(KINDS, required=True)
    category = fields.Selection(
        [('data', 'Data loss'), ('totals', 'Totals'), ('custom', 'Customisations'), ('modules', 'Modules')],
        compute='_compute_category', store=True)
    severity = fields.Selection(
        [('critical', 'Critical'), ('warning', 'Warning'), ('info', 'Info')], required=True)
    rank = fields.Integer(compute='_compute_rank', store=True)
    subject = fields.Char(required=True)
    model = fields.Char()
    table = fields.Char()
    column = fields.Char()
    before = fields.Char()
    after = fields.Char()
    delta = fields.Float(digits=(20, 4))
    delta_pct = fields.Float(string='Change %')
    hint = fields.Text()
    state = fields.Selection([('open', 'Open'), ('ack', 'Acknowledged')], default='open', required=True)
    ack_user_id = fields.Many2one('res.users', string='Acknowledged by', readonly=True)
    ack_note = fields.Char(string='Note')

    @api.depends('kind')
    def _compute_category(self):
        for rec in self:
            rec.category = CATEGORY_OF.get(rec.kind)

    @api.depends('severity')
    def _compute_rank(self):
        for rec in self:
            rec.rank = SEVERITY_RANK.get(rec.severity, 0)

    def action_acknowledge(self):
        self.write({'state': 'ack', 'ack_user_id': self.env.user.id})

    def action_reopen(self):
        self.write({'state': 'open', 'ack_user_id': False})
