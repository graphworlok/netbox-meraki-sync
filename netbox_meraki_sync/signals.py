"""
App-ready signal handler — ensures custom fields exist on dcim.site the first
time the plugin loads, so users see the meraki_network_id and meraki_site_name
fields on Site forms immediately after installation.
"""

from django.apps import apps


def _ensure_site_custom_fields() -> None:
    """
    Create the two Meraki custom fields on dcim.site if they do not exist.
    Silently no-ops if the database is not yet initialised (e.g. during tests
    or before the first migration run).
    """
    try:
        from django.contrib.contenttypes.models import ContentType
        from extras.models import CustomField

        # Lazy import to avoid circular imports during app startup
        Site = apps.get_model("dcim", "Site")
        site_ct = ContentType.objects.get_for_model(Site)

        fields = [
            {
                "name": "meraki_network_id",
                "label": "Meraki Network ID",
                "description": (
                    "The Meraki Dashboard network ID for this site "
                    "(e.g. N_xxxxxxxxxxxx).  Populate this to enable automatic "
                    "device sync from that Meraki network."
                ),
            },
            {
                "name": "meraki_site_name",
                "label": "Meraki Site Name",
                "description": (
                    "Human-readable Meraki network name.  Populated automatically "
                    "by the sync command; can also be set manually."
                ),
            },
        ]

        for spec in fields:
            cf, _created = CustomField.objects.get_or_create(
                name=spec["name"],
                defaults={
                    "label": spec["label"],
                    "description": spec["description"],
                    "type": "text",
                    "required": False,
                    "ui_editable": "yes",
                },
            )
            if site_ct not in cf.object_types.all():
                cf.object_types.add(site_ct)

    except Exception:
        # Database may not be ready yet (migrations not run).  This is harmless
        # — the management command also calls this helper before each sync run.
        pass


# Run once when the plugin's AppConfig.ready() fires.
_ensure_site_custom_fields()
