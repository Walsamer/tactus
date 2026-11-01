# Canary fixture

A tiny source tree the agent canary copies into a disposable git repository
(see `scripts/run_agent_canary.py`). It is not part of the Tactus package.

The canary asks the agent for exactly one small, deterministic edit: add
`sub(a, b)` to `src/calc.py` and one test for it in `tests/test_calc.py`. Keeping
the fixture this small is what makes the resulting patch checkable by machine
rather than by reading.

Run its tests from the repository root:

```bash
python3 -m unittest discover -s tests -t .
```
