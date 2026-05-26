# Lance Namespace Read / Write Fragments Analysis

A focused analysis of how `namespace` is used in:

- reading a Lance table via `lance.dataset(...)`
- low-level distributed writes via `lance.fragment.write_fragments(...)`

Validated against:

- `pylance` **v6.0.0**
- `lance-namespace` **v0.7.6**

## What is in this repo

- `docs/namespace-read-write-fragments.md` — main analysis document
- `examples/read_with_namespace.py` — minimal read example
- `examples/distributed_write_with_namespace.py` — minimal distributed-write pattern

## Core takeaway

`namespace` plays two different roles:

1. **Read path**: `namespace` is a **table locator + storage-options provider**.
2. **write_fragments path**: `namespace` is **not the table locator by itself**; you still need a concrete `table_uri`, while `namespace` mainly helps with **credential refresh / version-management context**.

## Upstream source roots analyzed

- `../_lance_src_v6.0.0`
- `../_lance_namespace_src_v0.7.6`

## Quick summary

### Read path

```python
import lance
from lance_namespace import connect

ns = connect("dir", {"root": "memory://demo"})
ds = lance.dataset(namespace_client=ns, table_id=["workspace", "table"])
```

Internally, `lance.dataset(...)` uses `namespace.describe_table(...)` to resolve:

- table `location`
- `storage_options`
- `managed_versioning`

### `write_fragments` path

```python
from lance.fragment import write_fragments
from lance_namespace import DeclareTableRequest

resp = ns.declare_table(DeclareTableRequest(id=table_id, location=None))
table_uri = resp.location

fragments = write_fragments(
    data,
    table_uri,
    storage_options=merged_options,
    namespace_client=ns,
    table_id=table_id,
)
```

Notice the difference:

- `write_dataset(...)` can resolve the table URI from namespace for you.
- `write_fragments(...)` cannot; you must provide `table_uri` yourself.
