from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0016_cierreturno_total_gastos_perfilusuario_permisos_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='negocio',
            name='fecha_vencimiento',
            field=models.DateField(blank=True, null=True),
        ),
    ]
