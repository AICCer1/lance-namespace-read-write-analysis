# create / update / index 怎么和 namespace 结合

## 版本范围

- `pylance` / `lance`：`v6.0.0`
- `lance-namespace`：`v0.7.6`

---

## 1. 先回答你这个问题

### 现在仓库里有没有 read / write？

有了。

目前仓库已经覆盖：

- **read**：`lance.dataset(namespace_client=..., table_id=...)`
- **write**：
  - `write_dataset(..., namespace_client=..., table_id=...)`
  - `write_fragments(...) + LanceDataset.commit(...)`
  - `managed_versioning` 的传递链路

但你现在追问的这块——

- `create`
- `update`
- `index`

确实是另一层问题：

> **这些操作到底应该走 namespace 原生 API，还是先通过 namespace 打开 dataset，再走 Lance dataset API？**

这个才是关键。

---

## 2. 最实用的心智模型

把它分成两大类最清楚。

### 路线 A：namespace 原生 API

也就是直接按 `table_id` 操作，不先手动打开 dataset。

典型形式：

- `ns.create_table(...)`
- `ns.insert_into_table(...)`
- `ns.merge_insert_into_table(...)`
- `ns.update_table(...)`
- `ns.delete_from_table(...)`
- `ns.create_table_index(...)`

这类 API 的特点是：

- 输入核心是 `table_id`
- 由 namespace 自己去解析表位置
- 更像 RPC / 服务端接口
- 对 `RestNamespace` 特别自然

---

### 路线 B：先通过 namespace 打开 dataset，再走 Lance dataset API

也就是先：

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
```

然后再：

- `ds.update(...)`
- `ds.delete(...)`
- `ds.merge_insert(...).execute(...)`
- `ds.create_index(...)`

这类 API 的特点是：

- 先用 namespace 做表定位与存储参数解析
- 后续操作直接落到 dataset 对象上
- 更像 Lance Python 的“本地使用方式”

---

## 3. create 相关：怎么结合 namespace

`create` 其实有 **3 条路**。

## 3.1 路 1：高层 `write_dataset(..., mode="create")`

这是最像 Lance Python 正常用法的一条。

```python
import lance
import lance.namespace
import pyarrow as pa

ns = lance.namespace.DirectoryNamespace(
    root="memory://demo",
    table_version_tracking_enabled="true",
    manifest_enabled="true",
)

table_id = ["workspace", "events"]

table = pa.Table.from_pylist(
    [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
    ]
)

ds = lance.write_dataset(
    table,
    namespace_client=ns,
    table_id=table_id,
    mode="create",
)
```

### 这条路内部做了什么？

- 调 `declare_table(...)`
- 从响应里拿：
  - `location`
  - `storage_options`
  - `managed_versioning`
- 再进入 Lance 自己的写入与 commit 路径

所以它适合：

- 你想继续站在 Lance dataset 视角写代码
- 你想吃到 `managed_versioning` 自动透传
- 你后面还要继续用 `lance.dataset(...)` / `ds.update(...)` 这套用法

---

## 3.2 路 2：namespace 原生 `create_table(...)`

这是“直接按 `table_id` 创建表”的方式。

```python
from lance_namespace import CreateTableRequest

create_req = CreateTableRequest(id=["workspace", "events"])
ipc_bytes = table_to_ipc_bytes(table)  # 需要 Arrow IPC bytes
resp = ns.create_table(create_req, ipc_bytes)
```

### 这条路的特点

- 你直接跟 namespace 说：给我创建这张表
- 数据以 Arrow IPC bytes 传进去
- 更像服务端 API / catalog API

它适合：

- 你做的是 REST / 服务端式集成
- 你更关注“按表 ID 发命令”
- 你不想先自己走 Lance dataset 写入流程

---

## 3.3 路 3：`declare_table(...) + write_fragments(...) + commit(...)`

这是低层、最接近分布式写协议的一条路。

```python
resp = ns.declare_table(...)
table_uri = resp.location
managed = resp.managed_versioning is True

fragments = write_fragments(...)

lance.LanceDataset.commit(
    table_uri,
    op,
    namespace_client=ns,
    table_id=table_id,
    namespace_client_managed_versioning=managed,
)
```

这条路适合：

- 你要自己控制 coordinator / worker 分工
- 你要先产出 fragment，再统一提交
- 你在做分布式写协议层

---

## 3.4 create 这三条路怎么选？

### 如果你想要“最像正常 Lance 用法”
选：

- `write_dataset(..., namespace_client=..., table_id=..., mode="create")`

### 如果你想要“纯 namespace / 纯 table_id 风格”
选：

- `ns.create_table(...)`

### 如果你在做“分布式写协议 / worker 产物回收”
选：

- `declare_table + write_fragments + commit`

---

## 4. update 相关：怎么结合 namespace

update 这块要稍微小心，因为 **“namespace 规范里有”** 和 **“某个 backend 真正实现得完整”** 不是一回事。

## 4.1 路 1：先打开 dataset，再 `ds.update(...)`

这是我更推荐的默认做法。

```python
import lance

ns = ...
table_id = ["workspace", "events"]

ds = lance.dataset(namespace_client=ns, table_id=table_id)
ds.update(
    {"score": "score + 1"},
    where="is_vip = true",
)
```

### 这条路的好处

- 思维最统一
- 先用 namespace 解决表定位、storage options、managed versioning
- 后续 mutation 直接落到 dataset 对象
- 和 `delete`、`merge_insert`、`create_index` 的用法统一

### 同类操作

```python
ds.delete("expired = true")

ds.merge_insert("id") \
  .when_matched_update_all() \
  .when_not_matched_insert_all() \
  .execute(new_data)
```

这条路线非常适合：

- 你主要站在 Lance Python SDK 的角度开发
- 你想让“namespace 只负责定位与上下文，dataset 负责实际操作”

---

## 4.2 路 2：namespace 原生 `insert_into_table(...)`

这个在 `DirectoryNamespace` 里是明确实现了的。

```python
from lance_namespace import InsertIntoTableRequest

insert_req = InsertIntoTableRequest(
    id=["workspace", "events"],
    mode="append",
)
resp = ns.insert_into_table(insert_req, ipc_bytes)
```

### 这条路的特点

- 完全按 `table_id` 操作
- 不需要你手动先 open dataset
- 适合 REST / RPC 风格

对应 overwrite 也能走：

```python
InsertIntoTableRequest(id=table_id, mode="overwrite")
```

---

## 4.3 路 3：namespace 原生 `merge_insert_into_table(...)`

这个也是明确有实现的。

```python
from lance_namespace import MergeInsertIntoTableRequest

merge_req = MergeInsertIntoTableRequest(
    id=["workspace", "events"],
    on="id",
    when_matched_update_all=True,
    when_not_matched_insert_all=True,
)
resp = ns.merge_insert_into_table(merge_req, ipc_bytes)
```

这条路适合：

- 你希望把 upsert 语义直接交给 namespace backend / 服务端
- 你不想自己先 open dataset 再走 builder

---

## 4.4 `update_table(...)` 和 `delete_from_table(...)` 呢？

namespace 规范里是有这两个接口的：

- `update_table(...)`
- `delete_from_table(...)`

但从 `v6.0.0` 这份源码看：

- `RestNamespace` 客户端有这些 API 入口
- `DirectoryNamespace` 我能明确看到：
  - `insert_into_table(...)` 有实现
  - `merge_insert_into_table(...)` 有实现
  - `create_table_index(...)` 有实现
- 但 **我没有在 `DirectoryNamespace` 里看到原生 `update_table(...)` / `delete_from_table(...)` 的实现**

所以这里最稳的建议是：

> **如果你用的是 `DirectoryNamespace`，行级 update / delete 优先走 `ds.update(...)` / `ds.delete(...)`。**

也就是说：

### 对 `DirectoryNamespace`
- append / overwrite：`write_dataset(...)` 或 `ns.insert_into_table(...)`
- upsert：`ds.merge_insert(...)` 或 `ns.merge_insert_into_table(...)`
- update / delete：优先 `ds.update(...)` / `ds.delete(...)`

### 对 `RestNamespace`
- 如果服务端实现完整，namespace 原生 API 路子会更自然
- 但如果你想统一客户端写法，也可以先 `lance.dataset(namespace_client=..., table_id=...)` 再做 dataset 操作

---

## 5. index 相关：怎么结合 namespace

index 也有两条路。

## 5.1 路 1：先打开 dataset，再 `ds.create_index(...)`

这是最自然的一条。

```python
import lance

ns = ...
table_id = ["workspace", "docs"]

ds = lance.dataset(namespace_client=ns, table_id=table_id)

# 标量索引
ds.create_index("id", "BTREE", name="id_idx")

# 向量索引
ds.create_index(
    "vector",
    index_type="IVF_FLAT",
    name="vector_idx",
    metric="L2",
)
```

### 这条路的好处

- 跟 Lance 原生用法完全一致
- 只是 dataset 的来源换成了 namespace
- 对用户最顺手

如果你已经这样打开了：

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
```

那后续 `create_index(...)` 基本就可以把它当成“一个已经被 namespace 解析过的 dataset”来用。

---

## 5.2 路 2：namespace 原生 `create_table_index(...)`

```python
from lance_namespace import CreateTableIndexRequest

req = CreateTableIndexRequest(
    id=["workspace", "docs"],
    column="id",
    index_type="BTREE",
    name="id_idx",
)
resp = ns.create_table_index(req)
```

向量索引也一样：

```python
req = CreateTableIndexRequest(
    id=["workspace", "docs"],
    column="vector",
    index_type="IVF_FLAT",
    name="vector_idx",
    distance_type="l2",
)
resp = ns.create_table_index(req)
```

### 这条路适合什么？

- 你想完全按 `table_id` 发命令
- 你在用 namespace 当服务端入口
- 你不想在客户端显式打开 dataset

---

## 5.3 index 怎么选？

### 如果你主要用 Lance Python SDK
选：

- `ds = lance.dataset(namespace_client=..., table_id=...)`
- 然后 `ds.create_index(...)`

### 如果你主要用 namespace 作为服务端接口
选：

- `ns.create_table_index(...)`

---

## 6. 一个最好记的总规则

如果你懒得记太多，记这个就够了：

### create
- 高层 Lance 风格：`write_dataset(..., namespace_client=..., table_id=..., mode="create")`
- namespace 原生命令风格：`ns.create_table(...)`
- 分布式低层风格：`declare_table + write_fragments + commit`

### update / delete / merge
- 最稳通用：先 `lance.dataset(namespace_client=..., table_id=...)`
- 然后：
  - `ds.update(...)`
  - `ds.delete(...)`
  - `ds.merge_insert(...).execute(...)`
- 如果 backend 支持 namespace 原生 mutation，也可以走：
  - `ns.insert_into_table(...)`
  - `ns.merge_insert_into_table(...)`
  - `ns.update_table(...)`
  - `ns.delete_from_table(...)`

### index
- 最自然：`ds.create_index(...)`
- 服务端 / RPC 风格：`ns.create_table_index(...)`

---

## 7. 我个人建议的默认选型

如果你现在的目标是：

- 把 namespace 接进 Lance Python 正常开发流
- 同时保留 managed_versioning / storage_options / table_id 这套上下文

我建议默认这么选：

### 默认推荐

#### create
```python
lance.write_dataset(..., namespace_client=ns, table_id=table_id, mode="create")
```

#### read
```python
lance.dataset(namespace_client=ns, table_id=table_id)
```

#### append / overwrite
```python
lance.write_dataset(..., namespace_client=ns, table_id=table_id, mode="append")
```
或
```python
lance.write_dataset(..., namespace_client=ns, table_id=table_id, mode="overwrite")
```

#### update / delete / upsert
```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
ds.update(...)
ds.delete(...)
ds.merge_insert(...).execute(...)
```

#### index
```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
ds.create_index(...)
```

### 什么时候改用 namespace 原生 API？

当你满足下面任一条时：

- 你在做 REST / catalog / control plane 风格集成
- 你希望所有操作都只围绕 `table_id`
- 你不想在客户端显式 open dataset
- 你想把 mutation / index 创建尽量下沉到服务端后端

---

## 8. 跟 managed_versioning 的关系

这几类操作里，和 `managed_versioning` 关系最紧的是：

- `write_dataset(..., namespace_client=..., table_id=...)`
- `lance.dataset(namespace_client=..., table_id=...)`
- `write_fragments + commit`

原因是这几条路里，Python / Rust 明确会把 namespace 返回的：

- `location`
- `storage_options`
- `managed_versioning`

继续往后传。

而 namespace 原生 API（例如 `ns.create_table_index(...)`、`ns.insert_into_table(...)`）则更像：

> **直接把整个动作交给 namespace backend 自己做。**

这时“managed_versioning 怎么传递”这个问题，更多是在 backend 内部消化，而不是由你在 Python 客户端手动透传。

---

## 9. 最后一句话总结

如果你从 **Lance SDK 视角** 出发：

> **先用 namespace 打开 / 定位，再在 dataset 上做 update / delete / index，通常最顺手。**

如果你从 **namespace 服务端接口视角** 出发：

> **直接用 `ns.create_table(...)`、`ns.insert_into_table(...)`、`ns.merge_insert_into_table(...)`、`ns.create_table_index(...)` 这类 table-id API，更像 control plane / RPC。**

而 `create` 是最特殊的，因为它既可以走：

- `write_dataset(mode="create")`
- `ns.create_table(...)`
- `declare_table + write_fragments + commit`

也就是：

> **create 这块不是只有一条路，而是要看你站在高层写接口、namespace 原生接口，还是分布式低层协议层。**

---

## 10. 关键源码定位

- `/root/.openclaw/workspace/_lance_src_v6.0.0/python/python/lance/__init__.py`
  - `lance.dataset(namespace_client=..., table_id=...)`
- `/root/.openclaw/workspace/_lance_src_v6.0.0/python/python/lance/dataset.py`
  - `write_dataset(...)`
  - `LanceDataset.update(...)`
  - `LanceDataset.delete(...)`
  - `LanceDataset.merge_insert(...)`
  - `LanceDataset.create_index(...)`
- `/root/.openclaw/workspace/_lance_src_v6.0.0/python/python/lance/namespace.py`
  - `create_table(...)`
  - `insert_into_table(...)`
  - `merge_insert_into_table(...)`
  - `update_table(...)`
  - `delete_from_table(...)`
  - `create_table_index(...)`
- `/root/.openclaw/workspace/_lance_src_v6.0.0/python/python/tests/test_namespace_dir.py`
  - `create_table` / `insert_into_table` / `create_table_index` 的测试
- `/root/.openclaw/workspace/_lance_src_v6.0.0/rust/lance-namespace-impls/src/dir.rs`
  - `create_table_index(...)`
  - `insert_into_table(...)`
  - `merge_insert_into_table(...)`
- `/root/.openclaw/workspace/_lance_namespace_src_v0.7.6/python/lance_namespace_urllib3_client/docs/`
  - `CreateTableIndexRequest.md`
  - `InsertIntoTableRequest.md`
  - `MergeInsertIntoTableRequest.md`
  - `UpdateTableRequest.md`
  - `DeleteFromTableRequest.md`
