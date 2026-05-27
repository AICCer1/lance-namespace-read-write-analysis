import tempfile
from pprint import pprint

import lance
import pyarrow as pa
from lance.namespace import CreateNamespaceRequest, DescribeTableRequest, DirectoryNamespace


# 这个脚本的目标不是测业务结果，而是“穿刺验证”：
# 验证当 managed_versioning=True 且通过 namespace_client + table_id 访问时，
# Lance 是否真的走了 namespace-backed version path。
#
# 我们主要盯 3 个 namespace API：
# 1. list_table_versions      -> 打开 latest version
# 2. describe_table_version  -> 打开指定 version
# 3. create_table_version    -> CREATE / APPEND commit 发布新版本
#
# 另外，这里把 table_version_storage_enabled=true 也打开，
# 这样 DirectoryNamespace 在 list/describe version 时会尽量走 __manifest 版本存储，
# 而不是 fallback 到物理 _versions/ 目录扫描。


def metric(ns: DirectoryNamespace, name: str) -> int:
    return ns.retrieve_ops_metrics().get(name, 0)


with tempfile.TemporaryDirectory() as tmpdir:
    ns = DirectoryNamespace(
        root=tmpdir,
        table_version_tracking_enabled="true",
        manifest_enabled="true",
        table_version_storage_enabled="true",
        ops_metrics_enabled="true",
    )

    ns.create_namespace(CreateNamespaceRequest(id=["workspace"]))
    table_id = ["workspace", "probe_table"]

    initial = pa.Table.from_pylist(
        [
            {"id": 1, "name": "a"},
            {"id": 2, "name": "b"},
        ]
    )

    print("=== Step 1: CREATE via namespace_client + table_id ===")
    ds = lance.write_dataset(
        initial,
        namespace_client=ns,
        table_id=table_id,
        mode="create",
    )
    print("created version:", ds.version)

    desc = ns.describe_table(DescribeTableRequest(id=table_id))
    if not desc.location:
        raise RuntimeError("namespace did not return table location")
    if desc.managed_versioning is not True:
        raise RuntimeError(
            f"expected managed_versioning=True, got {desc.managed_versioning}"
        )

    table_uri = desc.location
    print("table uri:", table_uri)
    print("managed_versioning:", desc.managed_versioning)

    # create 本身应该已经用 create_table_version 发布了一次 version=1
    assert metric(ns, "create_table_version") == 1, (
        "expected create_table_version to be called once during CREATE"
    )

    print("namespace metrics after CREATE:")
    pprint(ns.retrieve_ops_metrics())

    print("\n=== Step 2: reset metrics, then open latest via namespace ===")
    ns.reset_ops_metrics()
    ds_latest = lance.dataset(namespace_client=ns, table_id=table_id)
    assert ds_latest.version == 1
    assert metric(ns, "list_table_versions") == 1, (
        "expected latest open via namespace to call list_table_versions exactly once"
    )
    assert metric(ns, "describe_table_version") == 0
    assert metric(ns, "create_table_version") == 0
    print("version from namespace open(latest):", ds_latest.version)
    print("namespace metrics:")
    pprint(ns.retrieve_ops_metrics())

    print("\n=== Step 3: reset metrics, then open latest directly by URI (negative control) ===")
    ns.reset_ops_metrics()
    ds_direct = lance.dataset(table_uri)
    assert ds_direct.version == 1
    assert metric(ns, "list_table_versions") == 0
    assert metric(ns, "describe_table_version") == 0
    assert metric(ns, "create_table_version") == 0
    print("version from direct URI open(latest):", ds_direct.version)
    print("namespace metrics:")
    pprint(ns.retrieve_ops_metrics())

    print("\n=== Step 4: reset metrics, then APPEND via namespace ===")
    ns.reset_ops_metrics()
    more = pa.Table.from_pylist(
        [
            {"id": 3, "name": "c"},
            {"id": 4, "name": "d"},
        ]
    )
    ds_after_append = lance.write_dataset(
        more,
        namespace_client=ns,
        table_id=table_id,
        mode="append",
    )
    assert ds_after_append.version == 2
    assert metric(ns, "create_table_version") == 1, (
        "expected append via namespace to call create_table_version exactly once"
    )
    print("version after namespace append:", ds_after_append.version)
    print("namespace metrics:")
    pprint(ns.retrieve_ops_metrics())

    print("\n=== Step 5: reset metrics, then open version=1 via namespace ===")
    ns.reset_ops_metrics()
    ds_v1 = lance.dataset(namespace_client=ns, table_id=table_id, version=1)
    assert ds_v1.version == 1
    assert metric(ns, "describe_table_version") == 1, (
        "expected namespace open(version=1) to call describe_table_version exactly once"
    )
    assert metric(ns, "list_table_versions") == 0
    assert metric(ns, "create_table_version") == 0
    print("version from namespace open(version=1):", ds_v1.version)
    print("namespace metrics:")
    pprint(ns.retrieve_ops_metrics())

    print("\n=== Step 6: reset metrics, then open version=1 directly by URI (negative control) ===")
    ns.reset_ops_metrics()
    ds_v1_direct = lance.dataset(table_uri, version=1)
    assert ds_v1_direct.version == 1
    assert metric(ns, "describe_table_version") == 0
    assert metric(ns, "list_table_versions") == 0
    assert metric(ns, "create_table_version") == 0
    print("version from direct URI open(version=1):", ds_v1_direct.version)
    print("namespace metrics:")
    pprint(ns.retrieve_ops_metrics())

    print("\n=== Probe success ===")
    print("结论：")
    print("1. 用 namespace_client + table_id 打开 latest，会命中 list_table_versions")
    print("2. 用 namespace_client + table_id 打开指定 version，会命中 describe_table_version")
    print("3. 用 namespace_client + table_id 做 create/append，会命中 create_table_version")
    print("4. 直接用 table_uri 打开，不会命中这些 namespace version API")
