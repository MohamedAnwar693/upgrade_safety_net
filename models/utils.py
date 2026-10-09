"""Small helpers shared by the Upgrade Safety Net models."""
from contextlib import contextmanager

from odoo import tools

NUMERIC_TYPES = {'integer', 'bigint', 'smallint', 'numeric', 'double precision', 'real'}
TEXT_TYPES = {'character varying', 'text', 'character'}


def quote_ident(name):
    """Quote a PostgreSQL identifier (taken from information_schema)."""
    return '"%s"' % name.replace('"', '""')


def filled_expression(column, data_type):
    """SQL aggregate counting the rows where ``column`` holds meaningful data.

    * booleans: rows that are true
    * numbers: rows that are not NULL and not zero
    * text: rows that are not NULL and not empty
    * anything else (m2o, dates, jsonb, bytea...): rows that are not NULL
    """
    col = quote_ident(column)
    if data_type == 'boolean':
        return 'COUNT(*) FILTER (WHERE %s)' % col
    if data_type in NUMERIC_TYPES:
        return 'COUNT(*) FILTER (WHERE %s IS NOT NULL AND %s <> 0)' % (col, col)
    if data_type in TEXT_TYPES:
        return "COUNT(*) FILTER (WHERE %s IS NOT NULL AND %s::text <> '')" % (col, col)
    return 'COUNT(%s)' % col


@contextmanager
def sandbox(cr, timeout_ms=0, read_only=True):
    """Run statements inside a savepoint that is ALWAYS rolled back.

    The statement timeout and the read-only flag are ``SET LOCAL`` so they are
    reverted by the rollback and never leak into the surrounding transaction.
    """
    cr.execute('SAVEPOINT usn_sandbox')
    try:
        cr.execute(
            "SELECT set_config('statement_timeout', %s, true),"
            " set_config('transaction_read_only', %s, true)",
            [str(int(timeout_ms)), 'on' if read_only else 'off'],
        )
        yield cr
    finally:
        cr.execute('ROLLBACK TO SAVEPOINT usn_sandbox')
        cr.execute('RELEASE SAVEPOINT usn_sandbox')


def existing_tables(cr, names):
    names = list(names)
    if not names:
        return set()
    cr.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = current_schema() AND table_name = ANY(%s)",
        [names],
    )
    return {row[0] for row in cr.fetchall()}


def commit(env):
    """Commit between work chunks (never while tests are running)."""
    if tools.config['test_enable'] or tools.config['test_file']:
        return
    env.cr.commit()
