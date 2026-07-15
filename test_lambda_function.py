import json
import os
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError


os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_REGION", "ap-southeast-1")

import lambda_function as proxy


def event(method, path, body=None, token=None):
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return {
        "rawPath": path,
        "headers": headers,
        "requestContext": {"http": {"method": method, "path": path}},
        "body": json.dumps(body) if body is not None else None,
    }


class MemoryTable:
    def __init__(self):
        self.items = {}

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["quota_id"])
        return {"Item": item} if item is not None else {}

    def update_item(self, **kwargs):
        key = kwargs["Key"]["quota_id"]
        values = kwargs.get("ExpressionAttributeValues", {})
        if key == proxy.CREDENTIALS_KEY:
            self.items[key] = {
                "quota_id": key,
                "admin_username": values[":username"],
                "admin_password_hash": values[":password_hash"],
                "api_key_hash": values[":api_key_hash"],
                "api_key_hint": values[":api_key_hint"],
                "credential_version": values.get(":new_version", values.get(":version")),
                "updated_at": values[":updated_at"],
            }
        elif key == proxy.CONFIG_KEY:
            self.items[key] = {
                "quota_id": key,
                "monthly_budget_usd": values[":budget"],
                "input_max_tokens": values[":input_limit"],
                "output_max_tokens": values[":output_limit"],
                "model_enabled": values[":models"],
                "updated_at": values[":updated_at"],
            }
        return {}


class EmptyTable(MemoryTable):
    pass


class DeniedTable(MemoryTable):
    def get_item(self, **kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "GetItem",
        )


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "API_KEY": "client-secret-key-at-least-24",
                "ADMIN_PASSWORD": "admin-password",
                "ADMIN_SESSION_SECRET": "a" * 32,
                "CREDENTIAL_HASH_KEY": "h" * 32,
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_admin_login_and_signed_session(self):
        memory_table = MemoryTable()
        with patch.object(proxy, "table", return_value=memory_table):
            login = proxy.lambda_handler(
                event("POST", "/admin/login", {"username": "admin", "password": "admin-password"}),
                None,
            )
            self.assertEqual(login["statusCode"], 200)
            token = json.loads(login["body"])["token"]
            status = proxy.lambda_handler(event("GET", "/admin/status", token=token), None)
        self.assertEqual(status["statusCode"], 200)
        stored = memory_table.items[proxy.CREDENTIALS_KEY]
        self.assertNotIn("admin-password", stored["admin_password_hash"])
        self.assertNotIn("client-secret-key-at-least-24", stored["api_key_hash"])

    def test_invalid_client_api_key_is_rejected(self):
        with patch.object(proxy, "table", return_value=MemoryTable()):
            result = proxy.lambda_handler(event("GET", "/v1/models", token="wrong"), None)
        self.assertEqual(result["statusCode"], 401)

    def test_admin_login_reports_actionable_dynamodb_permission_error(self):
        with patch.object(proxy, "table", return_value=DeniedTable()), patch("builtins.print"):
            result = proxy.lambda_handler(
                event("POST", "/admin/login", {"username": "admin", "password": "admin-password"}), None
            )
        self.assertEqual(result["statusCode"], 500)
        self.assertIn("dynamodb:GetItem", json.loads(result["body"])["error"]["message"])

    def test_model_config_requires_real_boolean(self):
        with patch.object(proxy, "table", return_value=EmptyTable()), patch.object(
            proxy, "runtime_config", return_value=proxy.default_runtime_config()
        ):
            result = proxy.handle_admin_config(
                event(
                    "PUT",
                    "/admin/config",
                    {"model_enabled": {"claude-sonnet-5": "false"}},
                )
            )
        self.assertEqual(result["statusCode"], 400)

    def test_admin_can_change_monthly_budget_and_token_limits(self):
        config_table = MemoryTable()
        with patch.object(proxy, "table", return_value=config_table):
            result = proxy.handle_admin_config(
                event(
                    "PUT",
                    "/admin/config",
                    {
                        "monthly_budget_usd": 35.5,
                        "input_max_tokens": 200000,
                        "output_max_tokens": 64000,
                        "model_enabled": {"claude-sonnet-5": True, "gpt-4o": False},
                    },
                )
            )
        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["config"]["monthly_budget_usd"], 35.5)
        self.assertEqual(body["config"]["input_max_tokens"], 200000)
        self.assertFalse(body["models"][1]["enabled"])

    def test_admin_can_rotate_password_and_api_key(self):
        memory_table = MemoryTable()
        with patch.object(proxy, "table", return_value=memory_table):
            login = proxy.lambda_handler(
                event("POST", "/admin/login", {"username": "admin", "password": "admin-password"}), None
            )
            old_token = json.loads(login["body"])["token"]
            rotated = proxy.lambda_handler(
                event(
                    "PUT",
                    "/admin/credentials",
                    {
                        "current_password": "admin-password",
                        "new_admin_password": "new-admin-password",
                        "generate_api_key": True,
                    },
                    old_token,
                ),
                None,
            )
            rotated_body = json.loads(rotated["body"])
            self.assertEqual(rotated["statusCode"], 200)
            self.assertTrue(rotated_body["new_api_key"].startswith("brx_"))
            self.assertEqual(proxy.lambda_handler(event("GET", "/admin/status", token=old_token), None)["statusCode"], 401)
            self.assertEqual(
                proxy.lambda_handler(
                    event("POST", "/admin/login", {"username": "admin", "password": "admin-password"}), None
                )["statusCode"],
                401,
            )
            self.assertEqual(
                proxy.lambda_handler(
                    event("POST", "/admin/login", {"username": "admin", "password": "new-admin-password"}), None
                )["statusCode"],
                200,
            )
            self.assertEqual(
                proxy.lambda_handler(event("GET", "/v1/models", token=rotated_body["new_api_key"]), None)["statusCode"],
                200,
            )
            self.assertEqual(
                proxy.lambda_handler(event("GET", "/v1/models", token="client-secret-key-at-least-24"), None)["statusCode"],
                401,
            )

    def test_disabled_model_never_calls_bedrock(self):
        bedrock = Mock()
        config = proxy.default_runtime_config()
        config["model_enabled"]["claude-sonnet-5"] = False
        with patch.object(proxy, "runtime_config", return_value=config), patch.object(proxy, "bedrock", bedrock):
            result = proxy.handle_chat_completions(
                event(
                    "POST",
                    "/v1/chat/completions",
                    {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hello"}]},
                    "client-secret",
                )
            )
        self.assertEqual(result["statusCode"], 403)
        bedrock.converse.assert_not_called()

    def test_invalid_temperature_is_client_error(self):
        with patch.object(proxy, "runtime_config", return_value=proxy.default_runtime_config()):
            result = proxy.handle_chat_completions(
                event(
                    "POST",
                    "/v1/chat/completions",
                    {
                        "model": "claude-sonnet-5",
                        "messages": [{"role": "user", "content": "hello"}],
                        "temperature": 2,
                    },
                    "client-secret",
                )
            )
        self.assertEqual(result["statusCode"], 400)

    def test_no_refund_after_bedrock_has_returned_billable_result(self):
        result_from_bedrock = {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {"inputTokens": 2, "outputTokens": 1, "totalTokens": 3},
            "stopReason": "end_turn",
        }
        bedrock = Mock()
        bedrock.converse.return_value = result_from_bedrock
        reservation = {"enabled": True, "quota_id": "global#2099-01", "reserved_usd": 1.0}
        with patch.object(proxy, "runtime_config", return_value=proxy.default_runtime_config()), patch.object(
            proxy, "reserve_budget", return_value=reservation
        ), patch.object(proxy, "bedrock", bedrock), patch.object(
            proxy, "finalize_budget", side_effect=RuntimeError("DynamoDB unavailable")
        ), patch.object(proxy, "safe_refund_budget") as refund, patch("builtins.print"):
            result = proxy.handle_chat_completions(
                event(
                    "POST",
                    "/v1/chat/completions",
                    {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hello"}]},
                    "client-secret",
                )
            )
        self.assertEqual(result["statusCode"], 500)
        refund.assert_not_called()

    def test_reservation_is_refunded_when_bedrock_fails(self):
        bedrock = Mock()
        bedrock.converse.side_effect = RuntimeError("Bedrock unavailable")
        reservation = {"enabled": True, "quota_id": "global#2099-01", "reserved_usd": 1.0}
        with patch.object(proxy, "runtime_config", return_value=proxy.default_runtime_config()), patch.object(
            proxy, "reserve_budget", return_value=reservation
        ), patch.object(proxy, "bedrock", bedrock), patch.object(proxy, "safe_refund_budget") as refund, patch(
            "builtins.print"
        ):
            result = proxy.handle_chat_completions(
                event(
                    "POST",
                    "/v1/chat/completions",
                    {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hello"}]},
                    "client-secret",
                )
            )
        self.assertEqual(result["statusCode"], 500)
        refund.assert_called_once_with(reservation)


if __name__ == "__main__":
    unittest.main()
