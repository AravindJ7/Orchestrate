import json
from agent import nvidia_client
from agent.tools import TOOLS, run_tool, get_engine
from agent.prompts import SYSTEM_PROMPT


REQUIRED_STEP_KEYS = {"phase", "system_id", "resource_id", "operation", "state", "evidence_id", "step_no"}
VALID_PHASES = {"FINDING", "REMEDIATION", "VERIFICATION"}
VALID_STATUSES = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}


def _validate_steps(steps):
    problems = []
    if not isinstance(steps, list):
        return ["steps is not a list"]
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            problems.append(f"step {i} is not an object")
            continue
        missing = REQUIRED_STEP_KEYS - set(step)
        extra = set(step) - REQUIRED_STEP_KEYS
        if missing:
            problems.append(f"step {i} missing keys: {sorted(missing)}")
        if extra:
            problems.append(f"step {i} unexpected keys: {sorted(extra)}")
        if step.get("phase") not in VALID_PHASES:
            problems.append(f"step {i} invalid phase: {step.get('phase')!r}")
        if step.get("phase") == "FINDING" and not step.get("evidence_id"):
            problems.append(f"step {i} FINDING without evidence_id")
        if step.get("phase") in {"REMEDIATION", "VERIFICATION"} and step.get("evidence_id"):
            # only invalid when the audit is INCOMPLETE (post-remediation checks)
            pass
    phases = [s.get("phase") for s in steps if isinstance(s, dict)]
    order = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
    ranks = [order.get(p, 99) for p in phases]
    if ranks != sorted(ranks):
        problems.append("phases are not ordered FINDING -> REMEDIATION -> VERIFICATION")
    return problems


class AgentOrchestrator:
    """LLM task-manager that drives the deterministic audit engine via tool calls."""

    def __init__(self, model=None, models=None):
        self.models = models or ([model] if model else nvidia_client.get_model_candidates())
        if not self.models:
            raise ValueError(
                "No NVIDIA model configured. Run: python -m agent.model_discovery "
                "and set NVIDIA_MODEL in .env"
            )
        self.model = self.models[0]

    def _chat(self, messages, client):
        last_error = None
        for model in self.models:
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                    temperature=0,
                    max_tokens=2048,
                )
                self.model = model
                return response
            except Exception as e:  # timeout / 404 / rate limit -> try next model
                last_error = e
                continue
        raise RuntimeError(f"All models failed. Last error: {last_error}")

    def analyze(self, case: dict, max_iterations: int = 12):
        trace = []
        try:
            client = nvidia_client.get_client()
        except Exception as e:
            return {"error": str(e), "trace": trace}

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Analyze the following deletion request case:\n{json.dumps(case, indent=2)}"},
        ]
        last_analysis = None

        for _ in range(max_iterations):
            try:
                response = self._chat(messages, client)
            except Exception as e:
                return {
                    "error": f"Model call failed: {e}",
                    "trace": trace,
                    "model": self.model,
                    "deterministic_fallback": self._deterministic(case),
                }

            msg = response.choices[0].message
            content = (msg.content or "").strip()
            if content:
                trace.append({"type": "thought", "content": content})

            if msg.tool_calls:
                messages.append(msg)
                for tc in msg.tool_calls:
                    tool_name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except Exception:
                        args = {}
                    trace.append({"type": "tool_call", "tool": tool_name, "args": args})
                    result = run_tool(tool_name, args)
                    trace.append({"type": "tool_result", "tool": tool_name, "result": result})
                    if tool_name == "analyze_case" and isinstance(result, dict) and result.get("steps") is not None:
                        last_analysis = result
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    })
                continue

            parsed = self._parse_final(content, case)
            if not parsed.get("schema_valid") and last_analysis:
                # The model's prose/JSON was malformed; recover from the tool output it actually called.
                recovered = {
                    "parsed": True,
                    "case_id": case.get("case_id"),
                    "deletion_status": last_analysis.get("deletion_status"),
                    "steps": last_analysis.get("steps"),
                    "steps_json": json.dumps(last_analysis.get("steps"), separators=(",", ":")),
                    "schema_valid": True,
                    "problems": parsed.get("problems", []),
                    "recovered_from_tool": True,
                }
                parsed = recovered

            trace.append({"type": "final", "content": content})
            deterministic = self._deterministic(case)
            return {
                "answer": content,
                "parsed": parsed,
                "comparison": self._compare(parsed, deterministic),
                "trace": trace,
                "model": self.model,
                "deterministic": deterministic,
            }

        return {
            "error": "Max iterations reached without a final answer",
            "trace": trace,
            "model": self.model,
            "deterministic_fallback": self._deterministic(case),
        }

    @staticmethod
    def _compare(parsed, deterministic):
        if not parsed or not parsed.get("schema_valid"):
            return {"status_match": False, "steps_match": False, "note": "agent output failed validation"}
        if not deterministic or deterministic.get("error"):
            return {"status_match": None, "steps_match": None, "note": "deterministic reference unavailable"}
        return {
            "status_match": parsed.get("deletion_status") == deterministic.get("deletion_status"),
            "steps_match": parsed.get("steps") == deterministic.get("steps"),
            "note": "engine is authoritative",
        }

    @staticmethod
    def _parse_final(content: str, case: dict):
        """Extract case_id/deletion_status/steps_json from the model's final message."""
        text = (content or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        # be tolerant: grab the outermost JSON object
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
        try:
            data = json.loads(text)
        except Exception:
            return {"parsed": False, "problems": ["final answer is not valid JSON"], "raw": content}

        steps = data.get("steps_json", data.get("steps"))
        if isinstance(steps, str):
            steps_text = steps.strip()
            start_b, end_b = steps_text.find("["), steps_text.rfind("]")
            if start_b != -1 and end_b > start_b:
                steps_text = steps_text[start_b:end_b + 1]
            try:
                steps = json.loads(steps_text)
            except Exception:
                return {"parsed": False, "problems": ["steps_json is not valid JSON"], "raw": content}

        status = data.get("deletion_status")
        problems = []
        if status not in VALID_STATUSES:
            problems.append(f"invalid deletion_status: {status!r}")
        problems.extend(_validate_steps(steps))

        return {
            "parsed": True,
            "case_id": data.get("case_id", case.get("case_id")),
            "deletion_status": status,
            "steps": steps,
            "steps_json": json.dumps(steps, separators=(",", ":")),
            "schema_valid": not problems,
            "problems": problems,
        }


    @staticmethod
    def _deterministic(case: dict):
        """Ground truth from the deterministic engine (used for verification)."""
        try:
            status, steps = get_engine().analyze_case(case)
            return {
                "case_id": case.get("case_id"),
                "deletion_status": status,
                "steps": steps,
                "steps_json": json.dumps(steps, separators=(",", ":")),
            }
        except Exception as e:
            return {"error": str(e)}


def run_agent(case: dict):
    return AgentOrchestrator().analyze(case)
