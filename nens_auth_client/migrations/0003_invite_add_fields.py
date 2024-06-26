# Generated by Django 2.2.16 on 2020-11-27 04:07

from django.conf import settings
from django.db import migrations
from django.db import models

import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("nens_auth_client", "0002_invite"),
    ]

    operations = [
        migrations.AddField(
            model_name="invitation",
            name="email",
            field=models.EmailField(
                default="testuser@testserver.com",
                help_text="The email address to which this invitation is / will be sent",
                max_length=254,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="invitation",
            name="email_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="invitation",
            name="user",
            field=models.ForeignKey(
                blank=True,
                help_text="Optionally associate this invitation to an existing local user. If set, the external user will be associated to this local useruser. Otherwise, a new user will be created.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="invitations_received",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
