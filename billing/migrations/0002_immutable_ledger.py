from django.db import migrations


TABLES=('billing_payment','billing_allocation','billing_paymentreversal','billing_cashclosure','billing_cashclosureitem')


def install(apps,schema_editor):
    if schema_editor.connection.vendor!='postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("""CREATE FUNCTION fireisp_immutable_ledger() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Ledger entries are immutable; create a reversal' USING ERRCODE='23514'; END; $$""")
        for table in TABLES:
            cursor.execute(f'CREATE TRIGGER immutable_ledger BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION fireisp_immutable_ledger()')


def uninstall(apps,schema_editor):
    if schema_editor.connection.vendor!='postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        for table in TABLES:
            cursor.execute(f'DROP TRIGGER IF EXISTS immutable_ledger ON {table}')
        cursor.execute('DROP FUNCTION IF EXISTS fireisp_immutable_ledger()')


class Migration(migrations.Migration):
    dependencies=[('billing','0001_initial')]
    operations=[migrations.RunPython(install,uninstall)]
