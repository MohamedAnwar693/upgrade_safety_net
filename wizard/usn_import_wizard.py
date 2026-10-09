import base64
import json

from odoo import _, fields, models
from odoo.exceptions import UserError


class UsnImportWizard(models.TransientModel):
    _name = 'usn.import.wizard'
    _description = 'Import an Upgrade Safety Net snapshot'

    file = fields.Binary(string='Snapshot file (.json)', required=True)
    filename = fields.Char()

    def action_import(self):
        self.ensure_one()
        try:
            data = json.loads(base64.b64decode(self.file).decode('utf-8'))
            snapshot = self.env['usn.snapshot']._import_dict(data)
        except (ValueError, UnicodeDecodeError) as exc:
            raise UserError(_("Could not import the file: %s") % exc) from exc
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'usn.snapshot',
            'res_id': snapshot.id,
            'view_mode': 'form',
        }
