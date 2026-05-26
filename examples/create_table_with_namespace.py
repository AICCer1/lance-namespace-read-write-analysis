import lance
import lance.namespace
import pyarrow as pa
from lance_namespace import CreateTableRequest


def table_to_ipc_bytes(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


source = pa.Table.from_pylist(
    [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
    ]
)

# 方式 1：高层 Lance 写法。
ns1 = lance.namespace.DirectoryNamespace(
    root="memory://create-via-write-dataset",
    table_version_tracking_enabled="true",
    manifest_enabled="true",
)

ds1 = lance.write_dataset(
    source,
    namespace_client=ns1,
    table_id=["events_from_write_dataset"],
    mode="create",
)
print("write_dataset created version:", ds1.version)
print(ds1.to_table())

# 方式 2：namespace 原生 create_table(...)。
ns2 = lance.namespace.DirectoryNamespace(
    root="memory://create-via-native-api",
    table_version_tracking_enabled="true",
    manifest_enabled="true",
)
resp = ns2.create_table(
    CreateTableRequest(id=["events_from_create_table"]),
    table_to_ipc_bytes(source),
)
print("create_table location:", resp.location)
print("create_table version:", resp.version)

# 创建完后，仍然可以再通过 namespace 打开成 dataset 使用。
ds2 = lance.dataset(namespace_client=ns2, table_id=["events_from_create_table"])
print(ds2.to_table())
