import json
import os
import unittest
from unittest.mock import Mock, patch


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


class EmptyTable:
    def get_item(self, **kwargs):
        return {}

    def update_item(self, **kwargs):
        return {}


class ConfigTable(EmptyTable):
    def __init__(self):
        self.item = {}

    def get_item(self, **kwargs):
        return {"Item": self.item} if kwargs["Key"]["quota_id"] == proxy.CONFIG_KEY else {}

    def update_item(self, **kwargs):
        values = kwargs["ExpressionAttributeValues"]
        self.item = {
            "monthly_budget_usd": values[":budget"],
            "input_max_tokens": values[":input_limit"],
            "output_max_tokens": values[":output_limit"],
            "model_enabled": values[":models"],
            "updated_at": values[":updated_at"],
        }
        return {}


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "API_KEY": "client-secret",
                "ADMIN_PASSWORD": "admin-password",
                "ADMIN_SESSION_SECRET": "a" * 32,
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_admin_login_and_signed_session(self):
        login = proxy.lambda_handler(
            event("POST", "/admin/login", {"username": "admin", "password": "admin-password"}),
            None,
        )
        self.assertEqual(login["statusCode"], 200)
        token = json.loads(login["body"])["token"]
        with patch.object(proxy, "table", return_value=EmptyTable()):
            status = proxy.lambda_handler(event("GET", "/admin/status", token=token), None)
        self.assertEqual(status["statusCode"], 200)

    def test_invalid_client_api_key_is_rejected(self):
        result = proxy.lambda_handler(event("GET", "/v1/models", token="wrong"), None)
        self.assertEqual(result["statusCode"], 401)

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
        config_table = ConfigTable()
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
