import lance
import lance.namespace
import pyarrow as pa
from lance.fragment import write_fragments
from lance_namespace import DescribeTableRequest

# 仅用于演示。
# 这个例子展示低层路径：
# 1. 先通过 namespace 拿到 location / managed_versioning
# 2. worker 写 fragments
# 3. coordinator commit 时手动透传 namespace_client_managed_versioning
ns = lance.namespace.DirectoryNamespace(
    root="memory://demo",
    table_version_tracking_enabled="true",
    manifest_enabled="true",
)

table_id = ["events"]

# 先创建一个初始表，便于后面演示 append。
lance.write_dataset(
    pa.Table.from_pylist(
        [
            {"id": 1, "name": "a"},
            {"id": 2, "name": "b"},
        ]
    ),
    namespace_client=ns,
    table_id=table_id,
    mode="create",
)

# CN 先通过 namespace 获取表上下文。
resp = ns.describe_table(DescribeTableRequest(id=table_id))
if not resp.location:
    raise RuntimeError("namespace did not return table location")

table_uri = resp.location
managed = resp.managed_versioning is True
storage_options = dict(resp.storage_options or {})

# APPEND 这类操作需要基于当前读版本提交。
base = lance.dataset(namespace_client=ns, table_id=table_id)

# 模拟某个 worker 产出 fragments。
fragments = write_fragments(
    pa.Table.from_pylist(
        [
            {"id": 3, "name": "c"},
            {"id": 4, "name": "d"},
        ]
    ),
    table_uri,
    storage_options=storage_options,
    namespace_client=ns,
    table_id=table_id,
)

append_op = lance.LanceOperation.Append(fragments)

# 低层 commit 时，记得继续传 namespace_client_managed_versioning。
new_ds = lance.LanceDataset.commit(
    table_uri,
    append_op,
    read_version=base.version,
    storage_options=storage_options,
    namespace_client=ns,
    table_id=table_id,
    namespace_client_managed_versioning=managed,
)

print("new version:", new_ds.version)
print(new_ds.to_table())
