import os
import re
import shutil
import tempfile
import uuid

import lance
import lance.namespace
import pyarrow as pa
from lance.fragment import write_fragments
from lance_namespace import DescribeTableRequest


# 这个脚本专门回答一个问题：
# ExternalManifestCommitHandler + namespace-managed versioning 到底防什么？
#
# 它会演示两个 case：
#
# 1. 没防住：两个 stale append writer 都从同一个 read_version 出发，最后都能成功。
#    这说明它不是“严格 read_version 乐观锁”。
#
# 2. 防住了：同一个 target version 的二次发布会被 create_table_version 拦住。
#    这说明它确实在“版本发布占位”这一层做了 CAS / put-if-not-exists 保护。
#
# 这个脚本使用本地临时目录，方便看 staging manifest / final manifest 的关系。


def make_namespace(root: str):
    return lance.namespace.DirectoryNamespace(
        root=root,
        table_version_tracking_enabled="true",
        manifest_enabled="true",
        ops_metrics_enabled="true",
    )


def create_base_table(ns, table_id):
    lance.write_dataset(
        pa.Table.from_pylist(
            [
                {"id": 1, "name": "base"},
            ]
        ),
        namespace_client=ns,
        table_id=table_id,
        mode="create",
    )


def table_context(ns, table_id):
    resp = ns.describe_table(DescribeTableRequest(id=table_id))
    if not resp.location:
        raise RuntimeError("namespace did not return table location")
    return resp.location, dict(resp.storage_options or {}), resp.managed_versioning is True


def make_fragments(ns, table_id, rows):
    table_uri, storage_options, _ = table_context(ns, table_id)
    return write_fragments(
        pa.Table.from_pylist(rows),
        table_uri,
        storage_options=storage_options,
        namespace_client=ns,
        table_id=table_id,
    )


def commit_append(ns, table_id, stale_ds, rows, max_retries=20):
    table_uri, storage_options, managed = table_context(ns, table_id)
    op = lance.LanceOperation.Append(make_fragments(ns, table_id, rows))
    return lance.LanceDataset.commit(
        table_uri,
        op,
        read_version=stale_ds.version,
        storage_options=storage_options,
        namespace_client=ns,
        table_id=table_id,
        namespace_client_managed_versioning=managed,
        max_retries=max_retries,
    )


def latest_rows(ns, table_id):
    return lance.dataset(namespace_client=ns, table_id=table_id).to_table().to_pylist()


def versions_dir(root: str, table_id):
    # DirectoryNamespace 的本地 root 下会有一个真实表目录，比如:
    #   <root>/events.lance/_versions/
    # 这里直接扫描 *_versions 目录，避免把路径写死。
    candidates = []
    for current_root, dirs, files in os.walk(root):
        if os.path.basename(current_root) == "_versions":
            candidates.append(current_root)
    if len(candidates) != 1:
        raise RuntimeError(f"expected exactly one _versions dir, got: {candidates}")
    return candidates[0]


FINAL_MANIFEST_RE = re.compile(r"^\d+\.manifest$")


def latest_final_manifest_path(root: str, table_id):
    vdir = versions_dir(root, table_id)
    finals = []
    for name in os.listdir(vdir):
        if FINAL_MANIFEST_RE.match(name):
            finals.append(os.path.join(vdir, name))
    if not finals:
        raise RuntimeError(f"no final manifest found under {vdir}")
    finals.sort(key=lambda p: int(os.path.basename(p).split(".")[0]))
    return finals[-1]


def copy_as_staging(root: str, src_manifest_abs: str, target_version: int):
    vdir = os.path.dirname(src_manifest_abs)
    staging_name = f"{target_version}.manifest-{uuid.uuid4().hex}"
    staging_abs = os.path.join(vdir, staging_name)
    shutil.copyfile(src_manifest_abs, staging_abs)

    # create_table_version 需要 object-store path，而不是宿主机绝对路径。
    staging_rel = os.path.relpath(staging_abs, root).replace(os.sep, "/")
    return staging_abs, staging_rel


def case_not_protected_stale_append():
    print("\n=== case 1: stale append -> append (not blocked) ===")
    with tempfile.TemporaryDirectory(prefix="ext-manifest-not-protected-") as root:
        ns = make_namespace(root)
        table_id = ["events"]
        create_base_table(ns, table_id)

        writer_a = lance.dataset(namespace_client=ns, table_id=table_id)
        writer_b = lance.dataset(namespace_client=ns, table_id=table_id)

        first = commit_append(
            ns,
            table_id,
            writer_a,
            [{"id": 2, "name": "append-a"}],
        )
        second = commit_append(
            ns,
            table_id,
            writer_b,
            [{"id": 3, "name": "append-b"}],
        )

        print("writer_a read_version:", writer_a.version)
        print("writer_b read_version:", writer_b.version)
        print("first commit version:", first.version)
        print("second commit version:", second.version)
        print("latest rows:", latest_rows(ns, table_id))
        print("ops metrics:", ns.retrieve_ops_metrics())
        print(
            "结论: 第二个 stale append 没被 external manifest handler 拦掉，"
            "因为它不是 strict read_version CAS；append vs append 允许 rebase 后继续提交。"
        )


def case_protected_duplicate_version_publish():
    print("\n=== case 2: duplicate create_table_version for same target version (blocked) ===")
    with tempfile.TemporaryDirectory(prefix="ext-manifest-protected-") as root:
        ns = make_namespace(root)
        table_id = ["events"]
        create_base_table(ns, table_id)

        # 找到当前 final manifest（version 1），复制成两个不同的 staging manifest，
        # 然后都尝试发布成 version 2。
        base_manifest = latest_final_manifest_path(root, table_id)
        print("base final manifest:", base_manifest)

        staging1_abs, staging1_rel = copy_as_staging(root, base_manifest, target_version=2)
        staging2_abs, staging2_rel = copy_as_staging(root, base_manifest, target_version=2)

        req1 = {
            "id": table_id,
            "version": 2,
            "manifest_path": staging1_rel,
            "manifest_size": os.path.getsize(staging1_abs),
            "naming_scheme": "V2",
        }
        req2 = {
            "id": table_id,
            "version": 2,
            "manifest_path": staging2_rel,
            "manifest_size": os.path.getsize(staging2_abs),
            "naming_scheme": "V2",
        }

        first = ns.create_table_version(req1)
        print("first create_table_version response:", first)

        try:
            second = ns.create_table_version(req2)
            print("second create_table_version unexpectedly succeeded:", second)
        except Exception as exc:
            print("second create_table_version failed as expected:", type(exc).__name__)
            print(str(exc))

        print("ops metrics:", ns.retrieve_ops_metrics())
        print(
            "结论: 同一个 target version=2 的二次发布会被 namespace 版本管理拦住。"
            "这正是 ExternalManifestCommitHandler / create_table_version 这层真正的防线。"
        )


if __name__ == "__main__":
    case_not_protected_stale_append()
    case_protected_duplicate_version_publish()
