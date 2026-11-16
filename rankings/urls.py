from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register("categories", views.CategoryViewSet, basename="category")
router.register("dishes", views.DishViewSet, basename="dish")
router.register("to-eat", views.ToEatViewSet, basename="to-eat")

urlpatterns = [
    path("hello/", views.hello),
    path("", include(router.urls)),
    path("auth/me/", views.me),
    path("auth/photo/", views.profile_photo),
    path("auth/logout/", views.logout_view),
    path("auth/google/", views.google_login),
    path("auth/delete-summary/", views.delete_summary),
    path("auth/delete/", views.delete_me),
    path("health/", views.health),
    path("search/", views.search),
    path("places/autocomplete/", views.place_suggestions),
]