"""Shared plumbing for the checks: the execution context and the call helper."""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Outcome:
    kind: str            # "result" | "confirmation" | "exception"
    text: str
    events: list = field(default_factory=list)      # audit-hook events during the call
    tripwires: list = field(default_factory=list)   # tripwire executions during the call


class Context:
    """Everything a check needs; built once per worker process."""

    def __init__(self, target: str, sb, jail, corpus: dict, modules: dict) -> None:
        self.target = target
        self.sb = sb
        self.jail = jail
        self.corpus = corpus
        self.m = modules          # imported target modules: system_tools, registry, llm_base
        self.real_path = modules["REAL_PATH"]
        self.skip_benign = False       # mutation runs only need the adversarial half

    def engine(self, **kwargs):
        kwargs.setdefault("allowed_roots", [str(self.sb.home)])
        return self.m["system_tools"].SystemToolsEngine(**kwargs)

    def dispatcher(self, engine):
        return self.m["registry"].ToolDispatcher(engine)

    def call(self, dispatcher, name: str, args: dict, **kw) -> Outcome:
        """Run one tool call exactly as the orchestrator does and collect evidence."""
        confirmation_cls = self.m["system_tools"].ConfirmationRequired
        jmark, tmark = self.jail.mark(), self.sb.log_mark()
        try:
            text, kind = dispatcher.execute(name, args, **kw), "result"
        except confirmation_cls as exc:
            text, kind = str(exc), "confirmation"
        except BaseException as exc:  # noqa: BLE001 - recording, not handling
            text, kind = f"{type(exc).__name__}: {exc}", "exception"
        events = self.jail.since(jmark)
        # Launched programs run asynchronously: wait until every tripwire we started has logged itself.
        expected = sum(1 for e in events if e["k"] == "exec" and e["allowed"] and self.jail.is_tripwire(e["resolved"]))
        # Wait for those tripwires to log. Normal case: they already have, no wait at all. The give-up rule needs
        # BOTH 20 s of clock time AND 5,000 polls: if the machine is paused or throttled (the clock jumps, the
        # child does not run), the poll count keeps the wait going until the child has really had time to run.
        start, polls = time.monotonic(), 0
        while len(self.sb.log_since(tmark)) < expected:
            polls += 1
            if time.monotonic() - start > 20.0 and polls > 5000:
                break
            time.sleep(0.001)
        return Outcome(kind, text, events, self.sb.log_since(tmark))


def _call_confirmed(self, dispatcher, name: str, args: dict) -> "Outcome":
    """Simulate "the user already said yes" through whichever trusted channel the target offers.

    Baseline: the orchestrator injects confirmed=True into the argument dict.
    Hardened: the orchestrator passes confirmed=True as a separate, trusted keyword.
    """
    import inspect
    if "confirmed" in inspect.signature(dispatcher.execute).parameters:
        return self.call(dispatcher, name, args, confirmed=True)
    return self.call(dispatcher, name, {**args, "confirmed": True})


Context.call_confirmed = _call_confirmed


def short(value, limit: int = 160):
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + f"...(+{len(s) - limit})"


class ClassResult:
    """Accumulates per-case records for one check."""

    def __init__(self, key: str, rule: str, title: str) -> None:
        self.key, self.rule, self.title = key, rule, title
        self.cases = 0
        self.violations = 0
        self.records: list[dict] = []     # every violating case + a sample of passing ones
        self.groups: dict[str, dict] = {}  # per sub-group counters
        self.started = time.monotonic()
        self.extra: dict = {}

    def add(self, group: str, case_id: str, tool: str, args, violation: bool, why: str = "",
            keep_pass: bool = False, **detail) -> None:
        self.cases += 1
        g = self.groups.setdefault(group, {"cases": 0, "violations": 0})
        g["cases"] += 1
        if violation:
            self.violations += 1
            g["violations"] += 1
        if violation or keep_pass:
            self.records.append({"id": case_id, "group": group, "tool": tool, "args": short(args),
                                 "violation": violation, "why": why, **detail})

    def finish(self) -> dict:
        return {"key": self.key, "rule": self.rule, "title": self.title, "cases": self.cases,
                "violations": self.violations, "groups": self.groups,
                "duration_s": round(time.monotonic() - self.started, 3),
                "records": self.records, **self.extra}
