"""Bad query parameters on the movements action answer a coded 400, not a 500 (#1833)."""
import datetime

from django.utils import timezone

from boards.models import CardMovement
from boards.tests.test_analytics_history import AnalyticsHistorySetup


class TestMovementsBadQueryParams(AnalyticsHistorySetup):

    def _url(self):
        return f"/api/v1/boards/{self.board.pk}/movements/"

    def _make_movement(self, days_ago=0):
        mv = CardMovement.objects.create(
            card=self.card,
            from_column=self.col_todo, from_column_name="To Do",
            to_column=self.col_done, to_column_name="Done",
            from_swimlane=self.swimlane, from_swimlane_name="Customer A",
            to_swimlane=self.swimlane, to_swimlane_name="Customer A",
            moved_by=self.admin,
            movement_type=CardMovement.MovementType.MOVE,
        )
        if days_ago:
            CardMovement.objects.filter(pk=mv.pk).update(
                moved_at=timezone.now() - datetime.timedelta(days=days_ago)
            )
        return mv

    def _assert_coded_400(self, param, value):
        resp = self._client_for(self.admin).get(self._url(), {param: value})
        self.assertEqual(resp.status_code, 400, f"{param}={value!r}")
        self.assertEqual(resp.data["code"], "invalid_query_param")
        self.assertIn(param, resp.data["detail"])

    def test_garbage_moved_after(self):
        for value in ("garbage", "2026-13-45", "2026-02-30"):
            self._assert_coded_400("moved_after", value)

    def test_garbage_moved_before(self):
        for value in ("garbage", "2026-13-45", "2026-02-30"):
            self._assert_coded_400("moved_before", value)

    def test_garbage_swimlane_id(self):
        for value in ("abc", "1.5", "-1", "0", str(2**64)):
            self._assert_coded_400("swimlane_id", value)

    def test_garbage_to_column_id(self):
        for value in ("abc", "1.5", "-1", "0", str(2**64)):
            self._assert_coded_400("to_column_id", value)

    def test_garbage_moved_by_id(self):
        for value in ("abc", "1.5", "-1", "0", str(2**64)):
            self._assert_coded_400("moved_by_id", value)

    def test_valid_moved_after_still_filters(self):
        self._make_movement(days_ago=0)
        self._make_movement(days_ago=30)
        cutoff = (timezone.now() - datetime.timedelta(days=10)).date().isoformat()
        resp = self._client_for(self.admin).get(self._url(), {"moved_after": cutoff})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["count"], 1)

    def test_valid_moved_before_still_filters(self):
        self._make_movement(days_ago=0)
        self._make_movement(days_ago=30)
        cutoff = (timezone.now() - datetime.timedelta(days=10)).date().isoformat()
        resp = self._client_for(self.admin).get(self._url(), {"moved_before": cutoff})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["count"], 1)

    def test_valid_ids_still_filter(self):
        self._make_movement()
        c = self._client_for(self.admin)
        for param, good, other in (
            ("swimlane_id", self.swimlane.pk, self.swimlane.pk + 1000),
            ("to_column_id", self.col_done.pk, self.col_todo.pk),
            ("moved_by_id", self.admin.pk, self.viewer.pk),
        ):
            self.assertEqual(c.get(self._url(), {param: good}).data["count"], 1, param)
            self.assertEqual(c.get(self._url(), {param: other}).data["count"], 0, param)
