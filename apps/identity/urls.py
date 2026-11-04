from django.urls import path

from .views import (
    ChangePasswordView, LoginView, LogoutView, MeView, MFADisableView, MFAEnableView, MFASetupView,
    MFAStatusView, PasswordResetConfirmView,
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
    path("mfa/", MFAStatusView.as_view(), name="mfa-status"),
    path("mfa/setup/", MFASetupView.as_view(), name="mfa-setup"),
    path("mfa/enable/", MFAEnableView.as_view(), name="mfa-enable"),
    path("mfa/disable/", MFADisableView.as_view(), name="mfa-disable"),
    path("password-reset/", PasswordResetRequestView.as_view(), name="password-reset"),
    path("password-reset/validate/", PasswordResetValidateView.as_view(), name="password-reset-validate"),
    path("password-reset/confirm/", PasswordResetConfirmView.as_view(), name="password-reset-confirm"),
    path("verify-email/", VerifyEmailView.as_view(), name="verify-email"),
    path("verify-email/resend/", ResendVerificationEmailView.as_view(), name="verify-email-resend"),
]
