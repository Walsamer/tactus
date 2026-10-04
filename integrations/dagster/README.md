# Dagster

Third-party execution substrate.

- Durable execution, run/step persistence, retries, re-execution.
- Execution dependencies, history, and observability.

Tactus/Ictus decide what should happen; Dagster owns **durably executing it**.
Tactus must not build another workflow/retry engine.

```text
Tactus
   ↓
Ictus
   ↓
Dagster
```
