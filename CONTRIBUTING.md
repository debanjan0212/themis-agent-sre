# How to work on Themis

## Branches

- `main` is the only long-lived branch. It always passes CI.
- Nobody pushes straight to it. Every change is a branch and a pull request into `main`.

## A normal change

```bash
git switch main && git pull
git switch -c feature/short-name
pip install -e ".[dev,postgres,ui-test]"
# ... make the change ...
make check          # lint, tests, security scan: what CI runs
git push -u origin feature/short-name    # then open a pull request into main
```

## What a change needs

- **A test that fails without it.** One good test, not coverage. New logic in `slo.py`, `autonomy.py` or `rca.py`
  usually wants a property test as well as an example: those files decide who loses autonomy.
- **No new dependency for a few lines.** The runtime depends on eight packages. Keep it that way.
- **No secret-looking strings**, not even fake ones in tests. Generate keys at run time (`security.new_key()`).
- **A `shortcut:` comment** when you knowingly leave a limit: say what the limit is and when to upgrade.
- **Docs in the same pull request** when behaviour changes: `docs/CONFIGURATION.md` for a variable,
  `docs/TELEMETRY.md` for an attribute, `docs/API.md` for a route.

## Where things are

| Path | What is in it |
|---|---|
| `themis/otlp.py` | OTLP in, span rows out. The telemetry trust boundary |
| `themis/slo.py`, `autonomy.py`, `rca.py` | Pure functions: burn rates, the ladder, root cause. No I/O |
| `themis/engine.py` | The evaluator loop that uses them |
| `themis/api.py`, `security.py` | Routes, keys, scopes, limits |
| `themis/client.py` | The gate client for agent runtimes |
| `themis/chaos.py` | Faults and how an experiment is scored |
| `themis/world/` | The ten-agent simulator and its fleet manifest |
| `themis/lab.py` | `themis demo` and `themis bench` |
| `themis/web/` | The dashboard: one HTML file, one stylesheet, one script |
| `tests/` | `test_system.py` runs everything for real; `test_engine.py` walks the control loop with a fake clock |
| `deploy/` | `intent.yaml` (the source) and `kubernetes/` (rendered by IntentOps, checked by a test) |

## Running parts of the suite

```bash
pytest tests/test_slo_autonomy.py tests/test_rca.py     # the maths, under a second
pytest tests/test_engine.py                             # the control loop, two seconds
pytest tests/test_system.py                             # the whole thing for real, about a minute
THEMIS_TEST_PG_URL=postgresql+psycopg://postgres@localhost/postgres pytest    # everything again on Postgres
make ecosystem                                          # against the sibling repositories
make bench                                              # rewrite docs/RESULTS.md from a fresh run
```

`docs/RESULTS.md` is written by `themis bench`. Do not edit it by hand.
