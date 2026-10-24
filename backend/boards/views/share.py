"""ShareBoardView — public read-only board access via share token."""

from drf_spectacular.utils import extend_schema, inline_serializer, OpenApiResponse
from rest_framework.generics import get_object_or_404
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from ..models import Board
from ..permissions import GROUP_ANCESTOR_SELECT_RELATED
from ..services.share_links import revoke_lapsed_share_links, share_enabler_has_standing
from ..serializers import PublicBoardSerializer


class ShareLinkThrottle(SimpleRateThrottle):
    """Rate-limit unauthenticated reads of public share-link boards.

    Scope 'share_link' maps to DEFAULT_THROTTLE_RATES['share_link'] in settings
    (120/hour in production).  Keyed on client IP so the limit applies per
    visitor, not per token.
    """

    scope = "share_link"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class ShareLinkTokenThrottle(SimpleRateThrottle):
    """Per-token rate limit for share-link reads (#988).

    Defense in depth on top of ``ShareLinkThrottle`` (per-IP).  UUID share
    tokens already provide 122 bits of entropy so brute-force enumeration is
    impractical; this throttle bounds the volume of legitimate-looking reads
    against any *single* token regardless of source IP, which is what
    catches CGNAT/proxy-pool abuse that would otherwise bypass the per-IP
    limit.

    Keyed on the token segment from the URL (passed via ``view.kwargs``).
    The throttle silently no-ops when no token is in scope.
    """

    scope = "share_link_token"

    def get_cache_key(self, request, view):
        token = (view.kwargs or {}).get("token")
        if not token:
            return None
        return self.cache_format % {
            "scope": self.scope,
            "ident": token,
        }


class ShareBoardView(APIView):
    """GET /api/share/<token>/ — public read-only board view, no authentication required.

    The UUID token is the sole credential. Returns a stripped board payload
    (columns, swimlanes, cards, labels) with all PII removed:
    - No user PKs or emails in assignee or any nested user object
    - No swimlane contact_email or notes
    - No comments, movement history, or member list
    - No share_token in the response body
    """

    permission_classes = []
    authentication_classes = []
    throttle_classes = [ShareLinkThrottle, ShareLinkTokenThrottle]

    @extend_schema(
        summary="Get a public read-only board by its share token",
        description="No authentication required. The UUID token is the sole credential.",
        responses={
            200: PublicBoardSerializer,
            404: OpenApiResponse(
                description=(
                    "Token is not a valid UUID, no board has that share token, or the "
                    "admin who enabled the link is no longer active or no longer an "
                    "admin of the board."
                ),
            ),
            410: OpenApiResponse(
                description="The share link has expired.",
                response=inline_serializer(name="ShareLinkExpired", fields={"detail": serializers.CharField()}),
            ),
        },
    )
    def get(self, request, token):
        import uuid as _uuid
        try:
            token_uuid = _uuid.UUID(token)
        except ValueError:
            raise Http404
        board = get_object_or_404(
            Board.objects.select_related("share_enabled_by", GROUP_ANCESTOR_SELECT_RELATED)
            .prefetch_related("columns", "swimlanes", "labels"),
            share_token=token_uuid,
        )
        # A link lasts while the admin who enabled it keeps admin standing on
        # the board (#1865). Checked on every read, before the expiry branch,
        # so a lapsed link answers like a revoked one. The board is cleared
        # here too, so a later return of that standing does not bring this
        # link back; the clear is conditional on this token and enabler, so
        # it never touches a link re-enabled meanwhile. Links with no recorded
        # enabler (enabled before 1.2) are not checked.
        if board.share_enabled_by_id is not None and not share_enabler_has_standing(
            board.share_enabled_by, board
        ):
            with transaction.atomic():
                revoke_lapsed_share_links(board_ids=[board.pk])
            raise Http404
        # TTL enforcement (#804): once the expiry has passed, stop serving the
        # board but keep returning 410 Gone rather than 404 so the caller can
        # tell "expired" apart from "never existed". Token is not auto-rotated
        # here — an admin must disable or regenerate it explicitly.
        if board.share_token_expires_at is not None and board.share_token_expires_at <= timezone.now():
            return Response(
                {"detail": "This share link has expired."},
                status=status.HTTP_410_GONE,
            )
        return Response(PublicBoardSerializer(board).data)
