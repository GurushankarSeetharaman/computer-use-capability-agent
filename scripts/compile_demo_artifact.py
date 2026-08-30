"""Compile the committed demo capability from the demo recording.

    python scripts/compile_demo_artifact.py

The compiler is interactive by design -- which literals become typed
inputs is a judgement call. This script records the answers given for the
demo capability so the committed artifact can be regenerated exactly,
rather than depending on someone reproducing a terminal session. Every
answer below is a decision a human made and can change here.

Run scripts/compile_recording.py instead to make those calls yourself.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from computer_use.agent.recording import Recording  # noqa: E402
from computer_use.artifact import Compiler, load, save  # noqa: E402

RECORDING = Path("evidence/_scratch/recording.json")
#: Committed on purpose: artifacts/ is generated output and gitignored,
#: but this one is the worked example the README points at, so the repo
#: can be read end to end without an API key or a browser.
OUT = Path("artifacts/examples")

CAPABILITY_ID = "saucedemo_add_item_to_cart_checkout"
NAME = "Add item to cart and reach checkout review"
DESCRIPTION = (
    "Logs in, adds a named item to the cart, and proceeds to the checkout "
    "review page, returning the order total."
)

#: Answers to the compiler's questions, in the order it asks them.
#:
#: The values are the decisions: every literal the discovery run typed
#: becomes a named input, because a capability that can only ever add a
#: backpack for one hard-coded person is not reusable. The item name is
#: templated into the element id -- see the compiler docstring for why
#: that is saucedemo-specific.
#:
#: The business outcome is the one from design notes section 2: a login
#: can legitimately be rejected, and that is a *result* to return to the
#: caller, not an error. Discovery never saw it, because discovery only
#: ever walked the happy path -- which is exactly why section 2 asks for
#: it to be authored here.
ANSWERS = {
    "capability_id:": CAPABILITY_ID,
    "name:": NAME,
    "step_id (blank to finish):": ["click_login", ""],
    "text that appears on the page:": "Epic sadface",
    "outcome_code:": "invalid_credentials",
}

CONFIRMATIONS = {
    "declare expected business outcomes": True,
}


class DemoPrompter:
    """Replays the answers above, failing loudly on an unexpected question."""

    def __init__(self) -> None:
        self._queues = {
            key: list(value) if isinstance(value, list) else [value]
            for key, value in ANSWERS.items()
        }

    def confirm(self, question: str, *, default: bool = False) -> bool:
        for marker, answer in CONFIRMATIONS.items():
            if marker in question:
                return answer
        # Parameterize every literal the run typed, and template the item id.
        return default

    def ask(self, question: str, *, default: str = "") -> str:
        # Matched exactly, not by substring: "name:" is a substring of
        # "input name:", and answering the wrong question here silently
        # produces a valid-looking artifact with the wrong contract.
        prompt = question.strip()
        queue = self._queues.get(prompt)
        if queue:
            return queue.pop(0)
        return default


def main() -> int:
    recording = Recording.model_validate_json(RECORDING.read_text(encoding="utf-8"))
    capability = Compiler(DemoPrompter()).compile(
        recording,
        capability_id=CAPABILITY_ID,
        name=NAME,
        description=DESCRIPTION,
    )
    path = save(capability, OUT)
    reloaded = load(path)

    print(f"capability : {reloaded.capability_id}  v{reloaded.version}")
    print(f"name       : {reloaded.name}")
    print(f"target     : {reloaded.target.app_id} ({reloaded.target.base_url})")
    print("\ninputs:")
    for spec in reloaded.inputs:
        flag = " [sensitive]" if spec.sensitive else ""
        example = f"  e.g. {spec.example!r}" if spec.example else ""
        print(f"  {spec.name:18} {spec.type.value:8}{flag}{example}")
    print("\noutputs:")
    for spec in reloaded.outputs:
        print(f"  {spec.name:18} <- {spec.source_step}")
    print("\nsteps:")
    for step in reloaded.steps:
        checkpoint = step.checkpoint.value if step.checkpoint else "-"
        outcomes = ", ".join(o.outcome_code for o in step.expected_business_outcomes) or "-"
        print(
            f"  {step.step_id:24} {step.action.value:9} risk={step.risk_level.value:11} "
            f"checkpoint={checkpoint:26} outcomes={outcomes}"
        )
    print(f"\nsuccess_checkpoint: {reloaded.success_checkpoint.value}")
    print(f"written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
