"""Post-create_all schema reconciliation.

`db.create_all()` only creates tables that don't exist; it never alters
existing ones. Constraints added after a database was first created are
therefore silently missing on that database. This module applies the small
set of such additions idempotently at boot. It is a stopgap for a real
migration tool (Flask-Migrate/Alembic) and should stay tiny.
"""
import logging

from sqlalchemy import inspect, text

from app import db

logger = logging.getLogger(__name__)

# (table, constraint name, DDL to add it). Only ever append here.
_LATE_CONSTRAINTS = (
    (
        'orders',
        'uq_order_number_period',
        'ALTER TABLE orders ADD CONSTRAINT uq_order_number_period '
        'UNIQUE (order_number, period_id)',
    ),
)


def ensure_schema():
    """Add constraints that create_all() would have skipped on a pre-existing
    database. Safe to call on every boot. Returns the list of constraint
    names that were added."""
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    added = []
    for table, name, ddl in _LATE_CONSTRAINTS:
        if table not in tables:
            continue
        existing = {c['name'] for c in inspector.get_unique_constraints(table)}
        if name in existing:
            continue
        if db.engine.dialect.name != 'postgresql':
            # SQLite cannot ADD CONSTRAINT; a dev DB must be recreated.
            logger.warning(
                'Constraint %s on %s is missing and cannot be added on %s. '
                'Delete the local database file and restart to recreate it.',
                name, table, db.engine.dialect.name,
            )
            continue
        try:
            with db.engine.begin() as conn:
                conn.execute(text(ddl))
            logger.info('Added missing constraint %s to %s', name, table)
            added.append(name)
        except Exception:
            # Most likely duplicate rows that violate the new constraint.
            # Log loudly but keep serving; the app degrades to pre-B3 behavior.
            logger.exception(
                'Could not add constraint %s to %s. Check for duplicate rows '
                'and add it manually.', name, table,
            )
    return added
