import lance
import lance.namespace
import pyarrow as pa
from lance_namespace import CreateTableIndexRequest

source = pa.Table.from_pylist(
    [
        {"id": 1, "title": "alpha"},
        {"id": 2, "title": "beta"},
        {"id": 3, "title": "gamma"},
    ]
)

ns = lance.namespace.DirectoryNamespace(
    root="memory://index-demo",
    table_version_tracking_enabled="true",
    manifest_enabled="true",
)

# 方式 1：先通过 namespace 打开 dataset，再调用 LanceDataset.create_index(...)
dataset_table_id = ["docs_from_dataset_api"]
lance.write_dataset(
    source,
    namespace_client=ns,
    table_id=dataset_table_id,
    mode="create",
)
ds = lance.dataset(namespace_client=ns, table_id=dataset_table_id)
ds.create_index("id", "BTREE", name="id_idx")
print("dataset API indices:", ds.describe_indices())

# 方式 2：直接走 namespace 原生 create_table_index(...)
native_table_id = ["docs_from_namespace_api"]
lance.write_dataset(
    source,
    namespace_client=ns,
    table_id=native_table_id,
    mode="create",
)
resp = ns.create_table_index(
    CreateTableIndexRequest(
        id=native_table_id,
        column="id",
        index_type="BTREE",
        name="id_idx",
    )
)
print("namespace create_table_index transaction_id:", resp.transaction_id)
print(
    "namespace API indices:",
    lance.dataset(namespace_client=ns, table_id=native_table_id).describe_indices(),
)
