{
    'name': 'Upgrade Safety Net',
    'version': '19.0.1.0.0',
    'category': 'Technical',
    'summary': 'Snapshot your data before an Odoo upgrade and detect silent data loss afterwards',
    'description': """
        Upgrade Safety Net
        ==================
        Take a snapshot of your database *before* an upgrade (which fields hold data,
        row counts, stock quantities, account balances, user counts, custom views and
        other customisations). After the upgrade the module takes a second snapshot and
        diffs the two, reporting:
        
        * tables / fields that vanished while holding data (with rename hints)
        * tables and fields whose data shrank or was wiped
        * totals that shifted (stock, balances, open receivables/payables, orders ...)
        * custom views, actions, crons, rules, templates that disappeared or were deactivated
        * modules that are no longer installed
        
        Snapshots can be exported to JSON and re-imported, so they survive an upgrade
        even if the upgraded database no longer contains this module's tables.
    """,
    'author': 'Mohamed Anwar',
    'depends': ['base', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'data/usn_config_data.xml',
        'data/usn_metric_data.xml',
        'data/ir_cron_data.xml',
        'report/usn_comparison_report.xml',
        'views/usn_snapshot_views.xml',
        'views/usn_comparison_views.xml',
        'views/usn_metric_definition_views.xml',
        'wizard/usn_import_wizard_views.xml',
        'views/usn_menus.xml',
    ],
    'application': True,
    'installable': True,
}
