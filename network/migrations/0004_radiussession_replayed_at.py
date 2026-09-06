from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('network', '0003_radiuscredential_state_revision')]
    operations = [migrations.AddField(model_name='radiussession', name='replayed_at', field=models.DateTimeField(null=True, blank=True))]
