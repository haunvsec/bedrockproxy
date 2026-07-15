import base64
import hashlib
import hmac
import json
import os
import time
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


# Non-secret deployment settings. Edit these constants when the infrastructure changes.
AWS_REGION = "ap-southeast-1"
BEDROCK_REGION = "ap-southeast-1"
BEDROCK_READ_TIMEOUT_SECONDS = 240
QUOTA_TABLE_NAME = "BedrockOpenAIProxyQuota"
QUOTA_SCOPE = "global"
DEFAULT_MONTHLY_BUDGET_USD = 20.0
DEFAULT_INPUT_MAX_TOKENS = 256_000
DEFAULT_OUTPUT_MAX_TOKENS = 128_000
ADMIN_USERNAME = "admin"

bedrock = boto3.client(
    "bedrock-runtime",
    region_name=BEDROCK_REGION,
    config=Config(connect_timeout=10, read_timeout=BEDROCK_READ_TIMEOUT_SECONDS),
)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)


DEFAULT_MODEL_MAP = {
    # OpenAI-compatible alias -> Bedrock model ID.
    "claude-sonnet-5": "global.anthropic.claude-sonnet-5",
    # Keep a common OpenAI alias for clients that hard-code this model name.
    "gpt-4o": "global.anthropic.claude-sonnet-5",
}

# USD per 1M tokens. Promotional Sonnet 5 pricing through 2026-08-31.
# Change to input=3.00/output=15.00 from 2026-09-01 unless AWS updates its pricing again.
DEFAULT_MODEL_PRICING = {
    "global.anthropic.claude-sonnet-5": {"input": 2.00, "output": 10.00},
}

CONFIG_KEY = "config#proxy"
ADMIN_SESSION_TTL_SECONDS = 8 * 60 * 60


def response(status_code: int, body: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
            "access-control-allow-headers": "authorization,content-type,x-api-key",
            "access-control-allow-methods": "GET,POST,PUT,OPTIONS",
            **(headers or {}),
        },
        "body": json.dumps(body, ensure_ascii=False),
    }


def html_response(html: str) -> Dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store",
            "content-security-policy": (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                "connect-src 'self' https: http://127.0.0.1:*; img-src 'self' data:; frame-ancestors 'none'"
            ),
            "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
        },
        "body": html,
    }


def dashboard_page() -> Dict[str, Any]:
    path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    with open(path, "r", encoding="utf-8") as dashboard_file:
        return html_response(dashboard_file.read())


def openai_error(status_code: int, message: str, error_type: str = "invalid_request_error") -> Dict[str, Any]:
    return response(
        status_code,
        {
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": None,
            }
        },
    )


def get_header(event: Dict[str, Any], name: str) -> Optional[str]:
    headers = event.get("headers") or {}
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw)


def route_path(event: Dict[str, Any]) -> str:
    return (
        event.get("rawPath")
        or event.get("path")
        or event.get("requestContext", {}).get("http", {}).get("path")
        or "/"
    )


def method(event: Dict[str, Any]) -> str:
    return (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()


def now_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def get_api_key(event: Dict[str, Any]) -> str:
    authorization = get_header(event, "authorization") or ""
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return get_header(event, "x-api-key") or "anonymous"


def configured_api_keys() -> List[str]:
    """Load the client API key. Keep secrets in Lambda environment variables."""
    single_key = os.environ.get("API_KEY", "").strip()
    return [single_key] if single_key else []


def authenticate(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        valid_keys = configured_api_keys()
    except (ValueError, json.JSONDecodeError) as exc:
        return openai_error(500, f"Authentication configuration error: {exc}", "server_error")

    if not valid_keys:
        return openai_error(
            500,
            "Authentication is not configured. Set API_KEY.",
            "server_error",
        )

    supplied_key = get_api_key(event)
    if supplied_key == "anonymous" or not any(
        hmac.compare_digest(supplied_key, valid_key) for valid_key in valid_keys
    ):
        return openai_error(401, "Invalid or missing API key.", "authentication_error")
    return None


def base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def base64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def admin_credentials_configured() -> bool:
    return all(os.environ.get(name, "").strip() for name in ("ADMIN_PASSWORD", "ADMIN_SESSION_SECRET"))


def admin_configuration_error() -> Optional[str]:
    if not admin_credentials_configured():
        return "Set ADMIN_PASSWORD and ADMIN_SESSION_SECRET."
    if len(os.environ["ADMIN_SESSION_SECRET"].encode("utf-8")) < 32:
        return "ADMIN_SESSION_SECRET must be at least 32 bytes."
    return None


def create_admin_session(username: str) -> Tuple[str, int]:
    expires_at = int(time.time()) + ADMIN_SESSION_TTL_SECONDS
    payload = base64url_encode(
        json.dumps({"sub": username, "exp": expires_at}, separators=(",", ":")).encode("utf-8")
    )
    secret = os.environ["ADMIN_SESSION_SECRET"].encode("utf-8")
    signature = base64url_encode(hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{signature}", expires_at


def verify_admin_session(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    configuration_error = admin_configuration_error()
    if configuration_error:
        return openai_error(500, f"Admin authentication is not configured: {configuration_error}", "server_error")

    token = get_api_key(event)
    try:
        payload, supplied_signature = token.split(".", 1)
        secret = os.environ["ADMIN_SESSION_SECRET"].encode("utf-8")
        expected_signature = base64url_encode(hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("invalid signature")
        claims = json.loads(base64url_decode(payload))
        if int(claims.get("exp", 0)) <= int(time.time()):
            return openai_error(401, "Admin session expired.", "authentication_error")
        if not hmac.compare_digest(str(claims.get("sub", "")), ADMIN_USERNAME):
            raise ValueError("invalid subject")
    except (ValueError, TypeError, json.JSONDecodeError):
        return openai_error(401, "Invalid or missing admin session.", "authentication_error")
    return None


def quota_id_for(api_key: str) -> str:
    if QUOTA_SCOPE == "global":
        return f"global#{now_month()}"
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:24]
    return f"{digest}#{now_month()}"


def decimal_str(value: float) -> str:
    return str(Decimal(str(round(value, 12))))


def table():
    return dynamodb.Table(QUOTA_TABLE_NAME)


def monthly_budget_usd() -> float:
    return float(runtime_config()["monthly_budget_usd"])


def default_runtime_config() -> Dict[str, Any]:
    return {
        "monthly_budget_usd": DEFAULT_MONTHLY_BUDGET_USD,
        "input_max_tokens": DEFAULT_INPUT_MAX_TOKENS,
        "output_max_tokens": DEFAULT_OUTPUT_MAX_TOKENS,
        "model_enabled": {alias: True for alias in DEFAULT_MODEL_MAP},
    }


def runtime_config() -> Dict[str, Any]:
    config = default_runtime_config()
    quota_table = table()
    item = quota_table.get_item(Key={"quota_id": CONFIG_KEY}, ConsistentRead=True).get("Item", {})
    if "monthly_budget_usd" in item:
        config["monthly_budget_usd"] = float(item["monthly_budget_usd"])
    if "input_max_tokens" in item:
        config["input_max_tokens"] = int(item["input_max_tokens"])
    if "output_max_tokens" in item:
        config["output_max_tokens"] = int(item["output_max_tokens"])
    if isinstance(item.get("model_enabled"), dict):
        config["model_enabled"].update({str(key): bool(value) for key, value in item["model_enabled"].items()})
    return config


def model_pricing(model_id: str) -> Tuple[float, float]:
    """
    Returns (input_usd_per_1m_tokens, output_usd_per_1m_tokens).
    Keep DEFAULT_MODEL_PRICING in sync with the current AWS Bedrock pricing page.
    """
    item = DEFAULT_MODEL_PRICING.get(model_id)
    if not item:
        raise ValueError(f"Missing pricing for model '{model_id}'. Update DEFAULT_MODEL_PRICING in code.")
    return float(item["input"]), float(item["output"])


def cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    input_per_1m, output_per_1m = model_pricing(model_id)
    return (input_tokens / 1_000_000 * input_per_1m) + (output_tokens / 1_000_000 * output_per_1m)


def estimate_tokens_from_messages(messages: List[Dict[str, Any]]) -> int:
    # Conservative approximation used only for pre-call reservation.
    chars = len(json.dumps(messages, ensure_ascii=False))
    return max(1, int(chars / 3))


def reserve_budget(
    api_key: str,
    model_id: str,
    estimated_input_tokens: int,
    max_output_tokens: int,
    budget: Optional[float] = None,
) -> Dict[str, Any]:
    budget = monthly_budget_usd() if budget is None else float(budget)
    quota_table = table()
    if budget <= 0:
        return {"enabled": False, "reserved_usd": 0.0}

    reserved = cost_usd(model_id, estimated_input_tokens, max_output_tokens)
    if reserved > budget:
        raise PermissionError(
            f"Request reservation (${reserved:.6f}) is larger than the monthly budget (${budget:.4f}). "
            "Reduce max_tokens or increase the monthly budget in the admin dashboard."
        )

    quota_id = quota_id_for(api_key)
    remaining_after_reserve = Decimal(str(budget - reserved))

    try:
        quota_table.update_item(
            Key={"quota_id": quota_id},
            UpdateExpression=(
                "SET api_key_hash = :api_key_hash, #month = :month, updated_at = :updated_at, monthly_budget_usd = :budget "
                "ADD spent_usd :reserved, request_count :one"
            ),
            ConditionExpression="attribute_not_exists(spent_usd) OR spent_usd <= :remaining_after_reserve",
            ExpressionAttributeNames={"#month": "month"},
            ExpressionAttributeValues={
                ":reserved": Decimal(decimal_str(reserved)),
                ":one": Decimal(1),
                ":api_key_hash": hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
                ":month": now_month(),
                ":updated_at": int(time.time()),
                ":budget": Decimal(decimal_str(budget)),
                ":remaining_after_reserve": remaining_after_reserve,
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise PermissionError(f"Monthly budget exceeded. Budget: ${budget:.4f}/month.")
        raise

    return {"enabled": True, "quota_id": quota_id, "reserved_usd": reserved}


def finalize_budget(reservation: Dict[str, Any], actual_usd: float, input_tokens: int, output_tokens: int) -> None:
    if not reservation.get("enabled"):
        return

    delta = actual_usd - float(reservation["reserved_usd"])
    table().update_item(
        Key={"quota_id": reservation["quota_id"]},
        UpdateExpression=(
            "SET updated_at = :updated_at "
            "ADD spent_usd :delta, input_tokens :input_tokens, output_tokens :output_tokens"
        ),
        ExpressionAttributeValues={
            ":delta": Decimal(decimal_str(delta)),
            ":input_tokens": Decimal(input_tokens),
            ":output_tokens": Decimal(output_tokens),
            ":updated_at": int(time.time()),
        },
    )


def refund_budget(reservation: Dict[str, Any]) -> None:
    if not reservation.get("enabled"):
        return
    table().update_item(
        Key={"quota_id": reservation["quota_id"]},
        UpdateExpression="SET updated_at = :updated_at ADD spent_usd :refund, request_count :minus_one",
        ExpressionAttributeValues={
            ":refund": Decimal(decimal_str(-float(reservation["reserved_usd"]))),
            ":minus_one": Decimal(-1),
            ":updated_at": int(time.time()),
        },
    )


def safe_refund_budget(reservation: Dict[str, Any]) -> None:
    try:
        refund_budget(reservation)
    except Exception:
        # Keep the original request error visible. A failed refund is safer than under-counting spend.
        print("Failed to refund quota reservation:\n" + traceback.format_exc())


def text_from_openai_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif "text" in item:
                    parts.append(str(item["text"]))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(content)


def convert_messages(openai_messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    system: List[Dict[str, Any]] = []
    messages: List[Dict[str, Any]] = []

    for msg in openai_messages:
        if not isinstance(msg, dict):
            raise ValueError("Each message must be an object.")
        role = msg.get("role", "user")
        text = text_from_openai_content(msg.get("content"))

        if role == "system":
            if text:
                system.append({"text": text})
            continue

        bedrock_role = "assistant" if role == "assistant" else "user"
        if not text:
            text = " "
        if messages and messages[-1]["role"] == bedrock_role:
            messages[-1]["content"].append({"text": "\n" + text})
        else:
            messages.append({"role": bedrock_role, "content": [{"text": text}]})

    if not messages:
        messages.append({"role": "user", "content": [{"text": " "}]})
    elif messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": [{"text": "Continue."}]})

    return messages, system


def output_text_from_bedrock(result: Dict[str, Any]) -> str:
    content = result.get("output", {}).get("message", {}).get("content", [])
    parts = []
    for block in content:
        if "text" in block:
            parts.append(block["text"])
    return "".join(parts)


def finish_reason_from_bedrock(stop_reason: Optional[str]) -> str:
    mapping = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "content_filtered": "content_filter",
        "tool_use": "tool_calls",
    }
    return mapping.get(stop_reason or "", "stop")


def handle_models() -> Dict[str, Any]:
    model_map = DEFAULT_MODEL_MAP
    config = runtime_config()
    return response(
        200,
        {
            "object": "list",
            "data": [
                {
                    "id": alias,
                    "object": "model",
                    "created": 0,
                    "owned_by": "bedrock",
                }
                for alias in sorted(model_map.keys())
                if config["model_enabled"].get(alias, True)
            ],
        },
    )


def quota_snapshot(api_key: str) -> Dict[str, Any]:
    quota_table = table()
    config = runtime_config()
    budget = float(config["monthly_budget_usd"])
    item: Dict[str, Any] = {}

    if budget > 0:
        item = quota_table.get_item(
            Key={"quota_id": quota_id_for(api_key)},
            ConsistentRead=True,
        ).get("Item", {})
    spent = max(0.0, float(item.get("spent_usd", 0)))
    remaining = max(0.0, budget - spent)
    return {
        "enabled": budget > 0,
        "month": now_month(),
        "scope": QUOTA_SCOPE,
        "monthly_budget_usd": budget,
        "spent_usd": round(spent, 8),
        "remaining_usd": round(remaining, 8),
        "used_percent": round((spent / budget * 100) if budget else 0, 2),
        "request_count": int(item.get("request_count", 0)),
        "input_tokens": int(item.get("input_tokens", 0)),
        "output_tokens": int(item.get("output_tokens", 0)),
        "updated_at": int(item.get("updated_at", 0)) or None,
        "input_max_tokens_per_request": int(config["input_max_tokens"]),
        "output_max_tokens_per_request": int(config["output_max_tokens"]),
    }


def handle_quota(event: Dict[str, Any]) -> Dict[str, Any]:
    return response(200, quota_snapshot(get_api_key(event)))


def handle_admin_login(event: Dict[str, Any]) -> Dict[str, Any]:
    configuration_error = admin_configuration_error()
    if configuration_error:
        return openai_error(500, f"Admin authentication is not configured: {configuration_error}", "server_error")
    body = parse_body(event)
    username = str(body.get("username", ""))
    password = str(body.get("password", ""))
    if not (
        hmac.compare_digest(username, ADMIN_USERNAME)
        and hmac.compare_digest(password, os.environ["ADMIN_PASSWORD"])
    ):
        return openai_error(401, "Invalid username or password.", "authentication_error")
    token, expires_at = create_admin_session(username)
    return response(200, {"token": token, "expires_at": expires_at, "username": username})


def handle_admin_status() -> Dict[str, Any]:
    model_map = DEFAULT_MODEL_MAP
    config = runtime_config()
    # Admin dashboard shows the global monthly record.
    quota = quota_snapshot("admin-dashboard")
    return response(
        200,
        {
            "quota": quota,
            "config": {
                "monthly_budget_usd": config["monthly_budget_usd"],
                "input_max_tokens": config["input_max_tokens"],
                "output_max_tokens": config["output_max_tokens"],
            },
            "models": [
                {
                    "alias": alias,
                    "bedrock_model_id": model_id,
                    "enabled": config["model_enabled"].get(alias, True),
                }
                for alias, model_id in sorted(model_map.items())
            ],
        },
    )


def handle_admin_config(event: Dict[str, Any]) -> Dict[str, Any]:
    quota_table = table()
    body = parse_body(event)
    current = runtime_config()
    model_map = DEFAULT_MODEL_MAP

    try:
        monthly_budget = float(body.get("monthly_budget_usd", current["monthly_budget_usd"]))
        input_limit = int(body.get("input_max_tokens", current["input_max_tokens"]))
        output_limit = int(body.get("output_max_tokens", current["output_max_tokens"]))
    except (TypeError, ValueError):
        return openai_error(400, "Budget must be a number and token limits must be integers.")
    if not 0.01 <= monthly_budget <= 1_000_000:
        return openai_error(400, "monthly_budget_usd must be between 0.01 and 1,000,000.")
    if not 1 <= input_limit <= 1_000_000:
        return openai_error(400, "input_max_tokens must be between 1 and 1,000,000.")
    if not 1 <= output_limit <= 128_000:
        return openai_error(400, "output_max_tokens must be between 1 and 128,000.")

    model_enabled = dict(current["model_enabled"])
    requested_models = body.get("model_enabled", {})
    if not isinstance(requested_models, dict):
        return openai_error(400, "model_enabled must be an object.")
    for alias, enabled in requested_models.items():
        if alias not in model_map:
            return openai_error(400, f"Unknown model alias: {alias}")
        if not isinstance(enabled, bool):
            return openai_error(400, f"Model state for '{alias}' must be true or false.")
        model_enabled[alias] = enabled

    quota_table.update_item(
        Key={"quota_id": CONFIG_KEY},
        UpdateExpression=(
            "SET monthly_budget_usd = :budget, input_max_tokens = :input_limit, output_max_tokens = :output_limit, "
            "model_enabled = :models, updated_at = :updated_at"
        ),
        ExpressionAttributeValues={
            ":budget": Decimal(str(monthly_budget)),
            ":input_limit": Decimal(input_limit),
            ":output_limit": Decimal(output_limit),
            ":models": model_enabled,
            ":updated_at": int(time.time()),
        },
    )
    return handle_admin_status()


def handle_chat_completions(event: Dict[str, Any]) -> Dict[str, Any]:
    body = parse_body(event)
    if body.get("stream"):
        return openai_error(400, "stream=true is not implemented in this Lambda proxy yet.")

    model_map = DEFAULT_MODEL_MAP
    config = runtime_config()
    requested_model = body.get("model")
    if not requested_model:
        return openai_error(400, "Missing required field: model")

    if requested_model not in model_map:
        return openai_error(404, f"Model '{requested_model}' is not configured.", "invalid_request_error")
    if not config["model_enabled"].get(requested_model, True):
        return openai_error(403, f"Model '{requested_model}' is currently disabled.", "model_disabled")
    model_id = model_map[requested_model]
    openai_messages = body.get("messages")
    if not isinstance(openai_messages, list):
        return openai_error(400, "Missing or invalid field: messages")

    max_output_tokens = int(config["output_max_tokens"])
    try:
        requested_max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or 1024)
    except (TypeError, ValueError):
        return openai_error(400, "max_tokens must be an integer.")
    if requested_max_tokens < 1:
        return openai_error(400, "max_tokens must be greater than zero.")
    max_tokens = min(requested_max_tokens, max_output_tokens)
    inference_config: Dict[str, Any] = {"maxTokens": max_tokens}
    if "temperature" in body:
        try:
            temperature = float(body["temperature"])
        except (TypeError, ValueError):
            return openai_error(400, "temperature must be a number.")
        if not 0 <= temperature <= 1:
            return openai_error(400, "temperature must be between 0 and 1 for Bedrock Converse.")
        inference_config["temperature"] = temperature
    if "top_p" in body:
        try:
            top_p = float(body["top_p"])
        except (TypeError, ValueError):
            return openai_error(400, "top_p must be a number.")
        if not 0 < top_p <= 1:
            return openai_error(400, "top_p must be greater than 0 and at most 1.")
        inference_config["topP"] = top_p
    if "stop" in body:
        stop = body["stop"]
        stop_sequences = stop if isinstance(stop, list) else [stop]
        if not stop_sequences or not all(isinstance(sequence, str) and sequence for sequence in stop_sequences):
            return openai_error(400, "stop must be a non-empty string or a list of non-empty strings.")
        inference_config["stopSequences"] = stop_sequences

    try:
        messages, system = convert_messages(openai_messages)
    except ValueError as exc:
        return openai_error(400, str(exc))
    estimated_input_tokens = estimate_tokens_from_messages(openai_messages)
    max_input_tokens = int(config["input_max_tokens"])
    if estimated_input_tokens > max_input_tokens:
        return openai_error(
            400,
            f"Estimated input exceeds INPUT_MAX_TOKENS ({max_input_tokens}).",
            "invalid_request_error",
        )
    api_key = get_api_key(event)
    reservation: Dict[str, Any] = {"enabled": False, "reserved_usd": 0.0}
    bedrock_completed = False

    try:
        reservation = reserve_budget(
            api_key,
            model_id,
            estimated_input_tokens,
            max_tokens,
            float(config["monthly_budget_usd"]),
        )
        converse_args: Dict[str, Any] = {
            "modelId": model_id,
            "messages": messages,
            "inferenceConfig": inference_config,
        }
        if system:
            converse_args["system"] = system

        result = bedrock.converse(**converse_args)
        bedrock_completed = True
        usage = result.get("usage", {})
        input_tokens = int(usage.get("inputTokens", 0))
        output_tokens = int(usage.get("outputTokens", 0))
        total_tokens = int(usage.get("totalTokens", input_tokens + output_tokens))
        actual_usd = cost_usd(model_id, input_tokens, output_tokens) if reservation.get("enabled") else 0.0
        finalize_budget(reservation, actual_usd, input_tokens, output_tokens)

        created = int(time.time())
        choice = {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": output_text_from_bedrock(result),
            },
            "finish_reason": finish_reason_from_bedrock(result.get("stopReason")),
        }
        return response(
            200,
            {
                "id": f"chatcmpl-bedrock-{created}",
                "object": "chat.completion",
                "created": created,
                "model": requested_model,
                "choices": [choice],
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": total_tokens,
                },
                "bedrock": {
                    "model_id": model_id,
                    "estimated_cost_usd": round(actual_usd, 8) if reservation.get("enabled") else None,
                },
            },
        )
    except PermissionError as exc:
        return openai_error(429, str(exc), "insufficient_quota")
    except Exception:
        # Refund only when Bedrock did not return a successful, billable result.
        # If final accounting fails after inference, retain the conservative reservation.
        if not bedrock_completed:
            safe_refund_budget(reservation)
        print(traceback.format_exc())
        return openai_error(500, "Bedrock proxy request failed. Check Lambda logs for details.", "server_error")


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    if method(event) == "OPTIONS":
        return response(200, {})

    path = route_path(event).rstrip("/")
    try:
        if method(event) == "GET" and path in ("", "/", "/dashboard"):
            return dashboard_page()
        if method(event) == "POST" and path == "/admin/login":
            return handle_admin_login(event)
        if path.startswith("/admin/"):
            admin_auth_error = verify_admin_session(event)
            if admin_auth_error:
                return admin_auth_error
            if method(event) == "GET" and path == "/admin/status":
                return handle_admin_status()
            if method(event) == "PUT" and path == "/admin/config":
                return handle_admin_config(event)
            return openai_error(404, f"Admin route not found: {method(event)} {path}")

        auth_error = authenticate(event)
        if auth_error:
            return auth_error
        if method(event) == "GET" and path == "/v1/models":
            return handle_models()
        if method(event) == "GET" and path == "/v1/quota":
            return handle_quota(event)
        if method(event) == "POST" and path == "/v1/chat/completions":
            return handle_chat_completions(event)
        return openai_error(404, f"Route not found: {method(event)} {path}")
    except json.JSONDecodeError:
        return openai_error(400, "Invalid JSON body")
    except Exception:
        print(traceback.format_exc())
        return openai_error(500, "Proxy request failed. Check Lambda logs for details.", "server_error")
