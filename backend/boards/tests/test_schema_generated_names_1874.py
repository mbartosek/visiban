"""Generated OpenAPI names that a client generator would freeze (#1874).

``inline_serializer(name="XRequest")`` combined with ``COMPONENT_SPLIT_REQUEST``
yields ``XRequestRequest``, and a list endpoint with no explicit operationId is
named ``*_retrieve``. Neither is gated by api-compat, but generated clients turn
both into public type and method names, so they are pinned here before the
baseline roll.
"""

from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator


class GeneratedSchemaNamesTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def _operation_ids(self):
        return {
            op["operationId"]
            for item in self.schema["paths"].values()
            for op in item.values()
            if isinstance(op, dict) and "operationId" in op
        }

    def test_no_double_request_component_names(self):
        bad = [n for n in self.schema["components"]["schemas"] if n.endswith("RequestRequest")]
        self.assertEqual(bad, [])

    def test_renamed_request_components_exist(self):
        components = self.schema["components"]["schemas"]
        for name in (
            "ChecklistReorderRequest",
            "CardMoveRequest",
            "ColumnReorderRequest",
            "CustomFieldReorderRequest",
            "SwimlaneReorderRequest",
            "SwimlaneCustomFieldReorderRequest",
            "ShareBoardRequest",
            "AddOrUpdateMemberRequest",
            "PersonalAccessTokenCreateRequest",
        ):
            self.assertIn(name, components)

    def test_list_endpoints_use_list_operation_ids(self):
        ids = self._operation_ids()
        for new, old in (
            ("api_v1_admin_action_log_list", "api_v1_admin_action_log_retrieve"),
            ("api_v1_admin_board_invite_links_list", "api_v1_admin_board_invite_links_retrieve"),
            ("api_v1_boards_events_list", "api_v1_boards_events_retrieve"),
        ):
            self.assertIn(new, ids)
            self.assertNotIn(old, ids)
