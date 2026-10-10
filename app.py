import os
import uuid
import time
import threading
import httpx
import gradio as gr

A2A_URL = os.getenv(
    "A2A_URL",
    "https://agent-network-shared-conceirge-hn2hkw.rajrd4-1.usa-e1.cloudhub.io/lynn_itinerary_agent/",
)
REQUEST_TIMEOUT = int(os.getenv("A2A_TIMEOUT", "90"))
MAX_RETRIES = int(os.getenv("A2A_MAX_RETRIES", "3"))
RETRY_DELAY_SECS = int(os.getenv("A2A_RETRY_DELAY", "30"))

# Comma-separated health-check URLs hit on session start to wake Aiven DB connection pools.
# Override via WARMUP_URLS env var on Render.
_WARMUP_URLS_RAW = os.getenv(
    "WARMUP_URLS",
    (
        "https://lynn-resort-systems-esulje.2tku8l.usa-e1.cloudhub.io/health,"
        "https://lynn-interests-esulje.2tku8l.usa-e1.cloudhub.io/health,"
        "https://lynn-casino-gaming-esulje.2tku8l.usa-e1.cloudhub.io/health"
    ),
)
WARMUP_URLS = [u.strip() for u in _WARMUP_URLS_RAW.split(",") if u.strip()]

STATE_MAP = {
    "TASK_STATE_COMPLETED": "completed",
    "TASK_STATE_INPUT_REQUIRED": "input-required",
    "TASK_STATE_FAILED": "failed",
    "TASK_STATE_CANCELED": "canceled",
    "TASK_STATE_WORKING": "working",
}

_session_ctx: dict[str, str] = {}
_warmed_sessions: set[str] = set()


def _fire_warmup() -> None:
    """Background: ping each MCP health endpoint to wake Aiven DB connection pools."""
    for url in WARMUP_URLS:
        try:
            httpx.get(url, timeout=15)
        except Exception:
            pass


def respond(message: str, history: list, broker_url: str, request: gr.Request):
    url = (broker_url or A2A_URL).strip().rstrip("/") + "/"
    session_key = str(request.session_hash) if request else "default"
    context_id = _session_ctx.get(session_key, "")

    # Kick off DB warm-up on first message in this session (fire-and-forget).
    if session_key not in _warmed_sessions:
        _warmed_sessions.add(session_key)
        if WARMUP_URLS:
            threading.Thread(target=_fire_warmup, daemon=True).start()

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

    retry_reason = None  # "server_error" | "timeout" | "empty_artifact"

    for attempt in range(MAX_RETRIES):
        if attempt > 0:
            # Short delay when the broker responded but artifacts were missing;
            # longer delay when the broker itself was down/overloaded.
            delay = 5 if retry_reason == "empty_artifact" else RETRY_DELAY_SECS
            yield (
                f"_Incomplete response — retrying ({attempt + 1}/{MAX_RETRIES})…_"
                if retry_reason == "empty_artifact"
                else (
                    f"_Connection issue — retrying ({attempt + 1}/{MAX_RETRIES}, "
                    f"waiting {delay}s…)_"
                )
            )
            time.sleep(delay)
            # Fresh messageId so the broker doesn't deduplicate the retry.
            payload["params"]["message"]["messageId"] = uuid.uuid4().hex

        retry_reason = None

        try:
            resp = httpx.post(
                url, json=payload, timeout=REQUEST_TIMEOUT,
                headers={"A2A-Version": "1.0"},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            retry_reason = "timeout"
            if attempt < MAX_RETRIES - 1:
                continue
            yield (
                f"Request timed out (>{REQUEST_TIMEOUT}s) after {MAX_RETRIES} attempts. "
                "The agent is busy — wait ~60s and try again."
            )
            return
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                retry_reason = "server_error"
                if attempt < MAX_RETRIES - 1:
                    continue
                yield (
                    f"Server error ({e.response.status_code}) after {MAX_RETRIES} attempts. "
                    "The broker may be recovering — wait ~60–90s and try again."
                )
                return
            # 4xx — don't retry
            yield f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            return
        except Exception as e:
            yield f"Error: {e}"
            return

        if "error" in data:
            err = data["error"]
            yield f"Agent error {err.get('code')}: {err.get('message')}"
            return

        outer = data.get("result", {})
        task = outer.get("task", outer)

        new_ctx = task.get("contextId") or outer.get("contextId")
        if new_ctx:
            _session_ctx[session_key] = new_ctx

        raw_state = task.get("status", {}).get("state", "unknown")
        state = STATE_MAP.get(raw_state, raw_state)

        status_text = _extract_text(
            task.get("status", {}).get("message", {}).get("parts", [])
        )
        artifact_text = "".join(
            _extract_text(a.get("parts", [])) for a in task.get("artifacts", [])
        )

        # Retry whenever the agent returns a status message but no itinerary artifact:
        # - completed with no artifact (agent finished without emitting content)
        # - input-required with no artifact (phase-0 stall leaking internal summary)
        # - failed with no artifact (transient agent error)
        if (
            state in ("completed", "input-required", "failed")
            and not artifact_text
            and attempt < MAX_RETRIES - 1
        ):
            retry_reason = "empty_artifact"
            continue

        body = artifact_text or status_text or f"(state: {state}, no text returned)"

        if state == "input-required":
            body = f"**Agent needs more info:**\n\n{body}"
        elif state == "failed":
            body = f"**Agent failed:**\n\n{body}"
        elif state not in ("completed", "unknown"):
            body = f"*State: {state}*\n\n{body}"

        yield body
        return


def _extract_text(parts: list) -> str:
    return "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()


broker_input = gr.Textbox(
    value=A2A_URL,
    label="Broker URL",
    placeholder="https://your-agent-url/",
    scale=1,
)

demo = gr.ChatInterface(
    fn=respond,
    title="A2A Chat",
    additional_inputs=[broker_input],
    additional_inputs_accordion=gr.Accordion("⚙️ Settings", open=True),
    examples=[
        [
            "I'm Alex Carter, confirmation LYNN-ALEX01. Plan our two nights — we've got a 12-year-old with us. Mix in what we like and apply any comps I qualify for.",
            A2A_URL,
        ],
        [
            "I'm Alex Carter, confirmation LYNN-ALEX01. Plan our two nights. Actually we're really into wellness and shopping this trip — less gaming.",
            A2A_URL,
        ],
        [
            "I'm Alex Carter, confirmation LYNN-ALEX01. Two nights, and this trip is all about the casino floor — high-stakes tables, exclusive gaming lounges, and anything I'm comped for. Skip the spa.",
            A2A_URL,
        ],
        [
            "Create a personalized resort itinerary for my stay. My reservation confirmation is LYNN-ALEX01. Include dining, entertainment, and any offers I am eligible for.",
            A2A_URL,
        ],
    ],
    chatbot=gr.Chatbot(height=480),
)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port, share=False)
