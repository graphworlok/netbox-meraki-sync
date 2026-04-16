from netbox.api.routers import NetBoxRouter

from .views import SyncLogViewSet

router = NetBoxRouter()
router.register("sync-logs", SyncLogViewSet)

urlpatterns = router.urls
