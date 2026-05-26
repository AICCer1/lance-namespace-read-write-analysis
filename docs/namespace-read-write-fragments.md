# How Namespace Is Used When Reading Lance Tables and Writing Fragments

## Versions analyzed

- `pylance` / `lance` source: `v6.0.0`
- `lance-namespace` source: `v0.7.6`

---

## 1. Big picture

`lance-namespace` itself is intentionally thin.

It mainly provides:

- the `LanceNamespace` abstract interface
- the `connect()` factory
- the plugin registry (`register_namespace_impl`)
- shared request / response models
- shared error types

The actual built-in implementations used by Python live in `pylance`:

- `lance.namespace.DirectoryNamespace`
- `lance.namespace.RestNamespace`

### Native implementation mapping

`lance-namespace` hardcodes these aliases:

- `"dir" -> "lance.namespace.DirectoryNamespace"`
- `"rest" -> "lance.namespace.RestNamespace"`

Source:

- `_lance_namespace_src_v0.7.6/python/lance_namespace/lance_namespace/__init__.py#L1086-L1173`

---

## 2. Read path: how `namespace` is used by `lance.dataset(...)`

### Public API shape

```python
import lance

# Either provide uri...
ds = lance.dataset("s3://bucket/table.lance")

# ...or provide namespace_client + table_id
nds = lance.dataset(
    namespace_client=ns,
    table_id=["workspace", "table"],
)
```

### Key rule

When using namespace-based reads:

- provide `namespace_client`
- provide `table_id`
- **do not** provide `uri`

Source:

- `_lance_src_v6.0.0/python/python/lance/__init__.py#L90-L239`

### Internal read flow

```mermaid
flowchart TD
    A[Caller invokes lance.dataset(namespace_client, table_id)] --> B[Validate: either uri OR namespace_client+table_id]
    B --> C[Build DescribeTableRequest]
    C --> D[namespace_client.describe_table(request)]
    D --> E[Response: location + storage_options + managed_versioning]
    E --> F[Merge namespace storage_options with user storage_options]
    F --> G[Create LanceDataset/_Dataset]
    G --> H[Attach dynamic storage options provider in Rust]
    H --> I[Read scans / file access]
    I --> J[Refresh credentials when needed]
```

### What exactly is fetched from namespace

`lance.dataset(...)` calls:

```python
request = DescribeTableRequest(id=table_id, version=version)
response = namespace_client.describe_table(request)
```

Then it reads:

- `response.location`
- `response.storage_options`
- `response.managed_versioning`

and uses them to construct the dataset.

Source:

- `_lance_src_v6.0.0/python/python/lance/__init__.py#L203-L233`

### What `managed_versioning` changes

If namespace returns `managed_versioning=True`, dataset version lookup follows namespace version APIs instead of relying only on native Lance version discovery.

Observed behavior in tests:

- opening latest version triggers `list_table_versions`
- opening a specific version triggers `describe_table_version`

Source:

- `_lance_src_v6.0.0/python/python/tests/test_namespace_dir.py#L923-L979`

### Why this matters

On the read path, namespace is doing more than metadata lookup.
It becomes a live source of:

- **table resolution** (`table_id -> location`)
- **storage connection material** (`storage_options`)
- **credential refresh behavior** (through the storage options provider)
- **version-management policy** (`managed_versioning`)

---

## 3. `write_dataset(...)` vs `write_fragments(...)`

This is the most important distinction.

### `write_dataset(...)`

`write_dataset(...)` can do namespace resolution for you.

If you pass:

- `namespace_client`
- `table_id`

then it can:

- call `declare_table()` in `mode="create"`
- call `describe_table()` in `mode="append"` / `mode="overwrite"`
- fetch `location`
- fetch `storage_options`
- pass managed-versioning context through to commit

Source:

- `_lance_src_v6.0.0/python/python/lance/dataset.py#L6544-L6684`

### `write_fragments(...)`

`write_fragments(...)` is lower-level.

It **does accept**:

- `namespace_client`
- `table_id`

but this is **not enough to locate the table by itself**.

You still need to pass a concrete dataset URI / table URI as `dataset_uri`.

Source:

- `_lance_src_v6.0.0/python/python/lance/fragment.py#L1130-L1173`

### The mental model

- `write_dataset(...)` = namespace-aware convenience write path
- `write_fragments(...)` = low-level fragment materialization path

For `write_fragments(...)`, namespace is mainly there for:

- storage-options provider creation
- credential refresh
- keeping namespace/table context attached to low-level file work

It is **not** acting as the automatic `table_id -> table_uri` resolver in that API.

---

## 4. How `write_fragments(...)` should be used with namespace

## 4.1 CREATE / distributed-first-write path

Typical pattern:

1. call `namespace.declare_table(...)`
2. get `response.location`
3. merge `response.storage_options`
4. call `write_fragments(..., table_uri, namespace_client=..., table_id=...)`
5. build an operation / transaction
6. call `LanceDataset.commit(...)`

### Call-flow diagram

```mermaid
flowchart TD
    A[Worker / coordinator wants distributed write] --> B[namespace.declare_table(table_id)]
    B --> C[Response: location + storage_options + managed_versioning]
    C --> D[Coordinator chooses concrete table_uri = response.location]
    D --> E[Workers call write_fragments(data, table_uri, namespace_client, table_id)]
    E --> F[FragmentMetadata or Transaction returned]
    F --> G[Coordinator builds LanceOperation / collects Transaction]
    G --> H[LanceDataset.commit(table_uri, operation, namespace_client, table_id, namespace_client_managed_versioning)]
    H --> I[Namespace-aware version commit path]
```

### Real upstream test pattern

The upstream integration test does exactly this:

1. `DeclareTableRequest(id=table_id, location=None)`
2. `response = ns_client.declare_table(request)`
3. `table_uri = response.location`
4. `merged_options = base_storage_options + response.storage_options`
5. multiple `write_fragments(..., table_uri, namespace_client=ns_client, table_id=table_id)`
6. `LanceDataset.commit(table_uri, operation, ..., namespace_client=ns_client, table_id=table_id)`

Source:

- `_lance_src_v6.0.0/python/python/tests/test_namespace_integration.py#L563-L650`

---

## 4.2 APPEND / OVERWRITE path

For existing tables, the pattern is similar except you usually start from `describe_table(...)` instead of `declare_table(...)`.

1. `describe_table(id=table_id)`
2. get `location`
3. merge returned `storage_options`
4. call `write_fragments(...)` against that `table_uri`
5. commit the generated operation / transaction

---

## 5. Why `write_fragments(...)` still accepts `namespace_client`

At first glance this looks odd: if you already have `table_uri`, why pass namespace again?

Because low-level read/write/file paths can still benefit from namespace-based storage refresh.

The code comments explicitly say the storage options provider is created automatically when `namespace_client` and `table_id` are provided.

Related source points:

- `_lance_src_v6.0.0/python/python/lance/fragment.py#L1130-L1138`
- `_lance_src_v6.0.0/python/python/lance/dataset.py#L6577-L6684`
- `_lance_src_v6.0.0/python/python/lance/dataset.py#L3960-L4125`

So the meaning is:

- `table_uri` tells Lance **where** to write/read
- `namespace_client + table_id` tells Lance **how to keep access valid over time** and **how versioning should be handled**

---

## 6. Minimal examples

## 6.1 Reading through namespace

```python
import lance
from lance_namespace import connect

ns = connect("rest", {"uri": "http://localhost:4099"})

ds = lance.dataset(
    namespace_client=ns,
    table_id=["workspace", "my_table"],
)

print(ds.version)
print(ds.count_rows())
print(ds.to_table())
```

## 6.2 Distributed write with `write_fragments(...)`

```python
import lance
import pyarrow as pa
from lance.fragment import write_fragments
from lance_namespace import connect, DeclareTableRequest

ns = connect("rest", {"uri": "http://localhost:4099"})
table_id = ["workspace", "my_table"]

# Step 1: declare table and get real URI
resp = ns.declare_table(DeclareTableRequest(id=table_id, location=None))
table_uri = resp.location
managed = (resp.managed_versioning is True)

# Step 2: merge storage options
merged_options = {}
if resp.storage_options:
    merged_options.update(resp.storage_options)

# Step 3: workers write fragments
frag_a = write_fragments(
    pa.Table.from_pylist([{"a": 1}, {"a": 2}]),
    table_uri,
    storage_options=merged_options,
    namespace_client=ns,
    table_id=table_id,
)
frag_b = write_fragments(
    pa.Table.from_pylist([{"a": 3}, {"a": 4}]),
    table_uri,
    storage_options=merged_options,
    namespace_client=ns,
    table_id=table_id,
)

# Step 4: commit collected fragments
op = lance.LanceOperation.Overwrite(pa.schema([("a", pa.int64())]), frag_a + frag_b)

ds = lance.LanceDataset.commit(
    table_uri,
    op,
    storage_options=merged_options,
    namespace_client=ns,
    table_id=table_id,
    namespace_client_managed_versioning=managed,
)
```

---

## 7. Common mistakes

### Mistake 1: expecting `write_fragments(...)` to resolve table URI from namespace

This does **not** work conceptually:

```python
write_fragments(data, namespace_client=ns, table_id=table_id)
```

Why: `write_fragments(...)` still requires the dataset URI / table URI argument.

### Mistake 2: passing both `uri` and `namespace_client + table_id` to `lance.dataset(...)`

The code explicitly rejects this.

### Mistake 3: ignoring namespace-returned storage options

If namespace vends storage options (especially temporary credentials), you should merge them into the options used for reads/writes.

### Mistake 4: forgetting managed versioning during commit

If namespace indicates `managed_versioning=True`, keep that context flowing into commit paths.

---

## 8. Precise source map

### `lance-namespace` contract layer

- alias mapping and `connect()`:
  - `_lance_namespace_src_v0.7.6/python/lance_namespace/lance_namespace/__init__.py#L1086-L1173`
- error model:
  - `_lance_namespace_src_v0.7.6/python/lance_namespace/lance_namespace/errors.py#L43-L160`

### Python implementations in `pylance`

- `DirectoryNamespace`:
  - `_lance_src_v6.0.0/python/python/lance/namespace.py#L253-L445`
- `RestNamespace`:
  - `_lance_src_v6.0.0/python/python/lance/namespace.py#L854-L990`
- `DynamicContextProvider` and provider construction:
  - `_lance_src_v6.0.0/python/python/lance/namespace.py#L120-L220`
- `RestAdapter`:
  - `_lance_src_v6.0.0/python/python/lance/namespace.py#L1444-L1485`

### Read path

- `lance.dataset(...)` namespace flow:
  - `_lance_src_v6.0.0/python/python/lance/__init__.py#L90-L239`
- dataset credential refresh hooks / file-session handoff:
  - `_lance_src_v6.0.0/python/python/lance/dataset.py#L582-L624`
  - `_lance_src_v6.0.0/python/python/lance/dataset.py#L2618-L2669`

### Write path

- namespace-aware write path in `write_dataset(...)`:
  - `_lance_src_v6.0.0/python/python/lance/dataset.py#L6544-L6684`
- commit path carrying namespace context:
  - `_lance_src_v6.0.0/python/python/lance/dataset.py#L3960-L4125`
- low-level `write_fragments(...)` namespace parameters:
  - `_lance_src_v6.0.0/python/python/lance/fragment.py#L1080-L1173`

### Tests

- managed versioning + read behavior:
  - `_lance_src_v6.0.0/python/python/tests/test_namespace_dir.py#L910-L979`
- distributed write pattern using `declare_table -> write_fragments -> commit`:
  - `_lance_src_v6.0.0/python/python/tests/test_namespace_integration.py#L563-L650`

---

## 9. Final takeaway

If you want one sentence to keep in your head:

> **`lance.dataset(...)` can ask namespace where the table is; `write_fragments(...)` cannot — it needs the real URI first, and uses namespace mainly to keep credentials/versioning context attached.**
