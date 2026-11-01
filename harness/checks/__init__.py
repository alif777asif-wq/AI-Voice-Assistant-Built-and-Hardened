from harness.checks import (browser, confirm, control, destroy, fsboundary, inject, overmatch,
                            pathexec, robust, sensitive)

# the nine checks run against every target; `control` is run once on its own
ALL = {m.KEY: m for m in (inject, overmatch, pathexec, browser, fsboundary, sensitive,
                          confirm, destroy, robust)}
CONTROL = control
