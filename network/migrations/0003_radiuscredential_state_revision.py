from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('network', '0002_radiuscredential_commissioning')]
    operations = [migrations.AddField(model_name='radiuscredential', name='state_revision', field=models.PositiveIntegerField(default=0))]
