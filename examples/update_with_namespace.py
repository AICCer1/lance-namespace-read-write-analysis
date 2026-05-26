import lance
import lance.namespace
import pyarrow as pa

# 对 DirectoryNamespace 来说，update / delete 这类行级变更
# 默认更推荐：先通过 namespace 打开 dataset，再走 ds.update(...) / ds.delete(...)
ns = lance.namespace.DirectoryNamespace(
    root="memory://update-demo",
    table_version_tracking_enabled="true",
    manifest_enabled="true",
)

table_id = ["users"]

initial = pa.Table.from_pylist(
    [
        {"id": 1, "name": "alice", "tier": "vip", "score": 10, "is_deleted": False},
        {"id": 2, "name": "bob", "tier": "normal", "score": 20, "is_deleted": False},
        {"id": 3, "name": "carol", "tier": "vip", "score": 30, "is_deleted": True},
    ]
)

lance.write_dataset(
    initial,
    namespace_client=ns,
    table_id=table_id,
    mode="create",
)

ds = lance.dataset(namespace_client=ns, table_id=table_id)

update_stats = ds.update(
    {"score": "score + 100"},
    where="tier = 'vip'",
)
print("update stats:", update_stats)

delete_stats = ds.delete("is_deleted = true")
print("delete stats:", delete_stats)

incoming = pa.Table.from_pylist(
    [
        {"id": 2, "name": "bob", "tier": "normal", "score": 999, "is_deleted": False},
        {"id": 4, "name": "dave", "tier": "vip", "score": 77, "is_deleted": False},
    ]
)

merge_stats = (
    ds.merge_insert("id")
    .when_matched_update_all()
    .when_not_matched_insert_all()
    .execute(incoming)
)
print("merge_insert stats:", merge_stats)

latest = lance.dataset(namespace_client=ns, table_id=table_id)
print("latest version:", latest.version)
print(latest.to_table())
