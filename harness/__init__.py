"""Adversarial test harness for LLM tool-call execution layers.

Runs real attack strings against the real tool engine and judges the outcome
with operating-system level oracles (tripwire executables, canary files,
a real `pgrep`, interpreter audit events), never by inspecting the engine's
own return text.
"""
