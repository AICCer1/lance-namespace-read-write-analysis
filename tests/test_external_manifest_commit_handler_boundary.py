import os
import re
import shutil
import tempfile
import uuid

import lance
import lance.namespace
import pyarrow as pa
import pytest
from lance.fragment import write_fragments
from lance_namespace import DescribeTableRequest


"""
这个测试文件专门验证 ExternalManifestCommitHandler / namespace-managed
create_table_version 的保护边界。

覆盖两个关键结论：

1. 没防住：stale append -> append 仍可成功
   说明它不是 strict read_version CAS

2. 防住了：同一个 target version 的重复发布会失败
   说明它确实在 version publish claim 这一层做了保护
"""


FINAL_MANIFEST_RE = re.compile(r"^\d+\.manifest$")


@pytest.fixture
def temp_root():
    with tempfile.TemporaryDirectory(prefix="ext-manifest-boundary-") as root:
        yield root


@pytest.fixture
def table_id():
    return ["events"]


@pytest.fixture
def managed_ns(temp_root):
    return lance.namespace.DirectoryNamespace(
        root=temp_root,
        table_version_tracking_enabled="true",
        manifest_enabled="true",
        ops_metrics_enabled="true",
    )


@pytest.fixture
def plain_dataset_uri(temp_root):
    return os.path.join(temp_root, "plain-events.lance")


def base_table():
    return pa.Table.from_pylist(
        [
            {"id": 1, "name": "base"},
        ]
    )


def create_base_table(ns, table_id):
    return lance.write_dataset(
        base_table(),
        namespace_client=ns,
        table_id=table_id,
        mode="create",
    )


def create_base_table_plain(uri):
    return lance.write_dataset(base_table(), uri)


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


def latest_rows_plain(uri):
    return lance.dataset(uri).to_table().to_pylist()


def versions_dir(root: str):
    candidates = []
    for current_root, dirs, files in os.walk(root):
        if os.path.basename(current_root) == "_versions":
            candidates.append(current_root)
    if len(candidates) != 1:
        raise RuntimeError(f"expected exactly one _versions dir, got: {candidates}")
    return candidates[0]


def latest_final_manifest_path(root: str):
    vdir = versions_dir(root)
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
    staging_rel = os.path.relpath(staging_abs, root).replace(os.sep, "/")
    return staging_abs, staging_rel


def commit_append_plain(uri, stale_ds, rows, max_retries=0):
    op = lance.LanceOperation.Append(
        write_fragments(pa.Table.from_pylist(rows), uri)
    )
    return lance.LanceDataset.commit(
        uri,
        op,
        read_version=stale_ds.version,
        max_retries=max_retries,
    )


def test_without_namespace_same_target_version_conflict_is_blocked(
    plain_dataset_uri,
):
    create_base_table_plain(plain_dataset_uri)

    writer_a = lance.dataset(plain_dataset_uri)
    writer_b = lance.dataset(plain_dataset_uri)

    first = commit_append_plain(
        plain_dataset_uri,
        writer_a,
        [{"id": 2, "name": "append-a"}],
        max_retries=0,
    )
    assert first.version == 2

    with pytest.raises(Exception) as exc_info:
        commit_append_plain(
            plain_dataset_uri,
            writer_b,
            [{"id": 3, "name": "append-b"}],
            max_retries=0,
        )

    rows = latest_rows_plain(plain_dataset_uri)
    names = {row["name"] for row in rows}
    assert names == {"base", "append-a"}
    assert str(exc_info.value)


def test_stale_append_then_append_is_not_blocked_with_namespace_default_retries(
    managed_ns, temp_root, table_id
):
    create_base_table(managed_ns, table_id)

    writer_a = lance.dataset(namespace_client=managed_ns, table_id=table_id)
    writer_b = lance.dataset(namespace_client=managed_ns, table_id=table_id)

    first = commit_append(
        managed_ns,
        table_id,
        writer_a,
        [{"id": 2, "name": "append-a"}],
    )
    second = commit_append(
        managed_ns,
        table_id,
        writer_b,
        [{"id": 3, "name": "append-b"}],
    )

    rows = latest_rows(managed_ns, table_id)
    names = {row["name"] for row in rows}

    assert writer_a.version == 1
    assert writer_b.version == 1
    assert first.version == 2
    assert second.version == 3
    assert names == {"base", "append-a", "append-b"}

    metrics = managed_ns.retrieve_ops_metrics() or {}
    assert metrics.get("create_table_version", 0) >= 3


def test_with_namespace_same_target_version_duplicate_publish_is_blocked(
    managed_ns, temp_root, table_id
):
    create_base_table(managed_ns, table_id)

    base_manifest = latest_final_manifest_path(temp_root)
    staging1_abs, staging1_rel = copy_as_staging(temp_root, base_manifest, target_version=2)
    staging2_abs, staging2_rel = copy_as_staging(temp_root, base_manifest, target_version=2)

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

    managed_ns.create_table_version(req1)

    with pytest.raises(Exception) as exc_info:
        managed_ns.create_table_version(req2)

    msg = str(exc_info.value)
    assert (
        "already exists" in msg
        or "Concurrent" in msg
        or "Version 2" in msg
    ), msg

    metrics = managed_ns.retrieve_ops_metrics() or {}
    assert metrics.get("create_table_version", 0) >= 3
