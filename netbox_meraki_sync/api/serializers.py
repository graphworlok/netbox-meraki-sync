from rest_framework import serializers
from netbox.api.serializers import NetBoxModelSerializer

from ..models import SyncLog


class SyncLogSerializer(serializers.ModelSerializer):
    """Read-only serialiser for SyncLog records."""

    status_display = serializers.CharField(
        source="get_status_display", read_only=True
    )
    duration_seconds = serializers.SerializerMethodField()

    class Meta:
        model  = SyncLog
        fields = [
            "id",
            "network_id",
            "network_name",
            "site_name",
            "started_at",
            "completed_at",
            "duration_seconds",
            "status",
            "status_display",
            "message",
            "devices_seen",
            "devices_created",
            "devices_updated",
            "interfaces_synced",
            "macs_synced",
            "ips_synced",
        ]
        read_only_fields = fields

    def get_duration_seconds(self, obj) -> float | None:
        d = obj.duration
        return d.total_seconds() if d else None
