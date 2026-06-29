from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView, TokenVerifyView

from .serializers import (
    CurrentUserSerializer,
    FoundryTokenObtainPairSerializer,
    LogoutSerializer,
)
from .throttles import (
    AuthIPThrottle,
    AuthManageIPThrottle,
    AuthRefreshThrottle,
    AuthUserThrottle,
)


class FoundryTokenObtainPairView(TokenObtainPairView):
    serializer_class = FoundryTokenObtainPairSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthIPThrottle, AuthUserThrottle]


class FoundryTokenRefreshView(TokenRefreshView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthRefreshThrottle]


class FoundryTokenVerifyView(TokenVerifyView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthRefreshThrottle]


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AuthManageIPThrottle]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"detail": "Logout successful."}, status=status.HTTP_200_OK)


class CurrentUserView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AuthManageIPThrottle]

    def get(self, request):
        serializer = CurrentUserSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)
