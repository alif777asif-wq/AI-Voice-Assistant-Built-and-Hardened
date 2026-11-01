| Rule | Requirement | Original code: rule-breaking calls | Hardened code | Real-use calls failing (original / hardened) |
|---|---|---:|---:|---:|
| R1 | Model text is never interpreted by a shell | 0 of 1,024 | 0 of 1,024 | - |
| R2 | Only a spoken yes can confirm a risky action | 40 of 56 | 0 of 56 | - |
| R3 | close_application signals only the process it names | 15 of 513 | 0 of 513 | 7 / 0 of 1,378 |
| R4 | open_application starts only a bare program name | 1,701 of 1,756 | 0 of 1,756 | 0 / 0 of 7,802 |
| R5a | File actions stay inside the allowed folders | 0 of 12,292 | 0 of 12,292 | 0 / 0 of 5,848 |
| R5b | Startup, SSH and credential files are protected | 41 of 60 | 0 of 60 | - |
| R5c/d | No silent root deletion or overwrite on rename | 5 of 6 | 0 of 6 | - |
| R6 | The browser gets only well-formed web addresses | 56 of 646 | 0 of 646 | 0 / 0 of 15,604 |
| R7 | A malformed tool call never crashes a turn | 0 of 385 | 0 of 385 | - |
| **All** | **6 of 9 requirements broken in the original** | **1,858 of 16,738** | **0 of 16,738** | **7 / 0 of 30,632** |

| Verification | Result |
|---|---|
| Project's own tests, original code | 223 passed, 0 failed |
| Whole suite on hardened code (original + 113 new) | 336 passed, 0 failed |
| New behaviour tests run against the original code | 54 fail, 4 pass (they can fail) |
| Harness's own tests | 31 passed, 0 failed |
| Planted bugs (each fix re-broken once) | 10 of 10 caught; 1 harmless variant correctly not flagged |
| Kernel execve trace = tripwire log = audit hook (original / hardened) | 7,882 = 7,882 = 7,882 / 7,827 = 7,827 = 7,827 |
| One real `pkill` call with the wildcard `.*` (bystanders killed, original / hardened) | 4 of 4 / 0 of 4 |
| Claims checked by `run_all.py` | 23 of 23 pass |
