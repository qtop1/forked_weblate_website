# Generated by Django 2.2.3 on 2019-09-04 11:20

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("weblate_web", "0015_auto_20190807_1633")]

    operations = [
        migrations.AlterField(
            model_name="donation", name="payment", field=models.UUIDField(blank=True)
        )
    ]
