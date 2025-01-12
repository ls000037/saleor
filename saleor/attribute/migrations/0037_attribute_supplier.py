# Generated by Django 3.2.22 on 2023-10-24 01:43

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('account', '0085_alter_supplier_name'),
        ('attribute', '0036_assignedproductattributevalue_product_data_migration'),
    ]

    operations = [
        migrations.AddField(
            model_name='attribute',
            name='supplier',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='attributes', to='account.supplier'),
        ),
    ]
