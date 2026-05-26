import lance
import lance.namespace
import pyarrow as pa

# 仅用于演示。
# 这里用 DirectoryNamespace，并在创建时打开 table_version_tracking_enabled。
ns = lance.namespace.DirectoryNamespace(
    root="memory://demo",
    table_version_tracking_enabled="true",
    manifest_enabled="true",
)

table_id = ["events"]

# CREATE：write_dataset 会先走 declare_table(...)，
# 然后自动读取 response.managed_versioning 并继续传下去。
initial = pa.Table.from_pylist(
    [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
    ]
)

ds = lance.write_dataset(
    initial,
    namespace_client=ns,
    table_id=table_id,
    mode="create",
)
print("after create version:", ds.version)

# APPEND：write_dataset 会先走 describe_table(...)，
# 同样会自动读取 response.managed_versioning 并传到 commit 路径。
more = pa.Table.from_pylist(
    [
        {"id": 3, "name": "c"},
        {"id": 4, "name": "d"},
    ]
)

ds = lance.write_dataset(
    more,
    namespace_client=ns,
    table_id=table_id,
    mode="append",
)
print("after append version:", ds.version)
print(ds.to_table())
