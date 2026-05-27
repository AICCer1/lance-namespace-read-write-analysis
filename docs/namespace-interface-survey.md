# Lance 接口接入 namespace 的总盘点

## 版本范围

- `pylance` / `lance`：`v6.0.0`
- `lance-namespace`：`v0.7.6`

---

## 1. 先给结论

如果你的项目原来是“直接拿 `uri` 调 Lance API”，现在要整体改成 namespace 接入，最稳的思路不是“到处改成 namespace 原生 RPC”，而是分成两层：

### 主路线：dataset 路线（推荐）

也就是：

1. 先通过 namespace 把 `table_id` 解析成表
2. 打开成 `LanceDataset`
3. 后续绝大多数读/改/索引操作，继续走 Lance 自己的 dataset API

典型形式：

```python
import lance

ns = ...
table_id = ["workspace", "events"]

ds = lance.dataset(namespace_client=ns, table_id=table_id)

# 后续还是正常 Lance 用法
ds.to_table()
ds.update({"score": "score + 1"}, where="tier = 'vip'")
ds.delete("is_deleted = true")
ds.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(data)
ds.create_index("id", "BTREE", name="id_idx")
```

这是你项目改造的**主路径**。

---

### 辅路线：少数“直接支持 namespace 参数”的低层接口

有一些 Lance 入口本身就直接支持：

- `namespace_client=...`
- `table_id=...`

这些主要用在：

- open dataset
- 高层写入
- 低层 fragment 写入
- 低层 commit
- file reader / writer / session
- TensorFlow `from_lance(...)`

---

### 另一条路线：namespace 原生 API

也就是直接调：

- `ns.create_table(...)`
- `ns.insert_into_table(...)`
- `ns.merge_insert_into_table(...)`
- `ns.create_table_index(...)`
- `ns.update_table(...)`
- `ns.delete_from_table(...)`

这条路线更像：

- `table_id` 风格控制面 API
- REST / RPC / catalog 风格接入
- 服务端统一封装

对你现在这个“把原来没有 namespace 的 Lance 项目整体改造上 namespace”的任务来说，**它不是主路线，更像补充路线**。

---

## 2. 我建议的总分类

把“能接 namespace 的接口”分成 3 类最清楚：

### A. 直接支持 `namespace_client/table_id` 的 Lance 接口

也就是你不需要先自己手动 `describe_table(...)` 再拼 URI，它们自己就能接 namespace 上下文。

### B. 先通过 namespace 打开 dataset，再继续调用的 dataset API

也就是只有入口那一步变了：

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
```

后续大部分 dataset 方法还是原样用。

### C. namespace 原生 API

也就是直接操作 `table_id` 的 namespace 接口，不先显式打开 LanceDataset。

---

## 3. A 类：直接支持 `namespace_client/table_id` 的 Lance 接口

这一类是最值得你在项目里优先盘点和替换的，因为它们是“原来用 uri，现在改成 namespace”最直接的位置。

---

## 3.1 `lance.dataset(...)`

源码位置：

- `python/python/lance/__init__.py:89`

它支持：

```python
lance.dataset(
    namespace_client=ns,
    table_id=["workspace", "events"],
)
```

### 它内部做什么

- 调 `describe_table(...)`
- 从 namespace 响应里取：
  - `location`
  - `storage_options`
  - `managed_versioning`
- 再构造 `LanceDataset`

### 它适合什么

- 所有 read 路径
- 所有“先打开表，再在 dataset 上继续做事”的路径
- 也是你项目整体改造的**第一入口**

---

## 3.2 `lance.write_dataset(...)`

源码位置：

- `python/python/lance/dataset.py:6403`

它支持：

```python
lance.write_dataset(
    data,
    namespace_client=ns,
    table_id=["workspace", "events"],
    mode="create",   # 或 append / overwrite
)
```

### 它内部做什么

- `mode="create"` 时：调用 `namespace.declare_table(...)`
- `mode in ("append", "overwrite")` 时：调用 `namespace.describe_table(...)`
- 取回：
  - `location`
  - `storage_options`
  - `managed_versioning`
- 再进入 Lance 正常写入流程

### 它适合什么

- create / append / overwrite 的高层写入
- 原项目里原本是 `write_dataset(data, uri, ...)` 的场景

### 改造方式

把：

```python
lance.write_dataset(data, uri, mode="append")
```

改成：

```python
lance.write_dataset(data, namespace_client=ns, table_id=table_id, mode="append")
```

---

## 3.3 `lance.write_fragments(...)`

源码位置：

- `python/python/lance/fragment.py:1036`

它支持：

```python
from lance.fragment import write_fragments

fragments = write_fragments(
    data,
    table_uri,
    schema=schema,
    mode="append",
    storage_options=storage_options,
    namespace_client=ns,
    table_id=table_id,
)
```

### 它适合什么

- 分布式写
- worker 只负责产 fragment
- CN 最后统一 commit

### 但要注意

它**不是**“纯 `table_id` 即可”的高层 API。

它仍然需要：

- `dataset_uri` / `table_uri`

也就是说通常还是要先：

- `declare_table(...)` 或 `describe_table(...)`
- 从响应里拿 `location`
- 再把 `location` 传给 `write_fragments(...)`

### 你项目里的典型接法

- CN：先经 namespace 解析表位置
- DN：拿到 `table_uri + storage_options + table_id` 后写 fragments
- CN：收集 fragments，最后统一 commit

---

## 3.4 `lance.fragment.LanceFragment.create(...)`

源码位置：

- `python/python/lance/fragment.py:337`

它支持：

```python
from lance.fragment import LanceFragment

frag = LanceFragment.create(
    table_uri,
    data,
    mode="append",
    storage_options=storage_options,
    namespace_client=ns,
    table_id=table_id,
)
```

### 它适合什么

- 比 `write_fragments(...)` 更原始的 fragment 级写入
- 你自己手动组织 `LanceOperation.Append / Overwrite`

### 什么时候用

- 只有在你项目本来就已经在做很低层的 fragment 协议时才值得碰
- 否则一般优先 `write_fragments(...)`

---

## 3.5 `lance.LanceDataset.commit(...)`

源码位置：

- `python/python/lance/dataset.py:3951`

它支持：

```python
lance.LanceDataset.commit(
    table_uri,
    operation,
    read_version=read_version,
    storage_options=storage_options,
    namespace_client=ns,
    table_id=table_id,
    namespace_client_managed_versioning=managed,
)
```

### 它适合什么

- 低层 commit
- 分布式写统一发布
- 自己构造 `LanceOperation.*`
- 自己拿 `Transaction` 做提交

### 这是你项目低层改造里最关键的一个接口

因为凡是：

- `write_fragments(...)`
- `LanceFragment.create(...)`
- `fragment.delete(...)`
- distributed index build

这些最后往往都要收敛到 `LanceDataset.commit(...)`。

---

## 3.6 `lance.file.LanceFileReader`

源码位置：

- `python/python/lance/file.py:57`

它支持：

```python
from lance.file import LanceFileReader

reader = LanceFileReader(
    file_path,
    storage_options=storage_options,
    namespace_client=ns,
    table_id=table_id,
)
```

### 用途

- 低层直接读 `.lance` 数据文件
- 自动 credential refresh

这不是 dataset 级读表接口，而是**文件级接口**。

---

## 3.7 `lance.file.LanceFileWriter`

源码位置：

- `python/python/lance/file.py:378`

它支持：

```python
from lance.file import LanceFileWriter

writer = LanceFileWriter(
    file_path,
    schema=schema,
    storage_options=storage_options,
    namespace_client=ns,
    table_id=table_id,
)
```

### 用途

- 低层直接写 Lance 文件
- 自动 credential refresh

同样，它是**文件级写入**，不是 dataset 级事务提交。

---

## 3.8 `lance.file.LanceFileSession`

源码位置：

- `python/python/lance/file.py:210`

它支持：

```python
from lance.file import LanceFileSession

session = LanceFileSession(
    base_path=table_uri,
    storage_options=storage_options,
    namespace_client=ns,
    table_id=table_id,
)
```

### 用途

- 一次创建 session
- 后续重复开多个 reader / writer
- 适合低层文件操作比较多的场景

---

## 3.9 `LanceDataset.new_file_session()`

源码位置：

- `python/python/lance/dataset.py:2651`

它不需要你再手动传 namespace 参数。

如果 dataset 本身就是这样打开的：

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
```

那么：

```python
file_sess = ds.new_file_session()
```

会自动继承：

- `self._namespace_client`
- `self._table_id`
- `self.latest_storage_options()`

### 这点很适合你的项目

如果你某些逻辑已经先拿到了 `ds`，后面又要做低层文件读写，那优先从 `ds.new_file_session()` 往下走，比重新拼参数更稳。

---

## 3.10 `lance.tf.data.from_lance(...)`

源码位置：

- `python/python/lance/tf/data.py:136`

它支持：

```python
import lance.tf.data

train_ds = lance.tf.data.from_lance(
    namespace_client=ns,
    table_id=table_id,
)
```

### 它内部做什么

如果传入的不是已经打开的 `LanceDataset`，它内部会先调用：

```python
lance.dataset(
    namespace_client=namespace_client,
    table_id=table_id,
)
```

### 适合什么

- 训练 / 推理数据管道
- 原来用 `from_lance(uri=...)` 的 TensorFlow 场景

---

## 4. B 类：先通过 namespace 打开 dataset，再继续调用的 Lance dataset API

这一类其实才是最重要的。

因为你项目里绝大多数原本已经写好的 Lance 逻辑，很可能都属于：

- 先拿一个 `ds`
- 再对 `ds` 做各种操作

对这种代码，最小改造往往不是把后续所有逻辑推翻重写，而是把**入口替换成 namespace-aware open**。

---

## 4.1 读路径：基本都可以沿用

先：

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
```

后面这些 read API，通常都沿用原写法：

- `ds.to_table()`
- `ds.to_batches()`
- `ds.scanner(...)`
- `ds.count_rows()`
- `ds.take(...)`
- `ds.search(...)`
- `ds.schema`
- `ds.get_fragments()`

也就是说：

> **read 侧最主要的 namespace 改造点，其实就是入口从 uri-open 改成 namespace-open。**

---

## 4.2 行级 mutation：`update / delete / merge_insert`

源码位置：

- `update`：`python/python/lance/dataset.py:2531`
- `delete`：`python/python/lance/dataset.py:2333`
- `merge_insert`：`python/python/lance/dataset.py:2415`

推荐接法：

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)

ds.update({"score": "score + 1"}, where="tier = 'vip'")
ds.delete("is_deleted = true")

ds.merge_insert("id") \
  .when_matched_update_all() \
  .when_not_matched_insert_all() \
  .execute(incoming)
```

### 这类接口为什么推荐走 dataset 路线

因为它们本来就是 Lance 的 dataset 语义：

- 代码风格统一
- 你的原项目改动最小
- 不需要把业务逻辑全部换成 namespace 原生 RPC 风格

### 对 `DirectoryNamespace` 的额外提醒

虽然 `namespace.py` 暴露了：

- `update_table(...)`
- `delete_from_table(...)`

但从 `rust/lance-namespace-impls/src/dir.rs` 这份源码能明确看到的 native 实现，主要是：

- `insert_into_table(...)`
- `merge_insert_into_table(...)`
- `create_table_index(...)`
- `update_table_schema_metadata(...)`

**没有清晰看到 `update_table / delete_from_table` 对应实现。**

所以对 `DirectoryNamespace`，更稳的建议仍然是：

- `update` → `ds.update(...)`
- `delete` → `ds.delete(...)`

---

## 4.3 索引：`create_index` 及其分布式低层变体

### 高层索引

源码位置：

- `python/python/lance/dataset.py:3483`

推荐接法：

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
ds.create_index("id", "BTREE", name="id_idx")
```

这是最自然的主路线。

---

### 分布式 vector index 低层接口

源码位置：

- `create_index_uncommitted`：`python/python/lance/dataset.py:3728`
- `create_index_segment_builder`：`python/python/lance/dataset.py:3896`
- `merge_existing_index_segments`：`python/python/lance/dataset.py:3907`
- `commit_existing_index_segments`：`python/python/lance/dataset.py:3913`

这条链路适合：

- worker 分别产 index segment
- coordinator 汇总 segment
- 最后统一 publish index

典型思路：

1. `ds = lance.dataset(namespace_client=ns, table_id=table_id)`
2. worker 调 `ds.create_index_uncommitted(...)`
3. 汇总 segment metadata
4. coordinator 用 `create_index_segment_builder / merge_existing_index_segments`
5. 最后 `ds.commit_existing_index_segments(...)`

### 这里的关键点

这些接口**不要求你再显式传 `namespace_client/table_id`**，因为它们是**实例方法**，本质上是在已经打开的 dataset 上继续操作。

所以它们天然适合你现在定下来的 **路线 B：namespace-aware dataset integration**。

---

### 分布式 scalar index 元数据合并

源码位置：

- `merge_index_metadata`：`python/python/lance/dataset.py:3846`

它的语义是：

- 先合并临时 scalar index 输出
- **但它本身不 commit**
- 后面仍要显式提交 index manifest

也就是说，这类路径最后仍可能回到：

- `lance.LanceDataset.commit(...)`

所以如果你的项目自己做 distributed scalar index publish，要单独注意最后那一步 commit 的 namespace 透传。

---

## 4.4 版本切换 / branch 相关

### `checkout_version(...)`

源码位置：

- `python/python/lance/dataset.py:2673`

它内部是 `copy.copy(self)` 再 checkout，因此会保留已有 dataset 上的 namespace 上下文。

### `create_branch(...)`

源码位置：

- `python/python/lance/dataset.py:773`

它也会把：

- `_namespace_client`
- `_table_id`

拷到新 dataset 上。

### 但有一个细节坑

`create_branch(...)` 这段实现里**没有显式复制**：

- `_namespace_client_managed_versioning`

所以如果你要在 branch dataset 上继续走**特别低层、显式依赖 managed_versioning 透传**的写提交路径，最好重新确认或重新打开，不要想当然地假设这个标志永远跟着走。

这不影响常规 read；主要影响你做非常低层的 commit 协议时的把握。

---

## 4.5 低层 delete 协议：`fragment.delete(...) + LanceOperation.Delete + commit(...)`

源码位置：

- `fragment.delete(...)`：`python/python/lance/fragment.py:919`
- `LanceOperation.Delete`：`python/python/lance/dataset.py:4937`

这条路适合：

- 自己做 distributed delete
- fragment 级删除产物收集
- CN 最后统一发布

典型思路：

1. 先打开 `ds = lance.dataset(namespace_client=ns, table_id=table_id)`
2. 对 fragments 分别调用 `fragment.delete(predicate)`
3. 组装 `LanceOperation.Delete(...)`
4. 最后 `LanceDataset.commit(...)`

这里本质上和 `write_fragments + commit` 是同一风格：

> **worker 做物理产物，coordinator 做最终 manifest publish。**

---

## 4.6 metadata / schema / config 更新

在 dataset API 里还可以看到：

- `update_metadata(...)`
- `update_config(...)`
- `update_schema_metadata(...)`
- `update_field_metadata(...)`

这些都属于“dataset 实例方法”这一类。

也就是说，如果你已经：

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
```

那么这类 API 也应该优先沿用 dataset 路线，而不是优先改写成 namespace 原生调用。

如果你想做 table-id 风格的控制面改造，则可以考虑 namespace 原生：

- `ns.update_table_schema_metadata(...)`

---

## 4.7 compaction / optimize 类接口

从类型定义可以看到：

- `Compaction.plan(dataset, options)`
- `Compaction.execute(dataset, options)`
- `Compaction.commit(dataset, rewrites)`

定义位置：

- `python/python/lance/lance/optimize.pyi:31-42`

这组 API 的入参是：

- `dataset: LanceDataset`

也就是说它们天然更适合走：

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
```

然后再：

```python
Compaction.plan(ds, ...)
Compaction.execute(ds, ...)
Compaction.commit(ds, rewrites)
```

### 这说明什么

说明 optimize / maintenance 这类接口，即使没有显式暴露 `namespace_client/table_id` 参数，也仍然很适合挂在“先 namespace-open dataset，再走 dataset/object API”这条主路线下面。

---

## 5. C 类：namespace 原生 API（不是主路线，但要知道有哪些）

如果你希望项目里某些层彻底不暴露 `uri` / `dataset`，而是只用 `table_id` 做操作，那么可以直接调用 namespace 原生 API。

源码位置：

- `python/python/lance/namespace.py`

---

## 5.1 表生命周期 / 表定位

常见的有：

- `describe_table(...)`
- `create_table(...)`
- `declare_table(...)`
- `register_table(...)`
- `rename_table(...)`
- `drop_table(...)`
- `restore_table(...)`
- `list_tables(...)`
- `list_all_tables(...)`
- `table_exists(...)`

### 用途

- 纯 catalog / control-plane 操作
- 不想先 open dataset
- 先拿表 location / storage options / managed_versioning

---

## 5.2 原生写入 / mutation

常见的有：

- `insert_into_table(...)`
- `merge_insert_into_table(...)`
- `update_table(...)`
- `delete_from_table(...)`
- `count_table_rows(...)`
- `query_table(...)`

### 适合什么

- 服务端统一封装“按 table_id 发命令”
- 你希望对外暴露的是 namespace service，而不是 Lance Python dataset 对象

### 但为什么我不建议拿它做你的主改造路线

因为你当前的项目原来已经是 Lance API 风格。

如果全改成 namespace native：

- 业务代码改动更大
- 原来 `ds.update / ds.delete / ds.merge_insert / ds.create_index` 这套都要重新抽象
- 很多 Lance 原生能力会变成“服务端代理一层再暴露”

所以更适合：

- 做 control plane
- 做 RPC service
- 做对外 API

而不是拿来替代你项目里原有的大量 dataset 逻辑。

---

## 5.3 原生 index / schema / tag / version API

namespace 原生还暴露了：

### index

- `create_table_index(...)`
- `create_table_scalar_index(...)`
- `list_table_indices(...)`
- `describe_table_index_stats(...)`
- `drop_table_index(...)`

### schema / 列变更

- `update_table_schema_metadata(...)`
- `alter_table_add_columns(...)`
- `alter_table_alter_columns(...)`
- `alter_table_drop_columns(...)`

### version

- `list_table_versions(...)`
- `create_table_version(...)`
- `describe_table_version(...)`
- `batch_delete_table_versions(...)`

### tag

- `list_table_tags(...)`
- `get_table_tag_version(...)`
- `create_table_tag(...)`
- `update_table_tag(...)`
- `delete_table_tag(...)`

### transaction

- `describe_transaction(...)`
- `alter_transaction(...)`

这些接口说明 namespace 不只是“给 Lance 提供 location 的薄封装”，它本身已经是一整层 **table-id oriented control plane**。

---

## 6. 对你项目最有用的“改造映射表”

下面这张映射最重要。

---

## 6.1 读表

原来：

```python
ds = lance.dataset(uri)
```

改成：

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
```

后续 read API 基本不变。

---

## 6.2 高层 create / append / overwrite

原来：

```python
lance.write_dataset(data, uri, mode="append")
```

改成：

```python
lance.write_dataset(data, namespace_client=ns, table_id=table_id, mode="append")
```

---

## 6.3 行级 update / delete / upsert

原来：

```python
ds = lance.dataset(uri)
ds.update(...)
ds.delete(...)
ds.merge_insert(...)
```

改成：

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
ds.update(...)
ds.delete(...)
ds.merge_insert(...)
```

也就是：

> **入口变，业务操作尽量不变。**

---

## 6.4 索引

原来：

```python
ds = lance.dataset(uri)
ds.create_index(...)
```

改成：

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
ds.create_index(...)
```

如果你非要走 table-id 原生风格，则可以：

```python
ns.create_table_index(...)
```

但默认还是推荐 dataset 路线。

---

## 6.5 分布式写

原来：

- worker 写 fragment
- coordinator commit 到 dataset URI

改成：

1. coordinator 通过 namespace 解析表：
   - `declare_table(...)` / `describe_table(...)`
2. worker 继续用真实 `table_uri` 写：
   - `write_fragments(...)`
3. coordinator 显式带 namespace 上下文提交：
   - `LanceDataset.commit(..., namespace_client=ns, table_id=table_id, namespace_client_managed_versioning=managed)`

---

## 6.6 文件级读写

原来：

```python
LanceFileReader(path, storage_options=...)
LanceFileWriter(path, storage_options=...)
```

改成：

```python
LanceFileReader(path, storage_options=..., namespace_client=ns, table_id=table_id)
LanceFileWriter(path, storage_options=..., namespace_client=ns, table_id=table_id)
```

或者如果已经有 dataset：

```python
file_sess = ds.new_file_session()
```

---

## 6.7 TensorFlow 数据管道

原来：

```python
lance.tf.data.from_lance(uri)
```

改成：

```python
lance.tf.data.from_lance(namespace_client=ns, table_id=table_id)
```

---

## 7. 这次调研里最值得你注意的坑

这些坑我建议你在项目改造时单独记住。

---

## 7.1 不是所有 Lance API 都直接暴露 `namespace_client/table_id`

直接带 namespace 参数的，只是少数关键入口。

真正的大多数高层能力，仍然是：

- 先 namespace-open dataset
- 再走 dataset 方法

所以不要期待“每个方法签名都多两个 namespace 参数”。

---

## 7.2 `LanceDataset.commit(...)` 不会自动从传入 dataset 对象里继承 namespace 参数

这是一个很关键的坑。

`LanceDataset.commit(...)` 是静态方法。

它的 `base_uri` 虽然可以传 `LanceDataset`，但源码里会先把它变成内部 `_ds` 对象：

- `python/python/lance/dataset.py:4072-4073`

然后真正的 namespace 透传，仍然依赖你显式传：

- `namespace_client`
- `table_id`
- `namespace_client_managed_versioning`

所以不要写成这样就以为万事大吉：

```python
lance.LanceDataset.commit(ds, op, read_version=ds.version)
```

如果你要走 namespace-managed commit，更稳的是显式写全：

```python
lance.LanceDataset.commit(
    ds.uri,
    op,
    read_version=ds.version,
    storage_options=ds.latest_storage_options(),
    namespace_client=ns,
    table_id=table_id,
    namespace_client_managed_versioning=managed,
)
```

---

## 7.3 `commit_batch(...)` 目前不是 namespace 改造的主路径

源码位置：

- `python/python/lance/dataset.py:4154`

它当前：

- 没有 `namespace_client/table_id` 参数
- 返回的新 dataset 还会把：
  - `_namespace_client = None`
  - `_table_id = None`

也就是说，**它当前并不是一个很好的一等 namespace commit 入口**。

如果你的项目原先用了 `commit_batch(...)`，建议谨慎，最好不要把它当 namespace 主路线的关键依赖。

---

## 7.4 dataset 对象跨进程 / pickle 后会丢 namespace 上下文

源码位置：

- `__setstate__`：`python/python/lance/dataset.py:673-700`

反序列化后会清掉：

- `_namespace_client = None`
- `_table_id = None`
- `_namespace_client_managed_versioning = False`

所以：

> **不要把 `LanceDataset` 当成跨 worker 传递 namespace 上下文的载体。**

跨进程时更稳的是传：

- `table_id`
- `table_uri`
- `storage_options`
- `managed_versioning`
- `read_version`

必要时在目标进程重新打开 dataset。

---

## 7.5 `create_branch(...)` 看起来保留了 namespace client / table_id，但没显式保留 managed flag

这不是最常见路径，但如果你在 branch 上继续玩低层 commit，就要小心。

对普通 read / scan 影响不大。

---

## 7.6 `DirectoryNamespace` 上别默认把所有 namespace-native mutation 都当成“已完整打通”

从 Python namespace 接口定义上看，原生 API 很全。

但从这次看的实现侧代码里，至少对 `DirectoryNamespace`：

- `insert_into_table(...)`
- `merge_insert_into_table(...)`
- `create_table_index(...)`

是明确能看到落地实现的。

而：

- `update_table(...)`
- `delete_from_table(...)`

不宜默认假设已经同样成熟可用。

所以仍然推荐：

- `update / delete` 默认走 dataset 路线

---

## 8. 我对你这个项目的最终建议

如果你要把“原来没有 namespace 的 Lance 项目”整体改上 namespace，我建议定成下面这个统一口径。

---

## 8.1 统一主路线：全部走 route B

也就是：

> **namespace-aware / namespace-resolved dataset integration**

具体说：

- 读：`lance.dataset(namespace_client=..., table_id=...)`
- 高层写：`lance.write_dataset(..., namespace_client=..., table_id=...)`
- 行级 mutation：先 open dataset，再 `ds.update/delete/merge_insert`
- 索引：先 open dataset，再 `ds.create_index(...)`
- optimize / maintenance：先 open dataset，再走 dataset/object API

---

## 8.2 低层分布式写单独收口

对 `write_fragments / fragment.create / distributed delete / distributed index build` 这类低层协议，统一约定：

- CN 负责 namespace 解析与 commit
- DN 负责物理产物生成
- 最终 publish 一律显式透传 namespace commit 参数

也就是把：

- `table_id`
- `table_uri`
- `storage_options`
- `managed_versioning`
- `read_version`

都当成协议字段，而不是隐式依赖某个 dataset 对象“自己记得住”。

---

## 8.3 namespace-native API 作为补充，而不是主替代

保留给这些场景：

- control plane
- RPC service
- 只想暴露 `table_id` 不想暴露 dataset / uri
- 统一服务端执行 table 操作

但不要为了“所有东西都 namespace 化”就把原本成熟的 Lance dataset 业务逻辑全部改成 RPC 风格。

那个改造成本高，而且并不一定更稳。

---

## 9. 一句话结论

如果只用一句话概括这次调研，那就是：

> **能直接接 namespace 的 Lance 入口主要集中在 `dataset / write_dataset / write_fragments / fragment.create / commit / file APIs / tf.data` 这些地方；而你项目里绝大多数原有逻辑，最合理的改法不是改成 namespace 原生 RPC，而是“先通过 namespace 打开 dataset，再继续走 Lance dataset API”。**

这也是我认为最适合你当前项目改造的路线。
