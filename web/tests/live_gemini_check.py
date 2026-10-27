"""Tries the assistant's Gemini connection for real, with your key: a question answered through a look-up, a spoken answer, and that
recording heard again. It costs a fraction of a cent. It is not part of the automated tests (they never use a key or the network).

Run from the web folder, with the key in the environment:
    set GEMINI_API_KEY=your-key            (Windows)       export GEMINI_API_KEY=your-key   (Mac, Linux)
    python tests/live_gemini_check.py

Each step says PASS or FAIL and how long it took. A FAIL prints Google's reply, which says which model or setting it did not accept; the
models and the voice can be changed with the environment variables ASSISTANT_MODEL, ASSISTANT_LISTEN_MODEL, ASSISTANT_SPEAK_MODEL,
ASSISTANT_VOICE and ASSISTANT_THINKING (empty = leave it to the model).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import assistant  # noqa: E402

key = assistant.api_key()
if not key:
    sys.exit("Set GEMINI_API_KEY first (see the top of this file).")
print(f"models: answers={assistant.MODEL}  listening={assistant.LISTEN_MODEL}  speaking={assistant.SPEAK_MODEL} (voice {assistant.VOICE})")
gemini = assistant.Gemini(key)
failed = []


def step(name, work):
    began = time.time()
    try:
        result = work()
    except Exception as e:   # noqa: BLE001  (this is a report, not a program)
        detail = getattr(e, "message", None) or str(e)
        print(f"FAIL {name} ({time.time() - began:.1f}s): {detail}")
        failed.append(name)
        return None
    print(f"PASS {name} ({time.time() - began:.1f}s): {result[1] if isinstance(result, tuple) else result}")
    return result[0] if isinstance(result, tuple) else result


TOOL = [{"type": "function", "name": "business_snapshot", "description": "Today's sales figures for the business.",
         "parameters": {"type": "object", "properties": {}, "required": []}}]
SYSTEM = "You are the assistant in a small business app. Use the tools for figures. Answer in one short sentence."
asked = [{"type": "user_input", "content": [{"type": "text", "text": "How much did we sell today?"}]}]


def looks_things_up():
    first = gemini.turn(SYSTEM, asked, TOOL)
    if not first.calls or first.calls[0]["name"] != "business_snapshot":
        raise RuntimeError(f"it did not ask for the look-up (it said: {first.text!r})")
    call = first.calls[0]
    steps = asked + first.steps + [{"type": "function_result", "name": call["name"], "call_id": call["id"],
                                    "result": [{"type": "text", "text": json.dumps({"ok": True, "data": {"sales_today": {"invoices": 3, "total": 52.0}}})}]}]
    second = gemini.turn(SYSTEM, steps, TOOL)
    if "52" not in second.text:
        raise RuntimeError(f"the answer does not use the look-up's figure: {second.text!r}")
    return second.text, f"{second.text!r} (tokens in/out: {first.tokens_in + second.tokens_in}/{first.tokens_out + second.tokens_out})"


answer = step("a question answered with a look-up (model, tools, the stateless steps)", looks_things_up)
speech = step("an answer spoken", lambda: (lambda wav: (wav, f"{len(wav):,} bytes of WAV"))(gemini.speak("You sold fifty two kwacha today, from three invoices.")))
if speech:
    heard = step("that recording heard again", lambda: (lambda said: (said, repr(said)))(gemini.transcribe(speech)))
    if heard and "52" not in heard and "fifty" not in heard.lower():
        print("NOTE the words heard do not match what was spoken; the listening model may need changing (ASSISTANT_LISTEN_MODEL).")

print("\nEverything works." if not failed else "\nNeeds attention: " + "; ".join(failed))
sys.exit(1 if failed else 0)
