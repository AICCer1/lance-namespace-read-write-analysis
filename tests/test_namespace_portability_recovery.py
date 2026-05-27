import os
import re
import shutil
import tempfile

import lance
import lance.namespace
import pyarrow as pa
from lance.fragment import write_fragments
from lance_namespace import DescribeTableRequest


FINAL_MANIFEST_RE = re.compile(r"^\d+\.manifest$")
STAGING_MANIFEST_RE = re.compile(r"^\d+\.manifest-[0-9a-f]+$")


def make_namespace(root: str):
    return lance.namespace.DirectoryNamespace(
        root=root,
        table_version_tracking_enabled="true",
        manifest_enabled="true",
        ops_metrics_enabled="true",
    )


def create_base_table(ns, table_id):
    return lance.write_dataset(
        pa.Table.from_pylist(
            [
                {"id": 1, "name": "base"},
                {"id": 2, "name": "warmup"},
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


def append_with_low_level_commit(ns, table_id, rows):
    table_uri, storage_options, managed = table_context(ns, table_id)
    base = lance.dataset(namespace_client=ns, table_id=table_id)
    frags = write_fragments(
        pa.Table.from_pylist(rows),
        table_uri,
        storage_options=storage_options,
        namespace_client=ns,
        table_id=table_id,
    )
    op = lance.LanceOperation.Append(frags)
    return lance.LanceDataset.commit(
        table_uri,
        op,
        read_version=base.version,
        storage_options=storage_options,
        namespace_client=ns,
        table_id=table_id,
        namespace_client_managed_versioning=managed,
    )


def rows_plain(uri: str):
    return lance.dataset(uri).to_table().to_pylist()


def rows_namespace(ns, table_id):
    return lance.dataset(namespace_client=ns, table_id=table_id).to_table().to_pylist()


def versions_dir_from_table_uri(table_uri: str) -> str:
    return os.path.join(table_uri, "_versions")


def final_and_staging_manifests(table_uri: str):
    vdir = versions_dir_from_table_uri(table_uri)
    finals = []
    stagings = []
    for name in os.listdir(vdir):
        if FINAL_MANIFEST_RE.match(name):
            finals.append(name)
        elif STAGING_MANIFEST_RE.match(name):
            stagings.append(name)
    finals.sort(key=lambda n: int(n.split(".")[0]))
    return finals, stagings


def test_namespace_commit_finalizes_to_object_store_and_plain_reader_can_open_latest():
    with tempfile.TemporaryDirectory(prefix="ns-portability-") as root:
        ns = make_namespace(root)
        table_id = ["events"]

        create_base_table(ns, table_id)
        new_ds = append_with_low_level_commit(
            ns,
            table_id,
            [
                {"id": 3, "name": "append-a"},
                {"id": 4, "name": "append-b"},
            ],
        )

        table_uri, _, _ = table_context(ns, table_id)

        ns_rows = rows_namespace(ns, table_id)
        plain_rows = rows_plain(table_uri)

        assert new_ds.version == 2
        assert ns_rows == plain_rows
        assert {row["name"] for row in plain_rows} == {
            "base",
            "warmup",
            "append-a",
            "append-b",
        }

        finals, stagings = final_and_staging_manifests(table_uri)
        assert any(name.startswith("2.") for name in finals), finals
        assert not stagings, stagings


def test_dataset_directory_remains_portable_after_namespace_managed_commit():
    with tempfile.TemporaryDirectory(prefix="ns-portability-src-") as root:
        ns = make_namespace(root)
        table_id = ["events"]

        create_base_table(ns, table_id)
        append_with_low_level_commit(
            ns,
            table_id,
            [
                {"id": 3, "name": "append-a"},
                {"id": 4, "name": "append-b"},
            ],
        )

        table_uri, _, _ = table_context(ns, table_id)
        expected_rows = rows_plain(table_uri)

        with tempfile.TemporaryDirectory(prefix="ns-portability-dst-") as dst_root:
            copied_uri = os.path.join(dst_root, "copied-events.lance")
            shutil.copytree(table_uri, copied_uri)

            copied_rows = rows_plain(copied_uri)
            assert copied_rows == expected_rows
            assert {row["name"] for row in copied_rows} == {
                "base",
                "warmup",
                "append-a",
                "append-b",
            }
