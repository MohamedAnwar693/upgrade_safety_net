import json

from odoo import http
from odoo.http import request


class UpgradeSafetyNetController(http.Controller):

    @http.route('/usn/snapshot/<int:snapshot_id>/export', type='http', auth='user')
    def export_snapshot(self, snapshot_id, **kwargs):
        if not request.env.user.has_group('base.group_system'):
            return request.not_found()
        snapshot = request.env['usn.snapshot'].browse(snapshot_id).exists()
        if not snapshot:
            return request.not_found()
        body = json.dumps(snapshot._export_dict(), separators=(',', ':'))
        filename = 'usn_snapshot_%s_%s.json' % (snapshot.id, (snapshot.odoo_series or 'unknown').replace('/', '_'))
        return request.make_response(body, headers=[
            ('Content-Type', 'application/json'),
            ('Content-Disposition', 'attachment; filename="%s"' % filename),
        ])
