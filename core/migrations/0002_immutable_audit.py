from django.db import migrations

def protect(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute("CREATE FUNCTION fireisp_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Audit records are immutable'; END $$")
        schema_editor.execute('CREATE TRIGGER audit_no_mutation BEFORE UPDATE OR DELETE ON core_auditevent FOR EACH ROW EXECUTE FUNCTION fireisp_audit_immutable()')

def unprotect(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('DROP TRIGGER IF EXISTS audit_no_mutation ON core_auditevent')
        schema_editor.execute('DROP FUNCTION IF EXISTS fireisp_audit_immutable()')

class Migration(migrations.Migration):
    dependencies = [('core', '0001_initial')]
    operations = [migrations.RunPython(protect, unprotect)]
