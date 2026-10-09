import os
import uuid
import httpx
import gradio as gr

A2A_URL = os.getenv("A2A_URL", "https://agent-network-shared-conceirge-hn2hkw.rajrd4-1.usa-e1.cloudhub.io/lynn_itinerary_agent/")
REQUEST_TIMEOUT = int(os.getenv("A2A_TIMEOUT", "90"))

STATE_MAP = {
    "TASK_STATE_COMPLETED": "completed",
    "TASK_STATE_INPUT_REQUIRED": "input-required",
    "TASK_STATE_FAILED": "failed",
    "TASK_STATE_CANCELED": "canceled",
    "TASK_STATE_WORKING": "working",
}

# Per-session context ids (keyed by Gradio session hash)
_session_ctx: dict[str, str] = {}


def respond(message: str, history: list, request: gr.Request):
    session_key = str(request.session_hash) if request else "default"
    context_id = _session_ctx.get(session_key, "")

    payload = {
        "jsonrpc": "2.0",
        "id": f"req-{uuid.uuid4().hex[:8]}",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": uuid.uuid4().hex,
                "role": "ROLE_USER",
                "parts": [{"text": message}],
                **({"contextId": context_id} if context_id else {}),
            }
        },
    }

    try:
        resp = httpx.post(A2A_URL, json=payload, timeout=REQUEST_TIMEOUT,
                          headers={"A2A-Version": "1.0"})
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        return "Request timed out. The agent may be busy — wait ~60s and retry."
    except Exception as e:
        return f"Error: {e}"

    if "error" in data:
        err = data["error"]
        return f"Agent error {err.get('code')}: {err.get('message')}"

    outer = data.get("result", {})
    task = outer.get("task", outer)

    new_ctx = task.get("contextId") or outer.get("contextId")
    if new_ctx:
        _session_ctx[session_key] = new_ctx

    raw_state = task.get("status", {}).get("state", "unknown")
    state = STATE_MAP.get(raw_state, raw_state)

    status_text = _extract_text(task.get("status", {}).get("message", {}).get("parts", []))
    artifact_text = "".join(
        _extract_text(a.get("parts", [])) for a in task.get("artifacts", [])
    )

    body = artifact_text or status_text or f"(state: {state}, no text returned)"

    if state == "input-required":
        body = f"**Agent needs more info:**\n\n{body}"
    elif state == "failed":
        body = f"**Agent failed:**\n\n{body}"
    elif state not in ("completed", "unknown"):
        body = f"*State: {state}*\n\n{body}"

    return body


def _extract_text(parts: list) -> str:
    return "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()


demo = gr.ChatInterface(
    fn=respond,
    title="A2A Chat",
    description=f"`{A2A_URL}`",
    examples=[
        "I'm Alex Carter, confirmation LYNN-ALEX01. Plan our two nights — we've got a 12-year-old with us. Mix in what we like and apply any comps I qualify for.",
        "Create a personalized resort itinerary for my stay. My reservation confirmation is LYNN-ALEX01. Include dining, entertainment, and any offers I am eligible for.",
        "What dining options are available?",
        "Show me available gaming tables.",
    ],
    chatbot=gr.Chatbot(height=520),
)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port, share=False)
