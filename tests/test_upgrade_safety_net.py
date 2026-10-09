import json
import time

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestUpgradeSafetyNet(TransactionCase):

    def _snapshot(self, tables=('res_partner', 'res_users', 'res_company')):
        cfg = self.env['usn.config']._get()
        snap = self.env['usn.snapshot'].create({})
        snap._start(cfg)
        snap.pending_tables = json.dumps(list(tables))
        snap.tables_total = len(tables)
        snap._process(time.monotonic() + 600, cfg)
        self.assertEqual(snap.state, 'done', snap.error)
        return snap

    def _compare(self, before, after):
        comp = self.env['usn.comparison'].create({'before_id': before.id, 'after_id': after.id})
        comp._run()
        return comp

    def test_identical_snapshots_are_clean(self):
        a, b = self._snapshot(), self._snapshot()
        comp = self._compare(a, b)
        self.assertFalse(comp.line_ids.filtered(lambda l: l.severity == 'critical'))

    def test_vanished_column_is_critical(self):
        a, b = self._snapshot(), self._snapshot()
        table = b.table_ids.filtered(lambda t: t.name == 'res_partner')
        cols = json.loads(table.columns_json)
        self.assertTrue(cols['name'][0] > 0)
        del cols['name']
        table.columns_json = json.dumps(cols)
        comp = self._compare(a, b)
        line = comp.line_ids.filtered(lambda l: l.kind == 'column_dropped' and l.column == 'name')
        self.assertEqual(line.severity, 'critical')
        self.assertEqual(comp.verdict, 'critical')

    def test_wiped_column_and_dropped_table(self):
        a, b = self._snapshot(), self._snapshot()
        table = b.table_ids.filtered(lambda t: t.name == 'res_partner')
        cols = json.loads(table.columns_json)
        cols['name'][0] = 0
        table.columns_json = json.dumps(cols)
        b.table_ids.filtered(lambda t: t.name == 'res_company').unlink()
        comp = self._compare(a, b)
        self.assertTrue(comp.line_ids.filtered(lambda l: l.kind == 'data_wiped'))
        self.assertTrue(comp.line_ids.filtered(lambda l: l.kind == 'table_dropped'))

    def test_shifted_total_is_reported(self):
        a, b = self._snapshot(), self._snapshot()
        metric = b.metric_ids.filtered(lambda m: m.code == 'partners')[:1]
        self.assertTrue(metric)
        metric.count -= 1
        comp = self._compare(a, b)
        self.assertTrue(comp.line_ids.filtered(lambda l: l.kind == 'metric_changed'))

    def test_export_import_roundtrip(self):
        a = self._snapshot()
        data = json.loads(json.dumps(a._export_dict()))
        imported = self.env['usn.snapshot']._import_dict(data)
        self.assertEqual(len(imported.table_ids), len(a.table_ids))
        self.assertEqual(len(imported.metric_ids), len(a.metric_ids))
        self.assertTrue(imported.imported)

    def test_metric_query_validation(self):
        with self.assertRaises(Exception):
            self.env['usn.metric.definition'].create({
                'name': 'bad', 'code': 'bad', 'query': 'DELETE FROM res_partner'})
        with self.assertRaises(Exception):
            self.env['usn.metric.definition'].create({
                'name': 'bad2', 'code': 'bad2', 'query': 'SELECT 1; SELECT 2'})
