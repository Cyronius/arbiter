"""Responses for explicit, single-call confirmations. Never grants persistent trust."""


def confirmation_response(approval, allow):
    method = approval["method"]
    params = approval.get("params", {})
    if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
        return {"decision": "accept" if allow else "decline"}
    if method == "claude/can_use_tool":
        return {"behavior": "allow", "updatedInput": params.get("input", {})} if allow else {
            "behavior": "deny", "message": "Denied from Arbiter"}
    if method == "mcpServer/elicitation/request" and params.get("mode") in {"form", "openai/form"}:
        schema = params.get("requestedSchema", {})
        if not allow:
            return {"action": "decline", "content": None}
        if schema.get("type") == "object" and schema.get("properties") == {} and not schema.get("required"):
            return {"action": "accept", "content": {}}
        raise ValueError("This form needs input; use `arbiter answer` with the requested content")
    raise ValueError("Unsupported confirmation; use `arbiter answer` with the provider response JSON")
