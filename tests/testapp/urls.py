from django.contrib import admin
from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views
from .api import EnrollmentViewSet, OrganizationViewSet, RoomViewSet, SpaceViewSet

router = DefaultRouter()
router.register("organizations", OrganizationViewSet)
router.register("enrollments", EnrollmentViewSet)
router.register("spaces", SpaceViewSet)
router.register("rooms", RoomViewSet)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("web/", views.EnrollmentListView.as_view(), name="web-list"),
    path("web/new/", views.EnrollmentCreateView.as_view(), name="web-create"),
    path("web/<int:pk>/", views.EnrollmentDetailView.as_view(), name="web-detail"),
    path("web/<int:pk>/edit/", views.EnrollmentUpdateView.as_view(), name="web-update"),
    path("web/<int:pk>/delete/", views.EnrollmentDeleteView.as_view(), name="web-delete"),
] + router.urls
