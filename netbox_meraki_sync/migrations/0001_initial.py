from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="SyncLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("network_id",    models.CharField(db_index=True, max_length=100)),
                ("network_name",  models.CharField(blank=True, max_length=200)),
                ("site_name",     models.CharField(blank=True, max_length=200)),
                ("started_at",    models.DateTimeField(auto_now_add=True, db_index=True)),
                ("completed_at",  models.DateTimeField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        db_index=True,
                        default="pending",
                        max_length=20,
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("success", "Success"),
                            ("failed",  "Failed"),
                        ],
                    ),
                ),
                ("message",           models.TextField(blank=True)),
                ("devices_seen",      models.PositiveIntegerField(default=0)),
                ("devices_created",   models.PositiveIntegerField(default=0)),
                ("devices_updated",   models.PositiveIntegerField(default=0)),
                ("interfaces_synced", models.PositiveIntegerField(default=0)),
                ("macs_synced",       models.PositiveIntegerField(default=0)),
                ("ips_synced",        models.PositiveIntegerField(default=0)),
            ],
            options={
                "verbose_name":        "Sync Log",
                "verbose_name_plural": "Sync Logs",
                "ordering": ["-started_at"],
            },
        ),
    ]
