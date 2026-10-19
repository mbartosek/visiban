"""drf-spectacular view extensions for third-party views routed as-is.

Imported from ``AccountsConfig.ready()`` so the extensions are registered
before any schema is generated. Schema only: nothing here runs at request time.
"""

from drf_spectacular.extensions import OpenApiViewExtension
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers


class ResendEmailVerificationViewSchema(OpenApiViewExtension):
    """``POST /auth/registration/resend-email/`` answers 200, not 201 (#1783).

    dj-rest-auth's ``ResendEmailVerificationView`` is a ``CreateAPIView`` whose
    ``create()`` returns ``200 {"detail": "ok"}``. drf-spectacular's bundled
    dj-rest-auth extension declares only the response body, so the status fell
    back to the ``CreateAPIView`` default of 201. The view is routed without a
    project subclass (see ``accounts/tests/test_auth_routes.py``), so the fix is
    a higher-priority extension for the same class.
    """

    target_class = "dj_rest_auth.registration.views.ResendEmailVerificationView"
    priority = 1  # above drf_spectacular.contrib.rest_auth's extension (0)

    def view_replacement(self):
        class Fixed(self.target_class):
            @extend_schema(
                responses={
                    200: inline_serializer(
                        name="ResendEmailVerificationResponse",
                        fields={"detail": serializers.CharField()},
                    ),
                },
            )
            def post(self, request, *args, **kwargs):
                pass  # pragma: no cover

        return Fixed
