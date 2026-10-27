"""Tries the assistant's Claude connection for real, with your key: a question answered through a look-up, the way the assistant does it. It
costs a fraction of a cent. It is not part of the automated tests (they never use a key or the network).

Run from the web folder, with the key in the environment:
    set ANTHROPIC_API_KEY=your-key         (Windows)       export ANTHROPIC_API_KEY=your-key   (Mac, Linux)
    python tests/live_claude_check.py

Each step says PASS or FAIL and how long it took. A FAIL says what Anthropic answered, which tells whether the key, the account's credit or the
model name is the problem. The model can be changed with the environment variable ASSISTANT_MODEL (default claude-haiku-5-5), and how much
it thinks with ASSISTANT_EFFORT (default low).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import assistant  # noqa: E402

key = assistant.api_key()
if not key:
    sys.exit("Set ANTHROPIC_API_KEY first (see the top of this file).")
print(f"model={assistant.MODEL}  effort={assistant.EFFORT}")
claude = assistant.Claude(key)
failed = []


def step(name, work):
    began = time.time()
    try:
        detail = work()
    except Exception as e:   # noqa: BLE001  (this is a report, not a program)
        print(f"FAIL {name} ({time.time() - began:.1f}s): {getattr(e, 'message', None) or e}")
        failed.append(name)
        return
    print(f"PASS {name} ({time.time() - began:.1f}s): {detail}")


TOOLS = [{"name": "business_snapshot", "description": "Today's sales figures for the business.", "input_schema": {"type": "object", "properties": {}, "required": []}}]
SYSTEM = "You are the assistant in a small business app. Use the tools for figures. Answer in one short sentence." + " Padding to make this prefix long enough to be cached." * 90
messages = [{"role": "user", "content": "How much did we sell today?"}]


def looks_things_up():
    first = claude.turn(SYSTEM, messages, TOOLS)
    if not first.calls or first.calls[0]["name"] != "business_snapshot":
        raise RuntimeError(f"it did not ask for the look-up (it said: {first.text!r}, stopped: {first.stop})")
    call = first.calls[0]
    thread = messages + [
        {"role": "assistant", "content": first.content},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": json.dumps({"ok": True, "data": {"sales_today": {"invoices": 3, "total": 52.0}}})}]},
    ]
    second = claude.turn(SYSTEM, thread, TOOLS)
    if "52" not in second.text:
        raise RuntimeError(f"the answer does not use the look-up's figure: {second.text!r}")
    print(f"     said: {second.text!r}")
    return f"{second.tokens_in + first.tokens_in} tokens in, {second.tokens_out + first.tokens_out} out"


step("a question answered with a look-up (model, tools, effort, the thinking blocks sent back)", looks_things_up)
step("a second question reads the long fixed instructions from Anthropic's cache", lambda: (lambda t: f"{t.tokens_in} tokens in; check your usage page for cache reads")(claude.turn(SYSTEM, messages, TOOLS)))

print("\nEverything works." if not failed else "\nNeeds attention: " + "; ".join(failed))
sys.exit(1 if failed else 0)
