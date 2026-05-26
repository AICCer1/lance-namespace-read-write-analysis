import lance
import pyarrow as pa
from lance.fragment import write_fragments
from lance_namespace import DeclareTableRequest, connect

# Example only.
ns = connect("rest", {"uri": "http://localhost:4099"})
table_id = ["workspace", "my_table"]

# 1) Declare the table through namespace so we get the concrete table URI.
resp = ns.declare_table(DeclareTableRequest(id=table_id, location=None))
if not resp.location:
    raise RuntimeError("namespace did not return table location")

table_uri = resp.location
managed = resp.managed_versioning is True

# 2) Merge storage options returned by namespace.
merged_options = {}
if resp.storage_options:
    merged_options.update(resp.storage_options)

# 3) Produce fragment sets, potentially from different workers.
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

# 4) Commit the fragments into a dataset version.
schema = pa.schema([("a", pa.int64())])
operation = lance.LanceOperation.Overwrite(schema, frag_a + frag_b)

ds = lance.LanceDataset.commit(
    table_uri,
    operation,
    storage_options=merged_options,
    namespace_client=ns,
    table_id=table_id,
    namespace_client_managed_versioning=managed,
)

print("committed version:", ds.version)
print(ds.to_table())
