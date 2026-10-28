"""Initialize the database when the package is run as a module."""

from billing_assurance.generate import DEFAULT_DB_PATH, DEFAULT_LABELS_PATH, build_database
from billing_assurance.validate import format_report, validate_database


def main() -> int:
    stats = build_database(DEFAULT_DB_PATH, DEFAULT_LABELS_PATH)
    print(f"Initialized {DEFAULT_DB_PATH}")
    print(f"Wrote testing-only labels to {DEFAULT_LABELS_PATH}")
    print(f"Injected cases recorded outside the database: {stats['injected_cases']}")
    print()
    checker = validate_database(DEFAULT_DB_PATH, DEFAULT_LABELS_PATH)
    print(format_report(checker, DEFAULT_DB_PATH, DEFAULT_LABELS_PATH))
    return 1 if checker.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
