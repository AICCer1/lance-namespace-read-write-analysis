# 现有 Lance 项目改造成 namespace 接入的 checklist

## 版本范围

- `pylance` / `lance`：`v6.0.0`
- `lance-namespace`：`v0.7.6`

---

## 1. 先给结论

如果一个现有项目原来已经在用 Lance，而且大多数代码是：

- 先拿 `uri`
- 打开 `dataset`
- 后续走 `ds.update / ds.delete / ds.merge_insert / ds.create_index / scan / to_table`

那么要整体改造成 namespace，**主体工作量通常不在“重写业务逻辑”，而在“改入口 + 补上下文字段透传”**。

也就是说：

### 轻改部分

主要是把：

- `uri` 风格入口
- 直接对象存储路径入口

替换成：

- `namespace_client + table_id`
- 或者先通过 namespace resolve 出 `table_uri`

这部分通常是：

> **接口改造 / 参数改造 / 依赖注入改造**

而不是业务语义重写。

---

### 真正要认真改的部分

主要集中在低层链路：

- 分布式写
- 低层 commit
- fragment 级操作
- index segment 级操作
- 跨进程传递 dataset 上下文

这部分的关键不是“多加两个参数”这么简单，而是：

> **把原来隐式依赖 dataset/uri 状态的链路，改成显式传递 namespace 协议字段。**

---

## 2. 建议先定总路线

先把团队口径定死，不然边改边飘最烦。

### 推荐总路线：route B

也就是：

> **先通过 namespace 打开 dataset，再继续走 Lance dataset API。**

具体是：

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
```

然后继续：

- `ds.to_table()`
- `ds.update(...)`
- `ds.delete(...)`
- `ds.merge_insert(...).execute(...)`
- `ds.create_index(...)`
- `Compaction.plan/execute/commit(ds, ...)`

### 为什么推荐这个路线

因为它对现有 Lance 项目最友好：

- 现有业务逻辑改动最小
- 最多只是入口变化
- 大多数数据平面代码可以保留
- 不需要把全部逻辑重构成 namespace-native RPC 风格

---

### 不推荐默认全改成 namespace-native mutation API

也就是不建议一上来就把业务层全改成：

- `ns.insert_into_table(...)`
- `ns.update_table(...)`
- `ns.delete_from_table(...)`
- `ns.create_table_index(...)`

因为这样做的代价通常更大：

- 代码风格整体改变
- 现有 dataset 逻辑要重封装
- backend 实现完整度也容易卡你

所以 namespace-native API 更适合作为：

- control plane
- RPC facade
- 对外服务接口

而不是你项目内部主数据面改造路线。

---

## 3. 总体改造分层

建议把项目里的 Lance 相关代码按下面 4 层分开盘点。

### 第 1 层：入口层

也就是这些地方：

- 哪里创建 `namespace_client`
- 哪里知道 `table_id`
- 哪里原来是 `uri`
- 哪里第一次打开 dataset

### 第 2 层：高层 dataset 操作层

也就是这些地方：

- query / read
- append / overwrite / create
- update / delete / merge_insert
- create_index
- schema / metadata update
- optimize / compaction

### 第 3 层：低层协议层

也就是这些地方：

- `write_fragments(...)`
- `LanceFragment.create(...)`
- `LanceDataset.commit(...)`
- `LanceOperation.*`
- distributed delete
- distributed index build

### 第 4 层：跨进程 / 跨节点通信层

也就是这些地方：

- CN -> DN 任务分发
- worker 参数结构
- RPC / 消息体 / task payload
- 任务重试 / 回放

真正容易出坑的，大部分都在 **第 3、4 层**。

---

## 4. 第一阶段 checklist：把“入口怎么开表”统一掉

这一步是最值回票价的，因为它能把 60%~80% 的常规业务路径快速带上 namespace。

---

## 4.1 搜索所有 `lance.dataset(uri)` / `LanceDataset(uri)` / 变种 open

### 目标

把所有“直接拿 URI 打开表”的入口盘出来。

### 典型旧代码

```python
ds = lance.dataset(uri)
```

### 目标新代码

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
```

### 需要确认的事

- `ns` 从哪里注入
- `table_id` 在业务里怎么表达
- 原来 `uri` 是不是已经被上层抽象封装
- 是否需要保留 `version` / `asof` / `storage_options` 等参数

### 风险等级

- **低**

### 工作量判断

- 大多属于接口替换
- 后续 read / scan / mutation 逻辑通常不用动

---

## 4.2 搜索所有 `write_dataset(data, uri, ...)`

### 典型旧代码

```python
lance.write_dataset(data, uri, mode="append")
```

### 目标新代码

```python
lance.write_dataset(
    data,
    namespace_client=ns,
    table_id=table_id,
    mode="append",
)
```

### 额外要确认的事

- create / append / overwrite 三种模式分别在哪里用
- create 路径是否原来依赖某些固定 path 约定
- commit message / transaction properties 有没有要求保留

### 风险等级

- **低到中**

### 说明

高层写入已经是 Lance 自己帮你处理 namespace resolve 的路线，所以这是非常适合先改的一类。

---

## 4.3 搜索所有 `from_lance(uri)` / 训练数据入口

### 典型旧代码

```python
lance.tf.data.from_lance(uri)
```

### 目标新代码

```python
lance.tf.data.from_lance(namespace_client=ns, table_id=table_id)
```

### 风险等级

- **低**

### 说明

这类通常也是替换入口，不涉及下游训练逻辑大改。

---

## 4.4 搜索“自己拼 storage path 再 open dataset”的辅助函数

很多项目不一定直接写 `lance.dataset(uri)`，而是有一层：

```python
def open_table(table_name):
    uri = build_table_uri(table_name)
    return lance.dataset(uri)
```

### 目标

把这种 helper 统一改成：

- 接受 `namespace_client`
- 接受 `table_id`
- 或者内部先 resolve 再 open

### 风险等级

- **低到中**

### 说明

如果 helper 层改得好，业务层很多地方几乎不用大动。

---

## 5. 第二阶段 checklist：高层 dataset API 保持不变，确认哪些能直接沿用

这一步的目标不是大改代码，而是**确认哪些地方只改入口即可**。

---

## 5.1 read / query 路径

通常包括：

- `to_table()`
- `to_batches()`
- `scanner(...)`
- `count_rows()`
- `take(...)`
- `search(...)`
- `schema`
- `get_fragments()`

### 改造原则

如果前面已经改成：

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
```

那么这些读路径通常都**不需要再 namespace 化改签名**。

### 风险等级

- **低**

### 结论

这是最典型的“只改入口，不改逻辑”。

---

## 5.2 `update / delete / merge_insert`

### 旧代码

```python
ds = lance.dataset(uri)
ds.update(...)
ds.delete(...)
ds.merge_insert(...).execute(...)
```

### 新代码

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
ds.update(...)
ds.delete(...)
ds.merge_insert(...).execute(...)
```

### 风险等级

- **低到中**

### 说明

这里的主要工作量仍然在入口替换，而不是重写 mutation 逻辑。

### 额外提醒

如果有人提议把这块全部改写成：

- `ns.update_table(...)`
- `ns.delete_from_table(...)`
- `ns.merge_insert_into_table(...)`

默认先别这么干。

原因很简单：

- 工程改造更重
- 原有 dataset builder 风格逻辑会被打散
- backend 对 namespace-native API 的成熟度未必和 dataset API 一样稳

---

## 5.3 `create_index(...)`

### 旧代码

```python
ds = lance.dataset(uri)
ds.create_index("id", "BTREE", name="id_idx")
```

### 新代码

```python
ds = lance.dataset(namespace_client=ns, table_id=table_id)
ds.create_index("id", "BTREE", name="id_idx")
```

### 风险等级

- **低到中**

### 说明

这类也通常是入口替换即可。

---

## 5.4 compaction / optimize / maintenance 类接口

如果项目里有：

- `Compaction.plan(ds, ...)`
- `Compaction.execute(ds, ...)`
- `Compaction.commit(ds, ...)`

### 改造方式

把 `ds` 的来源改成 namespace-open dataset。

### 风险等级

- **中**

### 为什么是中风险

因为这种链路虽然仍然是 dataset-based，但往往更靠近维护面和低层存储语义，最好单独做一次回归验证。

---

## 5.5 metadata / schema / config 更新

如果项目里有：

- `update_metadata(...)`
- `update_config(...)`
- `update_schema_metadata(...)`
- `update_field_metadata(...)`

### 改造原则

优先继续走 dataset 路线，不要急着改成 namespace-native schema API。

### 风险等级

- **中**

---

## 6. 第三阶段 checklist：低层写协议要补哪些字段

这一部分才是 namespace 改造里真正该盯紧的地方。

这里的核心原则是：

> **不要依赖 dataset 对象自己“带着 namespace 状态活着”，而要把关键上下文当成协议字段显式传递。**

---

## 6.1 盘点所有 `write_fragments(...)`

### 旧模式常见写法

```python
fragments = write_fragments(data, table_uri, ...)
```

### namespace 改造后要补的上下文

- `table_uri`
- `table_id`
- `storage_options`
- `namespace_client`
- `managed_versioning`（通常由 CN resolve 后决定）
- `read_version`（如果后续 commit 需要）

### 推荐职责划分

- CN：通过 namespace resolve 表
- DN：只负责写 fragments
- CN：统一 commit

### 风险等级

- **高**

### 为什么高风险

因为这部分不是简单改签名，而是要检查：

- 谁负责 resolve
- 谁负责传 storage options
- 谁负责 commit
- 失败重试时用哪一版 read_version

---

## 6.2 盘点所有 `LanceFragment.create(...)`

### 适用场景

- 项目已经使用 fragment 级低层写协议
- 自己构造 `LanceOperation.Append / Overwrite`

### 需要补的字段

和 `write_fragments(...)` 类似：

- `table_uri`
- `table_id`
- `storage_options`
- `namespace_client`
- `managed_versioning`
- `read_version`

### 风险等级

- **高**

---

## 6.3 盘点所有 `LanceDataset.commit(...)`

这是最关键的一项。

### 旧代码常见写法

```python
lance.LanceDataset.commit(table_uri, op, read_version=read_version)
```

或者更隐蔽一点：

```python
lance.LanceDataset.commit(ds, op, read_version=ds.version)
```

### namespace 改造后推荐写法

```python
lance.LanceDataset.commit(
    table_uri,
    op,
    read_version=read_version,
    storage_options=storage_options,
    namespace_client=ns,
    table_id=table_id,
    namespace_client_managed_versioning=managed,
)
```

### 必查点

- commit 的调用点是否能拿到 `ns`
- commit 的调用点是否能拿到 `table_id`
- `managed_versioning` 是谁决定并往下传
- `read_version` 是谁记录、谁校验、谁更新

### 风险等级

- **高 / 关键路径**

### 额外提醒

不要误以为：

```python
LanceDataset.commit(ds, ...)
```

就会自动继承 `ds` 的 namespace 上下文。

这里非常容易被坑。

---

## 6.4 如果项目用了 `commit_batch(...)`，单独拉红牌

### 原因

当前这条路：

- 没有 `namespace_client/table_id` 参数
- 返回的新 dataset 还会清掉 namespace 字段

### 结论

如果项目核心流程里大量依赖 `commit_batch(...)`，那这里不是“顺手加字段”能解决的，要单独评估。

### 风险等级

- **高 / 需专项评估**

---

## 6.5 distributed delete

如果项目不是直接 `ds.delete(...)`，而是：

- worker 对 fragment 做 delete
- coordinator 聚合 `LanceOperation.Delete`
- 最后统一 commit

### 改造重点

和低层 write/commit 一样：

- 统一由 CN 持有 namespace commit 上下文
- worker 不传 dataset 对象，传产物和协议字段

### 风险等级

- **高**

---

## 6.6 distributed index build

如果项目用了：

- `create_index_uncommitted(...)`
- `create_index_segment_builder(...)`
- `merge_existing_index_segments(...)`
- `commit_existing_index_segments(...)`

### 改造原则

这类 API 本身更适合 route B：

- 先打开 namespace-aware dataset
- 然后在 dataset 对象上继续做 index build

### 要检查什么

- worker 的 dataset 是怎么打开的
- segment metadata 是怎么回传的
- 最终 publish 是在哪个进程做的
- publish 阶段是否仍需显式 commit / version 语义确认

### 风险等级

- **中到高**

---

## 7. 第四阶段 checklist：跨进程/跨节点协议要改哪些

如果你有 CN / DN 架构，这一层很关键。

---

## 7.1 不要跨进程传 `LanceDataset` 作为上下文载体

### 原因

dataset 对象跨进程 / pickle 后，会丢：

- `_namespace_client`
- `_table_id`
- `_namespace_client_managed_versioning`

### 所以要怎么改

跨进程时不要传：

- `ds`

而要传：

- `table_id`
- `table_uri`
- `storage_options`
- `managed_versioning`
- `read_version`
- 任务本身需要的数据分片 / fragment assignment

### 风险等级

- **高**

---

## 7.2 统一 CN -> DN 协议字段

建议把 worker task payload 统一成显式字段，不要靠隐式约定。

### 推荐最小字段集

```python
{
  "table_id": [...],
  "table_uri": "...",
  "storage_options": {...},
  "managed_versioning": true,
  "read_version": 123,
  "job_type": "write_fragments",
  "assignment": {...}
}
```

### 为什么要这样做

因为 namespace 改造真正改的，不只是 Python 调用签名，而是**跨节点协议**。

### 风险等级

- **高 / 值得单独抽象**

---

## 7.3 统一 retry / stale writer / conflict 处理语义

### 需要明确的问题

- worker 失败重试时，是否重用旧 `read_version`
- CN 收到部分成功、部分失败的 fragment 怎么处理
- 如果 commit retry/rebase 发生，哪些层要感知
- 是否需要业务侧自己实现更严格的 CAS

### 风险等级

- **高**

### 说明

这部分和 namespace 接入不是一回事，但接入 namespace 后，很多人会误以为“并发控制就都自动解决了”。

其实不是。

---

## 8. 建议的实际改造顺序

不要上来全改。最稳的是分阶段推进。

---

## Phase 1：先把高层 open / read / write 入口 namespace 化

优先改：

- `dataset(...)`
- `write_dataset(...)`
- `from_lance(...)`
- 统一 helper / repository 层

### 目标

先让 70% 的普通路径跑起来。

---

## Phase 2：把 dataset-based mutation / index / maintenance 接上

优先改：

- `update / delete / merge_insert`
- `create_index`
- `metadata / schema update`
- `compaction / optimize`

### 目标

保持数据平面 API 风格不变，只换入口。

---

## Phase 3：单独收口低层分布式协议

优先改：

- `write_fragments`
- `fragment.create`
- `commit`
- distributed delete
- distributed index

### 目标

把 namespace 字段显式协议化。

---

## Phase 4：回头评估是否需要 namespace-native API facade

只有在这些场景才做：

- 需要对外暴露 control-plane / RPC
- 需要服务端统一 table-id 风格命令
- 需要屏蔽 Lance dataset 细节

这一步通常不是第一优先级。

---

## 9. 风险分级总表

## 低风险：基本是入口替换

- `dataset(uri)` -> `dataset(namespace_client=..., table_id=...)`
- `write_dataset(data, uri, ...)` -> namespace 版本
- `from_lance(uri)` -> namespace 版本
- 普通 read / scan / query

### 特征

- 改签名为主
- 业务逻辑不怎么动

---

## 中风险：需要做回归验证

- `update / delete / merge_insert`
- `create_index`
- metadata / schema update
- compaction / optimize
- helper / repository 层统一抽象

### 特征

- 逻辑主体不变
- 但需要确认 dataset 来源改变后语义仍一致

---

## 高风险：需要协议级改造

- `write_fragments(...)`
- `LanceFragment.create(...)`
- `LanceDataset.commit(...)`
- `commit_batch(...)`
- distributed delete
- distributed index build
- CN / DN task payload
- stale writer / retry / conflict 语义

### 特征

- 不是单纯加字段
- 需要重新梳理职责边界和上下文传递

---

## 10. 可以直接拿去执行的排查清单

下面这份最实用，基本可以按 repo 搜索一轮。

---

## 10.1 搜索这些关键调用点

- `lance.dataset(`
- `LanceDataset(`
- `write_dataset(`
- `write_fragments(`
- `LanceFragment.create(`
- `LanceDataset.commit(`
- `commit_batch(`
- `merge_insert(`
- `create_index(`
- `create_index_uncommitted(`
- `commit_existing_index_segments(`
- `Compaction.plan(`
- `Compaction.execute(`
- `Compaction.commit(`
- `from_lance(`
- `LanceFileReader(`
- `LanceFileWriter(`
- `LanceFileSession(`

---

## 10.2 对每个调用点问 5 个问题

1. 这里原来拿到的是 `uri` 还是 `dataset`？
2. 这里改完后，是不是只需要改入口？
3. 这里是否跨进程 / 跨节点？
4. 这里最终是否会走低层 commit？
5. 这里需要显式传哪些协议字段？

---

## 10.3 对每条跨节点链路问 4 个问题

1. `table_id` 是谁生成 / 持有的？
2. `table_uri` 是谁 resolve 的？
3. `managed_versioning` 是谁决定并下发的？
4. `read_version` 是谁记录并最终用于 commit 的？

---

## 11. 实操建议：改造时最好顺手抽一个统一上下文对象

如果你不想每个函数都散落一堆参数，建议抽一个显式上下文对象。

例如：

```python
@dataclass
class NamespaceTableContext:
    namespace_client: Any
    table_id: list[str]
    table_uri: str
    storage_options: dict[str, str]
    managed_versioning: bool
    read_version: int | None = None
```

### 为什么值得抽

因为低层链路里真正反复出现的，就是这一坨信息。

抽出来之后：

- helper 层更统一
- CN / DN payload 更容易规范
- commit 调用点不容易漏字段

这是很值的一个工程动作。

---

## 12. 一句话总结

如果你的项目整体改 namespace，**主要工作量确实大多集中在接口层与上下文透传层**：

- 高层路径：多数只是把 `uri` 入口改成 `namespace_client + table_id`
- 低层路径：重点是把 `table_id / table_uri / storage_options / managed_versioning / read_version` 从隐式状态改成显式协议字段

所以这不是“全面重写 Lance 业务逻辑”，而更像：

> **把现有 Lance 项目从 uri-first 改造成 namespace-aware，并把低层协议做显式化。**

这也是我认为最稳、最省工程量的一条路。
