import uuid

import lance
import lance.namespace
import pyarrow as pa
from lance.fragment import write_fragments
from lance_namespace import DescribeTableRequest


def make_namespace(case_name: str):
    return lance.namespace.DirectoryNamespace(
        root=f"memory://dir-ns-conflict-{case_name}-{uuid.uuid4().hex[:8]}",
        table_version_tracking_enabled="true",
        manifest_enabled="true",
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


def commit_overwrite(ns, table_id, stale_ds, rows, max_retries=20):
    table_uri, storage_options, managed = table_context(ns, table_id)
    op = lance.LanceOperation.Overwrite(stale_ds.schema, make_fragments(ns, table_id, rows))
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


def commit_restore(ns, table_id, stale_ds, restore_to_version=1, max_retries=20):
    table_uri, storage_options, managed = table_context(ns, table_id)
    op = lance.LanceOperation.Restore(restore_to_version)
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


def latest_table(ns, table_id):
    return lance.dataset(namespace_client=ns, table_id=table_id).to_table().to_pylist()


def run_case(title, second_commit):
    print(f"\n=== {title} ===")
    ns, table_id, first_commit, later_commit = second_commit()

    first_result = first_commit()
    print("first commit version:", first_result.version)

    try:
        second_result = later_commit()
        print("second commit version:", second_result.version)
    except Exception as exc:
        print("second commit failed:", type(exc).__name__)
        print(str(exc))

    print("latest rows:", latest_table(ns, table_id))


def case_append_then_append():
    ns = make_namespace("append_then_append")
    table_id = ["events"]
    create_base_table(ns, table_id)

    writer_a = lance.dataset(namespace_client=ns, table_id=table_id)
    writer_b = lance.dataset(namespace_client=ns, table_id=table_id)

    return (
        ns,
        table_id,
        lambda: commit_append(ns, table_id, writer_a, [{"id": 2, "name": "append-a"}]),
        lambda: commit_append(ns, table_id, writer_b, [{"id": 3, "name": "append-b"}]),
    )


def case_overwrite_then_append():
    ns = make_namespace("overwrite_then_append")
    table_id = ["events"]
    create_base_table(ns, table_id)

    writer_overwrite = lance.dataset(namespace_client=ns, table_id=table_id)
    writer_append = lance.dataset(namespace_client=ns, table_id=table_id)

    return (
        ns,
        table_id,
        lambda: commit_overwrite(
            ns,
            table_id,
            writer_overwrite,
            [{"id": 100, "name": "overwrite-first"}],
        ),
        lambda: commit_append(
            ns,
            table_id,
            writer_append,
            [{"id": 2, "name": "append-late"}],
        ),
    )


def case_append_then_overwrite():
    ns = make_namespace("append_then_overwrite")
    table_id = ["events"]
    create_base_table(ns, table_id)

    writer_append = lance.dataset(namespace_client=ns, table_id=table_id)
    writer_overwrite = lance.dataset(namespace_client=ns, table_id=table_id)

    return (
        ns,
        table_id,
        lambda: commit_append(
            ns,
            table_id,
            writer_append,
            [{"id": 2, "name": "append-first"}],
        ),
        lambda: commit_overwrite(
            ns,
            table_id,
            writer_overwrite,
            [{"id": 200, "name": "overwrite-late"}],
        ),
    )


def case_restore_then_append():
    ns = make_namespace("restore_then_append")
    table_id = ["events"]
    create_base_table(ns, table_id)

    # 先造一个 version=2，后面 restore 到 version=1 才有意义。
    lance.write_dataset(
        pa.Table.from_pylist([{"id": 2, "name": "warmup"}]),
        namespace_client=ns,
        table_id=table_id,
        mode="append",
    )

    writer_restore = lance.dataset(namespace_client=ns, table_id=table_id)
    writer_append = lance.dataset(namespace_client=ns, table_id=table_id)

    return (
        ns,
        table_id,
        lambda: commit_restore(ns, table_id, writer_restore, restore_to_version=1),
        lambda: commit_append(
            ns,
            table_id,
            writer_append,
            [{"id": 3, "name": "append-late"}],
        ),
    )


def case_append_then_restore():
    ns = make_namespace("append_then_restore")
    table_id = ["events"]
    create_base_table(ns, table_id)

    # 先造一个 version=2，保证 restore(1) 是真正的“回到旧版本”。
    lance.write_dataset(
        pa.Table.from_pylist([{"id": 2, "name": "warmup"}]),
        namespace_client=ns,
        table_id=table_id,
        mode="append",
    )

    writer_append = lance.dataset(namespace_client=ns, table_id=table_id)
    writer_restore = lance.dataset(namespace_client=ns, table_id=table_id)

    return (
        ns,
        table_id,
        lambda: commit_append(
            ns,
            table_id,
            writer_append,
            [{"id": 3, "name": "append-first"}],
        ),
        lambda: commit_restore(ns, table_id, writer_restore, restore_to_version=1),
    )


if __name__ == "__main__":
    # 这个脚本不是在测“底层真并发调度”，而是在测更稳定的语义：
    # 两个 stale writer 从同一个 base version 出发时，后提交操作会不会被接受。
    #
    # 预期：
    # - append -> append：第二个通常也成功
    # - overwrite -> append：第二个 append 失败
    # - append -> overwrite：第二个 overwrite 可成功
    # - restore -> append：第二个 append 失败
    # - append -> restore：第二个 restore 可成功

    run_case("append -> append", case_append_then_append)
    run_case("overwrite -> append", case_overwrite_then_append)
    run_case("append -> overwrite", case_append_then_overwrite)
    run_case("restore -> append", case_restore_then_append)
    run_case("append -> restore", case_append_then_restore)
