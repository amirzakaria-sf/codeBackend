from django.urls import path

from .views import (
    CurrentUserView,
    FoundryTokenObtainPairView,
    FoundryTokenRefreshView,
    FoundryTokenVerifyView,
    LogoutView,
)

urlpatterns = [
    path("token/", FoundryTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", FoundryTokenRefreshView.as_view(), name="token_refresh"),
    path("token/verify/", FoundryTokenVerifyView.as_view(), name="token_verify"),
    path("logout/", LogoutView.as_view(), name="auth_logout"),
    path("me/", CurrentUserView.as_view(), name="auth_me"),
]
