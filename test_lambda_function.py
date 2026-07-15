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

    def scan(self, **kwargs):
        return {
            "Items": [
                item
                for quota_id, item in self.items.items()
                if quota_id.startswith("global#")
            ]
        }

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
        else:
            item = self.items.setdefault(key, {"quota_id": key})
            if ":true" in values:
                item.update(
                    {
                        "budget_exhausted": True,
                        "budget_exhausted_at": values[":now"],
                        "monthly_budget_usd": values[":budget"],
                        "updated_at": values[":now"],
                    }
                )
            if "REMOVE budget_exhausted" in kwargs.get("UpdateExpression", ""):
                item.pop("budget_exhausted", None)
                item.pop("budget_exhausted_at", None)
                item["monthly_budget_usd"] = values[":budget"]
        return {}


class EmptyTable(MemoryTable):
    pass


class DeniedTable(MemoryTable):
    def get_item(self, **kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "GetItem",
        )


class ScanDeniedTable(MemoryTable):
    def scan(self, **kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "Scan",
        )


class ReservationRejectedTable(MemoryTable):
    def update_item(self, **kwargs):
        if "ConditionExpression" in kwargs and kwargs["Key"]["quota_id"].startswith("global#"):
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "budget"}},
                "UpdateItem",
            )
        return super().update_item(**kwargs)


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

    def test_models_route_supports_legacy_and_v1_paths(self):
        with patch.object(proxy, "authenticate", return_value=None), patch.object(
            proxy, "handle_models", return_value=proxy.response(200, {"object": "list", "data": []})
        ) as models_handler:
            legacy = proxy.lambda_handler(event("GET", "/models", token="client-secret"), None)
            openai = proxy.lambda_handler(event("GET", "/v1/models", token="client-secret"), None)
        self.assertEqual(legacy["statusCode"], 200)
        self.assertEqual(openai["statusCode"], 200)
        self.assertEqual(models_handler.call_count, 2)

    def test_chat_route_supports_legacy_and_v1_paths(self):
        with patch.object(proxy, "authenticate", return_value=None), patch.object(
            proxy, "handle_chat_completions", return_value=proxy.response(200, {"object": "chat.completion"})
        ) as chat_handler:
            legacy = proxy.lambda_handler(event("POST", "/chat/completions", {}), None)
            openai = proxy.lambda_handler(event("POST", "/v1/chat/completions", {}), None)
        self.assertEqual(legacy["statusCode"], 200)
        self.assertEqual(openai["statusCode"], 200)
        self.assertEqual(chat_handler.call_count, 2)

    def test_admin_login_reports_actionable_dynamodb_permission_error(self):
        with patch.object(proxy, "table", return_value=DeniedTable()), patch("builtins.print"):
            result = proxy.lambda_handler(
                event("POST", "/admin/login", {"username": "admin", "password": "admin-password"}), None
            )
        self.assertEqual(result["statusCode"], 500)
        self.assertIn("dynamodb:GetItem", json.loads(result["body"])["error"]["message"])

    def test_admin_status_reports_missing_scan_permission(self):
        memory_table = ScanDeniedTable()
        with patch.object(proxy, "table", return_value=memory_table), patch("builtins.print"):
            login = proxy.lambda_handler(
                event("POST", "/admin/login", {"username": "admin", "password": "admin-password"}), None
            )
            token = json.loads(login["body"])["token"]
            status = proxy.lambda_handler(event("GET", "/admin/status", token=token), None)
        self.assertEqual(status["statusCode"], 500)
        self.assertIn("dynamodb:Scan", json.loads(status["body"])["error"]["message"])

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
        models_by_alias = {model["alias"]: model for model in body["models"]}
        self.assertFalse(models_by_alias["gpt-4o"]["enabled"])
        self.assertEqual(models_by_alias["amazon-nova-lite"]["input_price_per_million"], 0.30)
        self.assertEqual(models_by_alias["claude-haiku"]["output_price_per_million"], 5.00)

    def test_low_cost_model_pricing(self):
        self.assertAlmostEqual(proxy.cost_usd("global.amazon.nova-2-lite-v1:0", 1_000_000, 1_000_000), 2.80)
        self.assertAlmostEqual(
            proxy.cost_usd("global.anthropic.claude-haiku-4-5-20251001-v1:0", 1_000_000, 1_000_000),
            6.00,
        )

    def test_quota_history_returns_current_and_previous_months_newest_first(self):
        history_table = MemoryTable()
        history_table.items["global#2026-05"] = {
            "quota_id": "global#2026-05",
            "month": "2026-05",
            "monthly_budget_usd": 20,
            "spent_usd": 3.5,
            "request_count": 12,
            "input_tokens": 1000,
            "output_tokens": 200,
        }
        history_table.items["global#2026-06"] = {
            "quota_id": "global#2026-06",
            "month": "2026-06",
            "monthly_budget_usd": 20,
            "spent_usd": 20,
            "request_count": 30,
            "input_tokens": 4000,
            "output_tokens": 800,
        }
        with patch.object(proxy, "table", return_value=history_table), patch.object(
            proxy, "now_month", return_value="2026-07"
        ):
            current = proxy.quota_snapshot("client-secret")
            history = proxy.quota_history(current)
        self.assertEqual([item["month"] for item in history], ["2026-07", "2026-06", "2026-05"])
        self.assertEqual(history[2]["request_count"], 12)
        self.assertEqual(history[2]["input_tokens"] + history[2]["output_tokens"], 1200)
        self.assertTrue(history[1]["budget_exhausted"])

    def test_budget_rejection_automatically_disables_all_models(self):
        budget_table = ReservationRejectedTable()
        with patch.object(proxy, "table", return_value=budget_table):
            with self.assertRaises(PermissionError):
                proxy.reserve_budget(
                    "client-secret-key-at-least-24",
                    "global.amazon.nova-2-lite-v1:0",
                    100,
                    1000,
                    20,
                )
            status = json.loads(proxy.handle_admin_status()["body"])
        self.assertTrue(status["quota"]["budget_exhausted"])
        self.assertTrue(all(not model["enabled"] for model in status["models"]))
        self.assertTrue(all(model["auto_disabled"] for model in status["models"]))

    def test_increasing_budget_clears_monthly_circuit_breaker(self):
        budget_table = MemoryTable()
        quota_id = proxy.quota_id_for("admin-dashboard")
        budget_table.items[quota_id] = {
            "quota_id": quota_id,
            "spent_usd": 10,
            "budget_exhausted": True,
            "budget_exhausted_at": 1,
        }
        with patch.object(proxy, "table", return_value=budget_table):
            proxy.sync_budget_circuit(20)
        self.assertNotIn("budget_exhausted", budget_table.items[quota_id])

    def test_nova_output_is_clamped_to_model_limit(self):
        bedrock = Mock()
        bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {"inputTokens": 2, "outputTokens": 1, "totalTokens": 3},
            "stopReason": "end_turn",
        }
        with patch.object(proxy, "runtime_config", return_value=proxy.default_runtime_config()), patch.object(
            proxy, "reserve_budget", return_value={"enabled": False, "reserved_usd": 0.0}
        ), patch.object(proxy, "bedrock", bedrock):
            result = proxy.handle_chat_completions(
                event(
                    "POST",
                    "/v1/chat/completions",
                    {
                        "model": "amazon-nova-lite",
                        "messages": [{"role": "user", "content": "hello"}],
                        "max_tokens": 100000,
                    },
                    "client-secret",
                )
            )
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(bedrock.converse.call_args.kwargs["inferenceConfig"]["maxTokens"], 64000)

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
