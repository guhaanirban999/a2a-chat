import os
import uuid
import time
import threading
import logging
import httpx
import gradio as gr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("a2a-chat")

A2A_URL = os.getenv(
    "A2A_URL",
    "https://agent-network-shared-conceirge-hn2hkw.rajrd4-1.usa-e1.cloudhub.io/lynn_itinerary_agent/",
)
REQUEST_TIMEOUT = int(os.getenv("A2A_TIMEOUT", "90"))
MAX_RETRIES = int(os.getenv("A2A_MAX_RETRIES", "3"))
RETRY_DELAY_SECS = int(os.getenv("A2A_RETRY_DELAY", "30"))

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

_DOT_FRAMES = ["", ".", "..", "..."]

_session_ctx: dict[str, str] = {}
_warmed_sessions: set[str] = set()


def _fire_warmup() -> None:
    for url in WARMUP_URLS:
        t0 = time.time()
        try:
            httpx.get(url, timeout=15)
            log.info("WARMUP ok  url=%s  elapsed=%.2fs", url, time.time() - t0)
        except Exception as e:
            log.warning("WARMUP fail  url=%s  elapsed=%.2fs  err=%s", url, time.time() - t0, e)


def _animate(label: str, thread: threading.Thread):
    """Yield animated dot frames (updating every 0.4s) while thread is alive."""
    i = 0
    while thread.is_alive():
        yield f"_{label}{_DOT_FRAMES[i % len(_DOT_FRAMES)]}_"
        i += 1
        time.sleep(0.4)


def respond(message: str, history: list, broker_url: str, request: gr.Request):
    url = (broker_url or A2A_URL).strip().rstrip("/") + "/"
    session_key = str(request.session_hash) if request else "default"
    context_id = _session_ctx.get(session_key, "")
    req_id = uuid.uuid4().hex[:8]
    t_total = time.time()

    # Shorten prompt for logs (first 80 chars)
    prompt_preview = message[:80].replace("\n", " ")
    log.info("REQUEST  id=%s  session=%s  prompt=%r", req_id, session_key[:8], prompt_preview)

    if session_key not in _warmed_sessions:
        _warmed_sessions.add(session_key)
        if WARMUP_URLS:
            log.info("WARMUP start  session=%s", session_key[:8])
            threading.Thread(target=_fire_warmup, daemon=True).start()

    payload = {
        "jsonrpc": "2.0",
        "id": f"req-{req_id}",
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

    retry_reason = None  # "server_error" | "timeout" | "empty_artifact" | "agent_failed"

    for attempt in range(MAX_RETRIES):
        # --- inter-attempt delay (animated) ---
        if attempt > 0:
            if retry_reason == "empty_artifact":
                delay = 25
            elif retry_reason == "agent_failed":
                delay = 35
            else:
                delay = RETRY_DELAY_SECS
            label = (
                "Our concierge is putting the finishing touches on your itinerary"
                if retry_reason in ("empty_artifact", "agent_failed")
                else "This is taking a little longer than usual — please hold"
            )
            log.info(
                "RETRY_DELAY  id=%s  attempt=%d/%d  reason=%s  delay=%ds",
                req_id, attempt + 1, MAX_RETRIES, retry_reason, delay,
            )
            deadline = time.time() + delay
            i = 0
            while time.time() < deadline:
                yield f"_{label}{_DOT_FRAMES[i % len(_DOT_FRAMES)]}_"
                i += 1
                time.sleep(0.4)
            payload["params"]["message"]["messageId"] = uuid.uuid4().hex

        retry_reason = None

        # --- fire HTTP request in a thread so we can animate while waiting ---
        result: list = [None]
        exc: list = [None]
        t_http = time.time()

        def _fetch(result=result, exc=exc):
            try:
                result[0] = httpx.post(
                    url, json=payload, timeout=REQUEST_TIMEOUT,
                    headers={"A2A-Version": "1.0"},
                )
            except Exception as e:
                exc[0] = e

        log.info("HTTP_START  id=%s  attempt=%d/%d", req_id, attempt + 1, MAX_RETRIES)
        t = threading.Thread(target=_fetch, daemon=True)
        t.start()

        for frame in _animate("Our Lynn concierge is crafting your itinerary", t):
            yield frame
        t.join()

        http_elapsed = time.time() - t_http

        # --- handle transport errors ---
        if exc[0] is not None:
            e = exc[0]
            log.warning("HTTP_ERROR  id=%s  attempt=%d  elapsed=%.2fs  err=%s", req_id, attempt + 1, http_elapsed, e)
            if isinstance(e, httpx.TimeoutException):
                retry_reason = "timeout"
                if attempt < MAX_RETRIES - 1:
                    continue
                yield "_I wasn't able to reach the concierge service in time — please try again in a moment._"
                return
            yield f"_Unexpected error: {e}_"
            return

        resp = result[0]
        log.info(
            "HTTP_DONE  id=%s  attempt=%d  status=%d  elapsed=%.2fs",
            req_id, attempt + 1, resp.status_code, http_elapsed,
        )

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                retry_reason = "server_error"
                if attempt < MAX_RETRIES - 1:
                    continue
                yield "_The service is temporarily unavailable — please try again in a moment._"
                return
            yield f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            return

        try:
            data = resp.json()
        except Exception:
            retry_reason = "server_error"
            if attempt < MAX_RETRIES - 1:
                continue
            yield "_Received an unexpected response — please try again._"
            return

        if "error" in data:
            err = data["error"]
            log.warning("AGENT_ERROR  id=%s  code=%s  msg=%s", req_id, err.get("code"), err.get("message"))
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

        log.info(
            "AGENT_RESPONSE  id=%s  attempt=%d  state=%s  artifact_chars=%d  status_chars=%d  total_elapsed=%.2fs",
            req_id, attempt + 1, state, len(artifact_text), len(status_text), time.time() - t_total,
        )

        # The DW gateway bridge puts the itinerary in status_text, not artifacts —
        # so artifact_chars is always 0. Only retry when BOTH are truly empty.
        no_content = not artifact_text and not status_text
        if no_content and attempt < MAX_RETRIES - 1:
            retry_reason = "agent_failed" if state == "failed" else "empty_artifact"
            log.info("WILL_RETRY  id=%s  attempt=%d  reason=%s  state=%s", req_id, attempt + 1, retry_reason, state)
            continue

        # Exhausted retries with genuinely no content — friendly final message.
        if no_content:
            log.warning(
                "GIVE_UP  id=%s  state=%s  total_elapsed=%.2fs",
                req_id, state, time.time() - t_total,
            )
            yield (
                "_I wasn't able to complete your itinerary just now. "
                "Please try sending your request again._"
            )
            return

        log.info("SUCCESS  id=%s  attempts=%d  total_elapsed=%.2fs", req_id, attempt + 1, time.time() - t_total)

        body = artifact_text or status_text or f"(state: {state}, no text returned)"

        if state == "input-required" and artifact_text:
            body = f"**Agent needs more info:**\n\n{body}"
        elif state not in ("completed", "unknown", "failed", "input-required"):
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
