import lance
from lance_namespace import connect

# Example only.
# Replace with your real namespace implementation and properties.
ns = connect("rest", {"uri": "http://localhost:4099"})

table_id = ["workspace", "my_table"]

ds = lance.dataset(
    namespace_client=ns,
    table_id=table_id,
    # storage_options={...},  # optional; namespace-returned options win on conflicts
)

print("version:", ds.version)
print("rows:", ds.count_rows())
print(ds.to_table())
