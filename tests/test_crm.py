"""Offline CRM identity-contract tests. No Mizito account or network is needed."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mizito import app, crm  # noqa: E402
from mizito_client import MizitoAuthError, MizitoError  # noqa: E402


class CrmCustomerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.dialogs = []
        self.full = {}
        self.requests = []

        def api(endpoint, payload):
            self.requests.append((endpoint, payload))
            if endpoint == "chat.getDialogs":
                return {"dialogs": self.dialogs}
            if endpoint == "chat.getFullChat":
                return self.full[payload["dialog"]]
            if endpoint == "deal.getCustomerDeals":
                return [{"_id": "deal-1", "customer": payload["customer"]}]
            if endpoint == "payment.getCustomerPayments":
                return [{"_id": "payment-1", "customer": payload["customer"]}]
            raise AssertionError(f"Unexpected API request: {endpoint}")

        caller = patch.object(app.client, "call", side_effect=api)
        caller.start()
        self.addCleanup(caller.stop)
        # crm imports the same singleton, but patch the helper so the test does
        # not depend on the generic error-wrapping path in app.call.
        full = patch.object(crm, "chat_full", side_effect=lambda cid: self.full[cid])
        full.start()
        self.addCleanup(full.stop)
        title = patch.object(app.client, "dialog_title", side_effect=lambda row: row.get("title") or row["_id"])
        title.start()
        self.addCleanup(title.stop)

    def test_embedded_customer_object_round_trips_from_list_to_get(self):
        self.dialogs = [{"_id": "dialog-1", "is_customer_entity": True}]
        self.full = {"dialog-1": {"_id": "dialog-1", "title": "Customer A",
                                   "customer": {"_id": "customer-1", "name": "Customer A"}}}

        listed = crm.mizito_list_customers()
        detail = crm.mizito_get_customer(listed["customers"][0]["conversation_id"])

        self.assertEqual(listed["customers"][0]["customer_id"], "customer-1")
        self.assertEqual(detail["customer"]["conversation_id"], "dialog-1")
        self.assertEqual(detail["deals"][0]["customer"], "customer-1")
        self.assertEqual(detail["payments"][0]["customer"], "customer-1")

    def test_customer_id_in_is_customer_entity_is_resolved(self):
        self.dialogs = [{"_id": "dialog-2", "title": "Customer B", "is_customer_entity": "customer-2"}]
        self.full = {"dialog-2": {"_id": "dialog-2", "title": "Customer B",
                                   "is_customer_entity": "customer-2"}}

        listed = crm.mizito_list_customers()
        detail = crm.mizito_get_customer("dialog-2")

        self.assertEqual(listed["customers"], [{"conversation_id": "dialog-2", "customer_id": "customer-2",
                                                "name": "Customer B"}])
        self.assertEqual(detail["customer"]["customer_id"], "customer-2")

    def test_customer_entity_object_is_resolved(self):
        self.dialogs = [{"_id": "dialog-3", "is_customer_entity": True,
                         "customer_entity": {"_id": "customer-3", "name": "Customer C"}}]
        self.full = {"dialog-3": {"_id": "dialog-3", "title": "Customer C", "is_customer_entity": True}}

        detail = crm.mizito_get_customer("dialog-3")

        self.assertEqual(detail["customer"]["customer_id"], "customer-3")

    def test_real_api_shape_customer_entity_string_in_full_chat_is_resolved(self):
        # Observed in the live read-only probe: the dialog is marked with a
        # boolean, while the distinct customer id lives in customer_entity.
        self.dialogs = [{"_id": "dialog-live", "is_customer_entity": True}]
        self.full = {"dialog-live": {"_id": "dialog-live", "title": "Redacted",
                                     "is_customer_entity": True, "customer_entity": "customer-live"}}

        listed = crm.mizito_list_customers()
        detail = crm.mizito_get_customer(listed["customers"][0]["conversation_id"])

        self.assertEqual(listed["customers"][0]["customer_id"], "customer-live")
        self.assertEqual(detail["customer"]["conversation_id"], "dialog-live")
        self.assertEqual(detail["deals"][0]["customer"], "customer-live")
        self.assertEqual(detail["payments"][0]["customer"], "customer-live")

    def test_get_accepts_customer_id_returned_by_list(self):
        self.dialogs = [{"_id": "dialog-4", "is_customer_entity": {"_id": "customer-4"}}]
        self.full = {"dialog-4": {"_id": "dialog-4", "title": "Customer D",
                                   "is_customer_entity": {"_id": "customer-4"}}}

        detail = crm.mizito_get_customer("customer-4")

        self.assertEqual(detail["customer"]["conversation_id"], "dialog-4")
        self.assertEqual(detail["customer"]["customer_id"], "customer-4")

    def test_boolean_marker_without_customer_id_is_not_listed(self):
        self.dialogs = [{"_id": "dialog-bad", "is_customer_entity": True}]
        self.full = {"dialog-bad": {"_id": "dialog-bad", "title": "Unresolved", "is_customer_entity": True}}

        self.assertEqual(crm.mizito_list_customers(), {"count": 0, "customers": []})
        with self.assertRaisesRegex(app.ToolError, "not a customer file"):
            crm.mizito_get_customer("dialog-bad")

    def test_read_tools_never_call_write_endpoints(self):
        self.dialogs = [{"_id": "dialog-5", "is_customer_entity": "customer-5"}]
        self.full = {"dialog-5": {"_id": "dialog-5", "title": "Customer E",
                                   "is_customer_entity": "customer-5"}}

        crm.mizito_list_customers()
        crm.mizito_get_customer("dialog-5", with_history=False)

        self.assertFalse(any(endpoint.startswith(("customer.add", "customer.update", "deal.add", "deal.update",
                                                  "payment.add", "payment.update"))
                             for endpoint, _ in self.requests))

    def test_phone_normalization(self):
        for value in (
            "09123456789",
            "+989123456789",
            "00989123456789",
            "989123456789",
            "9123456789",
            "۰۹۱۲۳۴۵۶۷۸۹",
            "٠٩١٢٣٤٥٦٧٨٩",
            "۹۱۲ ۳۴۵ ۶۷۸۹",
            "0912 345-6789",
            "(0912)345.6789",
        ):
            self.assertEqual(crm._phone_key(value), "09123456789")
        self.assertIsNone(crm._phone_key("02112345678"))
        self.assertIsNone(crm._phone_key("0912345678"))
        self.assertIsNone(crm._phone_key("091234567890"))

    def test_list_exact_phone_uses_dialog_customer_without_full_chat(self):
        customer = {"_id": "customer-phone", "name": "Test",
                    "mobile": [{"phone_number": "+989123456789"}]}
        self.dialogs = [{"_id": "dialog-phone", "is_customer_entity": True,
                         "customer_entity": "customer-phone", "customer": customer}]

        result = crm.mizito_list_customers(phone="۰۹۱۲۳۴۵۶۷۸۹")

        self.assertEqual(result["lookup_status"], "CUSTOMER_MATCHED")
        self.assertTrue(result["lookup_complete"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["customers"][0]["customer_id"], "customer-phone")
        self.assertFalse(any(endpoint == "chat.getFullChat" for endpoint, _ in self.requests))

    def test_list_exact_phone_does_not_treat_name_as_contact(self):
        self.dialogs = [{"_id": "dialog-name", "is_customer_entity": True,
                         "customer": {"_id": "customer-name", "name": "09123456789", "mobile": []}}]

        result = crm.mizito_list_customers(phone="09123456789")

        self.assertEqual(result["lookup_status"], "CUSTOMER_NOT_FOUND")
        self.assertEqual(result["customers"], [])

    def test_list_exact_phone_returns_all_duplicate_candidates(self):
        self.dialogs = [
            {"_id": "dialog-a", "is_customer_entity": True,
             "customer": {"_id": "customer-a", "name": "A",
                          "mobile": [{"phone_number": "09123456789"}]}},
            {"_id": "dialog-b", "is_customer_entity": True,
             "customer": {"_id": "customer-b", "name": "B",
                          "phone": [{"phone_number": "+989123456789"}]}},
        ]

        result = crm.mizito_list_customers(phone="09123456789")

        self.assertEqual(result["lookup_status"], "MULTIPLE_CUSTOMERS")
        self.assertEqual(result["count"], 2)
        self.assertEqual({row["customer_id"] for row in result["customers"]}, {"customer-a", "customer-b"})

    def test_invalid_phone_is_rejected_before_any_api_request(self):
        result = crm.mizito_list_customers(phone="not-a-phone")

        self.assertEqual(result["lookup_status"], "INVALID_PHONE")
        self.assertEqual(self.requests, [])

    def test_partial_customer_rows_fail_closed_for_phone_lookup(self):
        self.dialogs = [
            {"_id": "dialog-complete", "is_customer_entity": True,
             "customer": {"_id": "customer-complete", "name": "Complete",
                          "mobile": [{"phone_number": "09123456789"}]}},
            {"_id": "dialog-partial", "is_customer_entity": True},
        ]

        result = crm.mizito_list_customers(phone="09123456789")

        self.assertEqual(result["lookup_status"], "LOOKUP_INCOMPLETE")
        self.assertFalse(result["lookup_complete"])
        self.assertEqual(result["incomplete_customer_count"], 1)
        self.assertEqual(result["count"], 1)

    def test_invalid_dialog_index_response_is_not_reported_as_not_found(self):
        with patch.object(app.client, "call", return_value={"unexpected": []}):
            result = crm.mizito_list_customers(phone="09123456789")
        self.assertEqual((result["lookup_status"], result["error_code"]),
                         ("CONNECTOR_ERROR", "INVALID_RESPONSE"))

    def test_connector_error_has_stable_pii_free_code(self):
        with patch.object(app.client, "call", side_effect=MizitoError("opaque upstream failure")):
            result = crm.mizito_list_customers(phone="09123456789")
        self.assertEqual((result["lookup_status"], result["error_code"]),
                         ("CONNECTOR_ERROR", "CONNECTOR_FAILED"))

    def test_timeout_has_stable_pii_free_code(self):
        with patch.object(app.client, "call", side_effect=MizitoError("network error: ReadTimeout")):
            result = crm.mizito_list_customers(phone="09123456789")
        self.assertEqual((result["lookup_status"], result["error_code"]),
                         ("CONNECTOR_TIMEOUT", "MIZITO_API_TIMEOUT"))

    def test_transport_error_has_stable_pii_free_code(self):
        with patch.object(app.client, "call", side_effect=MizitoError("network error: ConnectError")):
            result = crm.mizito_list_customers(phone="09123456789")
        self.assertEqual((result["lookup_status"], result["error_code"]),
                         ("CONNECTOR_ERROR", "TRANSPORT_FAILED"))

    def test_authorization_error_has_stable_pii_free_code(self):
        with patch.object(app.client, "call", side_effect=MizitoError("chat.getDialogs -> HTTP 403: forbidden")):
            result = crm.mizito_list_customers(phone="09123456789")
        self.assertEqual((result["lookup_status"], result["error_code"]),
                         ("CONNECTOR_ERROR", "AUTHORIZATION_FAILED"))

    def test_authentication_error_has_stable_pii_free_code(self):
        with patch.object(app.client, "call", side_effect=MizitoAuthError("expired token")):
            result = crm.mizito_list_customers(phone="09123456789")
        self.assertEqual((result["lookup_status"], result["error_code"]),
                         ("CONNECTOR_ERROR", "AUTHENTICATION_FAILED"))

    def test_phone_lookup_makes_one_bounded_request_without_retry_storm(self):
        self.dialogs = [
            {"_id": f"dialog-{index}", "is_customer_entity": True,
             "customer": {"_id": f"customer-{index}", "name": f"Customer {index}",
                          "mobile": [], "phone": []}}
            for index in range(500)
        ]

        result = crm.mizito_list_customers(phone="09123456789", limit=5)

        self.assertEqual(result["lookup_status"], "CUSTOMER_NOT_FOUND")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(self.requests, [("chat.getDialogs", {})])

    def test_id_only_customer_reference_is_incomplete_not_not_found(self):
        self.dialogs = [{"_id": "dialog-id-only", "is_customer_entity": True,
                         "customer_entity": "customer-id-only"}]

        result = crm.mizito_list_customers(phone="09123456789")

        self.assertEqual(result["lookup_status"], "LOOKUP_INCOMPLETE")
        self.assertEqual(result["count"], 0)
        self.assertFalse(any(endpoint == "chat.getFullChat" for endpoint, _ in self.requests))


if __name__ == "__main__":
    unittest.main()
