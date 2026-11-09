from sqlalchemy import CheckConstraint


def check_in(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    """CHECK (column IN (...)) - status/enum columns are plain text + CHECK so
    adding a value later is a simple, reversible migration."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)
