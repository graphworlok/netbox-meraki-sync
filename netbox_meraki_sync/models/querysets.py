from django.db import models


class PluginQuerySet(models.QuerySet):
    """
    Minimal queryset that satisfies NetBox's ObjectListView.restrict() call.
    These internal plugin models don't require per-object ACL filtering.
    """

    def restrict(self, user, action="view"):
        return self
