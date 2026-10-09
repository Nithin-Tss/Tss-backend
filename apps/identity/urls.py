from django.urls import path

from .views import (
    ChangePasswordView, LoginView, LogoutView, MeView, PasswordResetConfirmView,
    PasswordResetRequestView, PasswordResetValidateView, RefreshView,
    ResendVerificationEmailView, SignupView, VerifyEmailView,
)


urlpatterns = [
    path("signup/", SignupView.as_view(), name="signup"),
    path("login/", LoginView.as_view(), name="login"),
    path("refresh/", RefreshView.as_view(), name="token-refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("password/", ChangePasswordView.as_view(), name="change-password"),
    path("me/", MeView.as_view(), name="me"),
    path("password-reset/", PasswordResetRequestView.as_view(), name="password-reset"),
    path("password-reset/validate/", PasswordResetValidateView.as_view(), name="password-reset-validate"),
    path("password-reset/confirm/", PasswordResetConfirmView.as_view(), name="password-reset-confirm"),
    path("verify-email/", VerifyEmailView.as_view(), name="verify-email"),
    path("verify-email/resend/", ResendVerificationEmailView.as_view(), name="verify-email-resend"),
]
