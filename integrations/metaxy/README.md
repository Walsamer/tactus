# Metaxy

Third-party provenance/metadata system, tightly integrated with the Dagster
execution plane.

```text
Dagster
   +
Metaxy
```

Enriches execution with data/artifact lineage, versions, feature/sample
provenance, incremental data/ML traceability, and code/data version
relationships.

Metaxy is **not** Tactus's universal audit log. General Work Order / agent /
decision provenance belongs to Tactus + Ictus + Dagster. Metaxy is especially
relevant for data/ML workloads.
