"""Data fixes shared by the alembic chain and the startup migration path.

`main._run_migrations` baselines create_all-built field databases with a stamp
instead of replaying the chain, and `_schema_state` compares table and column
names only: a data-only revision is invisible to that comparison, so a stamp
that lands on head would silently skip its UPDATEs. The revision that carries a
data fix calls this helper on the normal upgrade route, and the startup path
re-runs it after both of its own paths.
"""

from sqlalchemy import Connection, inspect, text

_SIDE_VALUE_TABLES = ("item_locations", "storage_bins")
_SIDE_VALUE_RENAMES = (
    ("left_module", "front_module"),
    ("right_module", "back_module"),
)


def rename_module_side_values(connection: Connection, *, forward: bool) -> None:
    """Rename the stored left/right module side values to front/back (or back).

    Each UPDATE only touches rows that still carry the source value, so the
    function is a no-op once the rename has happened and is safe to run on
    every startup path.
    """
    for table in _SIDE_VALUE_TABLES:
        if inspect(connection).has_table(table) is False:
            continue

        for old_value, new_value in _SIDE_VALUE_RENAMES:
            source, target = (old_value, new_value) if forward else (new_value, old_value)
            connection.execute(text(f"UPDATE {table} SET side = '{target}' WHERE side = '{source}'"))
