from django.urls import path

from . import views

app_name = "netbox_meraki_sync"

urlpatterns = [
    path("sync-logs/", views.SyncLogListView.as_view(), name="synclog_list"),
    path("sync-logs/<int:pk>/", views.SyncLogView.as_view(), name="synclog"),
]
