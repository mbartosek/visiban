"""Card comment, attachment and checklist writes use a role allow-list (#1829).

The comment and attachment write actions refused ``viewer`` by name, so any
other role, including one added later, was let through. They now check
``boards.services.cards.ANNOTATION_ROLES`` (collaborator, member, admin,
site admin), the same allow-list as the checklist actions, the way #875
fixed ``set_collapsed``. The four existing roles keep their behavior.

No request can reach these actions with an unrecognized role today
(``get_board_role`` fails closed and ``get_board_for_user`` answers 404), so
the unrecognized role is injected by patching ``CardViewSet._board_and_role``.
"""
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from boards.models import BoardMembership, Card, CardComment, Column, Swimlane
from boards.permissions import SITE_ADMIN
from boards.services.cards import ANNOTATION_ROLES
from boards.tests.conftest import _make_board, _make_membership, _make_user
from boards.views.cards import CardViewSet

NEW_ROLE = "auditor"


class AnnotationRoleAllowListTests(TestCase):
    def setUp(self):
        self.owner = _make_user("ann_owner")
        self.board = _make_board(self.owner, name="Annotations")
        col = Column.objects.create(board=self.board, name="To Do", position=0, allow_card_creation=True)
        swim = Swimlane.objects.create(board=self.board, name="General", position=0)
        self.card = Card.objects.create(
            board=self.board, column=col, swimlane=swim, created_by=self.owner, title="C", position=0,
        )
        self.base = f"/api/v1/boards/{self.board.id}/cards/{self.card.id}"
        self.user = _make_user("ann_user")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _writes(self):
        return {
            "comment create": lambda: self.client.post(f"{self.base}/comments/", {"body": "hi"}, format="json"),
            "comment delete": lambda: self.client.delete(f"{self.base}/comments/1/"),
            "attachment create": lambda: self.client.post(f"{self.base}/attachments/", {}, format="multipart"),
            "attachment delete": lambda: self.client.delete(f"{self.base}/attachments/1/"),
            "checklist create": lambda: self.client.post(f"{self.base}/checklist/", {"text": "x"}, format="json"),
            "checklist item": lambda: self.client.delete(f"{self.base}/checklist/1/"),
            "checklist reorder": lambda: self.client.post(f"{self.base}/checklist/reorder/", {"order": []}, format="json"),
        }

    def test_allow_list_is_the_four_annotating_roles(self):
        self.assertEqual(
            set(ANNOTATION_ROLES),
            {BoardMembership.Role.COLLABORATOR, BoardMembership.Role.MEMBER, BoardMembership.Role.ADMIN, SITE_ADMIN},
        )

    def test_a_role_outside_the_allow_list_is_refused_on_every_write(self):
        with patch.object(CardViewSet, "_board_and_role", return_value=(self.board, NEW_ROLE)):
            for name, call in self._writes().items():
                with self.subTest(action=name):
                    r = call()
                    self.assertEqual(r.status_code, 403, r.content)
                    self.assertEqual(r.json(), {"detail": "You do not have permission to perform this action."})
        self.assertFalse(CardComment.objects.exists())

    def test_viewer_keeps_its_documented_403_body(self):
        _make_membership(self.board, self.user, "viewer")
        for name in ("comment create", "comment delete", "attachment create", "attachment delete"):
            with self.subTest(action=name):
                r = self._writes()[name]()
                self.assertEqual(r.status_code, 403, r.content)
                self.assertEqual(r.json(), {"detail": "Viewers cannot perform this action."})

    def test_collaborator_and_member_may_still_comment_and_delete_their_own(self):
        for role in ("collaborator", "member"):
            with self.subTest(role=role):
                BoardMembership.objects.update_or_create(board=self.board, user=self.user, defaults={"role": role})
                r = self.client.post(f"{self.base}/comments/", {"body": f"by {role}"}, format="json")
                self.assertEqual(r.status_code, 201, r.content)
                r = self.client.delete(f"{self.base}/comments/{r.json()['id']}/")
                self.assertEqual(r.status_code, 204, r.content)
