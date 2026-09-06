from django.db import migrations


def install(apps,schema_editor):
    if schema_editor.connection.vendor=='postgresql':
        with schema_editor.connection.cursor() as cursor:
            for table in ('billing_creditmemo','billing_refund'):
                cursor.execute(f'CREATE TRIGGER immutable_ledger BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION fireisp_immutable_ledger()')


def uninstall(apps,schema_editor):
    if schema_editor.connection.vendor=='postgresql':
        with schema_editor.connection.cursor() as cursor:
            for table in ('billing_creditmemo','billing_refund'):
                cursor.execute(f'DROP TRIGGER IF EXISTS immutable_ledger ON {table}')


class Migration(migrations.Migration):
    dependencies=[('billing','0003_creditmemo_refund_and_more')]
    operations=[migrations.RunPython(install,uninstall)]
