# Generated by Django 2.1.5 on 2019-02-01 15:27

import django.db.models.deletion
from django.db import migrations, models

import pretix.base.models.fields


class Migration(migrations.Migration):

    dependencies = [
        ('pretixbase', '0107_auto_20190129_1337'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='generate_tickets',
            field=models.NullBooleanField(verbose_name='Allow ticket download'),
        ),
    ]
