"""Enforce append-only ledger rows at the database.

The ORM guards in ``payments.models`` are convenience; this trigger is the
guarantee. It fires on any UPDATE or DELETE of a ledger row, no matter who
issues it (ORM, raw SQL, admin, psql). TRUNCATE is deliberately not blocked so
the test runner can reset tables between tests.
"""

from django.db import migrations


FORWARD = r"""
CREATE OR REPLACE FUNCTION payments_ledger_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'payments_ledgerentry is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER payments_ledgerentry_append_only
    BEFORE UPDATE OR DELETE ON payments_ledgerentry
    FOR EACH ROW EXECUTE FUNCTION payments_ledger_append_only();
"""

BACKWARD = r"""
DROP TRIGGER IF EXISTS payments_ledgerentry_append_only ON payments_ledgerentry;
DROP FUNCTION IF EXISTS payments_ledger_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(FORWARD, BACKWARD),
    ]
