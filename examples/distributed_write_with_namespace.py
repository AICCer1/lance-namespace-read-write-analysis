import lance
import pyarrow as pa
from lance.fragment import write_fragments
from lance_namespace import DeclareTableRequest, connect

# 仅用于演示。
# 这个例子在单进程里模拟：
# 1. CN 先通过 namespace 拿到真实 table_uri
# 2. 多个 DN 各自写 fragment
# 3. 最后由 CN 统一 commit
ns = connect("rest", {"uri": "http://localhost:4099"})
table_id = ["workspace", "my_table"]

# 1) 先通过 namespace 声明表，并拿到具体表地址。
resp = ns.declare_table(DeclareTableRequest(id=table_id, location=None))
if not resp.location:
    raise RuntimeError("namespace did not return table location")

table_uri = resp.location
managed = resp.managed_versioning is True

# 2) 合并 namespace 返回的存储参数。
merged_options = {}
if resp.storage_options:
    merged_options.update(resp.storage_options)

# 3) 模拟多个 DN 分别产出 fragment。
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

# 4) 由 CN 收集 fragment 并提交新版本。
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
