import lance
from lance_namespace import connect

# 仅用于演示。
# 请替换成你自己的 namespace 实现和连接参数。
ns = connect("rest", {"uri": "http://localhost:4099"})

table_id = ["workspace", "my_table"]

ds = lance.dataset(
    namespace_client=ns,
    table_id=table_id,
    # storage_options={...},  # 可选；若与 namespace 返回值冲突，通常以后者为准
)

print("version:", ds.version)
print("rows:", ds.count_rows())
print(ds.to_table())
