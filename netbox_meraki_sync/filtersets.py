import django_filters

from .choices import SyncStatusChoices
from .models import SyncLog


class SyncLogFilterSet(django_filters.FilterSet):
    q = django_filters.CharFilter(method="search", label="Search")
    status = django_filters.ChoiceFilter(choices=SyncStatusChoices)

    class Meta:
        model  = SyncLog
        fields = ["network_id", "status"]

    def search(self, queryset, name, value):
        return queryset.filter(
            network_id__icontains=value
        ) | queryset.filter(
            network_name__icontains=value
        ) | queryset.filter(
            site_name__icontains=value
        )
