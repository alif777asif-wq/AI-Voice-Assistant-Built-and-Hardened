"""R7 - nothing a model emits may crash the dispatcher; only ConfirmationRequired may escape."""
from __future__ import annotations

from harness.runtime import ClassResult, Context

KEY, RULE = "robust", "R7"
TITLE = "Malformed tool arguments crash the dispatcher"

TOOLS = {
    "open_application": ["app_name"], "close_application": ["app_name"], "adjust_volume": ["action", "percent"],
    "minimize_all_windows": [], "control_window": ["action", "target"], "open_website": ["target"],
    "search_web": ["query", "site"], "manage_browser_tabs": ["action"],
    "file_operation": ["action", "source", "destination", "content"],
    "remember_user_fact": ["key", "value"], "forget_user_fact": ["key"],
}
WEIRD = [None, 0, -1, 10**30, 3.14, True, [], [1, "a"], {}, {"a": {"b": 1}}, "", " ", "\x00", "a\x00b", "\ud800",
         "A" * 100_000, "‮", "../" * 500, "%s%s%s%n", "{0}{__class__}", "ÿ" * 3000]


def run(ctx: Context) -> dict:
    res = ClassResult(KEY, RULE, TITLE)
    disp = ctx.dispatcher(ctx.engine())
    for tool, params in TOOLS.items():
        for i, weird in enumerate(WEIRD):
            for param in params or [None]:
                args = {param: weird} if param else {}
                if param and param != params[0]:
                    args[params[0]] = "x"
                before = ctx.jail.mark()
                try:
                    r = disp.execute(tool, args)
                    bad = not isinstance(r, str)
                    why = "non-string result" if bad else ""
                except ctx.m["system_tools"].ConfirmationRequired:
                    bad, why = False, ""
                except BaseException as exc:  # noqa: BLE001
                    bad, why = True, f"{type(exc).__name__} escaped the dispatcher: {str(exc)[:80]}"
                res.add(tool, f"{KEY}-{tool}-{param}-{i}", tool, args, bad, why)
    # unknown / private tool names
    for name in ("__init__", "_validate_name", "nonexistent", "", "run_command", "power_control", "__class__"):
        try:
            r = disp.execute(name, {})
            bad = not (isinstance(r, str) and r.startswith("Error"))
            why = "" if not bad else f"unexpected result {r!r}"
        except BaseException as exc:  # noqa: BLE001
            bad, why = True, f"{type(exc).__name__} escaped"
        res.add("unknown-tool-names", f"{KEY}-name-{name or 'empty'}", name, {}, bad, why)
    return res.finish()
