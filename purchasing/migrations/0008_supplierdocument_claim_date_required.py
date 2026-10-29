from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('purchasing', '0007_supplierdocument_claim_date'),
    ]

    operations = [
        migrations.AlterField(
            model_name='supplierdocument',
            name='claim_date',
            field=models.DateField(),
        ),
    ]
