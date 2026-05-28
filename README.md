# Lance Namespace 读表、写路径、冲突边界与项目改造总结

这个仓库不是单点文档集合，而是围绕同一件事做分层分析：

> **Lance 接进 namespace 之后，到底是谁负责表定位、凭证下发、版本发布、冲突检测，以及现有项目应该怎么改。**

分析基于以下固定版本：

- `lance` / `pylance`: `v6.0.0`
- `lance-namespace`: `v0.7.6`

## 一页理解

如果只记住几句话，这几句最值钱：

1. **namespace 在 Lance 里首先是控制面，不是数据面替代物。**
   它主要负责表定位、存储参数、凭证刷新、版本管理策略声明，而不是替换 Lance 的数据文件读写本体。

2. **推荐默认路线是“先通过 namespace 打开 dataset，再继续走 Lance dataset API”。**
   这条路线改造成本最低，也最接近现有 Lance 项目的自然用法。

3. **`managed_versioning` 不是一个本地随便开的布尔开关，而是 namespace backend 对表能力的声明。**
   正确方向是：`table_version_tracking_enabled` -> `describe_table(...).managed_versioning` -> dataset/runtime/commit 透传。

4. **namespace 接管的不是“所有 commit 冲突”，而是 commit 里“版本发布”这条边界。**
   `create_table_version(...)` 能防住重复发布同一个 target version、manifest publish race、对象存储不够原子的 publish 问题；它不等于替你兜住所有 stale writer / read_version CAS 语义。

5. **低层 `write_fragments(...) + LanceDataset.commit(...)` 是协议接口，不是高层便利接口。**
   到了这条路，`table_uri`、`storage_options`、`read_version`、`namespace_client`、`table_id`、`managed_versioning` 都应该由上游显式准备和透传，不能指望底层自动猜。

6. **namespace-managed commit 成功后，final manifest 还是会 materialize 回 object store / 表目录。**
   所以 plain `lance.dataset(uri)` 仍可打开，表目录 copy 走后仍可恢复读取。这是 external manifest 设计很关键的可移植性价值。

## 我基于全部文档整理出的统一理解

### 1. namespace 的真实职责分三层

- **表上下文解析器**：`table_id -> location/table_uri`
- **存储与凭证提供方**：返回 `storage_options`，必要时支持 refresh
- **版本发布协调器**：在 `managed_versioning=True` 时，接管 `list_table_versions / describe_table_version / create_table_version`

所以 namespace 不是“另一个 dataset API”，而是 Lance 前后的 **control plane + catalog + version registry**。

### 2. 读路径和写路径的接入深度不一样

- **读路径** 很自然：`lance.dataset(namespace_client=..., table_id=...)`
- **高层写路径** 也比较自然：`lance.write_dataset(...)`
- **低层写路径** 才是真正容易踩坑的地方：`write_fragments(...) + LanceDataset.commit(...)`

原因很简单：`write_fragments(...)` 只负责写 fragment，不负责“发布新版本”；真正决定是否走 namespace-managed commit 的，是最终 `commit(...)` 那一步。

### 3. `managed_versioning` 是一条必须完整透传的协议线

这条线在源码里看起来出现了三次，但语义并不重复：

- `table_version_tracking_enabled`
  - backend 能力开关
- `response.managed_versioning`
  - 表级能力信号
- `namespace_client_managed_versioning`
  - 本次 dataset / commit 的执行开关

最关键的结论不是“为什么传三次”，而是：

> **这是 capability -> response -> runtime execution 的三段式传播，不是重复配置。**

### 4. commit 里 namespace 真正接管的是“版本发布边界”

namespace-managed commit 的核心不是“commit 全交给 namespace”，而是：

- staging manifest 先写出来
- 再通过 namespace 的 `create_table_version(...)` 把版本正式发布出去
- 成功后得到 final manifest location
- staging manifest 清理掉

所以 `ExternalManifestCommitHandler` / `create_table_version(...)` 保护的是：

- 同一个目标版本不能被重复认领
- manifest 发布阶段的并发竞争
- 某些对象存储 publish 原子性不够时的版本发布安全

但它**不等于**完整事务冲突求解器。

### 5. `DirectoryNamespace` 很适合 probe，但不能误读它的语义

`DirectoryNamespace` 在本地验证上很好用，因为很多行为能直接从目录和 manifest 看出来。

但要注意：

- `table_version_tracking_enabled=false` 不等于 commit 层的硬拦截器
- 如果你在 commit 时硬传 `namespace_client_managed_versioning=True`
  - 对 `DirectoryNamespace` 可能还能跑
  - 但语义已经和它对外声明不一致
- 对别的 backend，则可能直接 `not supported`

这也是为什么文档里反复强调：

> **不要把 `namespace_client_managed_versioning=True` 当作本地 override。**

### 6. 从工程改造角度，namespace 不是“一次性全替换”

更现实的方式是分层改：

- 先统一入口层：所有“开表”都改成 namespace-aware
- 再统一高层 dataset 操作层
- 最后只在真正需要的地方改低层协议层
- 跨进程 / 跨节点时，再显式传递表上下文

这比一上来全面切成 namespace-native mutation API 更稳。

## 项目要适配 namespace，最关键的结论

这是我看完当前全部文档后，认为最关键的 8 条结论。

1. **默认主路线选 Route B：先通过 namespace 打开 dataset，再继续用 Lance dataset API。**
   这条路线最接近现有代码习惯，也最容易逐步替换旧的 `lance.dataset(uri)` 用法。

2. **先统一“入口怎么开表”，再谈 mutation。**
   如果项目里还到处散落原始 `table_uri` / `dataset(uri)`，后面 write、update、index、commit 的 namespace 语义一定会裂开。

3. **CN 应该显式 resolve 一次表上下文。**
   至少拿到：
   - `location`
   - `storage_options`
   - `managed_versioning`
   - `table_id`
   - 基础 snapshot / `read_version`

4. **DN / worker 最好只做数据写，不做版本语义判断。**
   worker 负责写 fragments；最终 commit 由 CN 收口，并带着上一步 resolve 出来的上下文统一提交。

5. **低层 commit 必须手动透传 namespace 语义，不能靠“之前打开过 dataset”来赌。**
   特别是 `write_fragments(...) + LanceDataset.commit(...)` 这条路，最容易出现：
   - 忘传 `namespace_client_managed_versioning`
   - 只传 `namespace_client + table_id` 但没传 managed flag
   - 手工强行写 `True`

6. **`managed_versioning` 必须来源于 namespace response，而不是本地拍脑袋。**
   正确写法是：

   ```python
   managed = resp.managed_versioning is True
   ```

   不应该写：

   ```python
   managed = True
   ```

7. **namespace 接入后，冲突语义会变“分层”，不是“统一归 namespace 管”。**
   - 表创建、index 命名、target version 重复发布，这类 catalog / version registry 边界由 namespace 更直接处理
   - 完整事务语义、stale writer 结果、append/overwrite/restore 组合冲突，仍要看 Lance 原生 conflict resolver

8. **验证 namespace 适配是否真的完成，要盯低层行为，不要只看 API 调通。**
   至少要验证：
   - 是否真的命中了 `list_table_versions / describe_table_version / create_table_version`
   - final manifest 是否落盘
   - plain reader 是否还能直接打开
   - 目录 copy 后是否仍可恢复读取

## 推荐阅读顺序

### 如果你只想先抓住整体脉络

1. `docs/namespace-read-write-fragments.md`
2. `docs/managed-versioning-how-it-works.md`
3. `docs/why-managed-versioning-is-passed-three-times.md`

### 如果你准备改造现有项目

1. `docs/namespace-interface-survey.md`
2. `docs/namespace-retrofit-checklist.md`
3. `docs/namespace-create-update-index.md`
4. `docs/namespace-native-vs-dataset-api.md`

### 如果你重点关注低层分布式写协议

1. `docs/namespace-low-level-write-fragments-commit-flow.md`
2. `docs/why-managed-versioning-is-passed-three-times.md`
3. `examples/distributed_write_with_namespace.py`
4. `examples/write_fragments_append_with_managed_versioning.py`

### 如果你重点关注冲突与边界

1. `docs/namespace-conflict-scope.md`
2. `docs/directory-namespace-conflict-detection.md`
3. `docs/external-manifest-commit-handler-boundary.md`
4. `examples/directory_namespace_conflict_matrix.py`
5. `examples/external_manifest_commit_handler_boundary.py`

## 仓库内容

### 核心分析文档

- `docs/namespace-read-write-fragments.md`
  - 主分析文档，先回答 namespace 在读表、分布式写、commit 冲突里分别扮演什么角色
- `docs/managed-versioning-how-it-works.md`
  - 专讲 `managed_versioning` 什么时候生效、怎么传递、写路径怎么用
- `docs/why-managed-versioning-is-passed-three-times.md`
  - 专讲为什么 backend config、response、dataset runtime、commit API 多处都出现 managed-versioning 相关字段，以及它们的源码边界和传错后果
- `docs/namespace-low-level-write-fragments-commit-flow.md`
  - 专讲从 `connect(...)` 到 `dataset(...)`、再到 `write_fragments(...) + commit(...)` 的低层 namespace 透传链路

### 改造与路线选择文档

- `docs/namespace-interface-survey.md`
  - 系统盘点哪些 Lance 接口能接 namespace，哪些适合作为项目改造主路线
- `docs/namespace-retrofit-checklist.md`
  - 现有 Lance 项目改造成 namespace 接入时，应该按什么阶段推进
- `docs/namespace-create-update-index.md`
  - `create / update / index` 怎么和 namespace 结合
- `docs/namespace-native-vs-dataset-api.md`
  - namespace 原生 API 与“先打开 dataset 再走 Lance API”的差别

### 冲突、边界与验证文档

- `docs/namespace-conflict-scope.md`
  - 统一讲 namespace 能解决哪些冲突、哪些冲突仍由 Lance 原生层处理
- `docs/directory-namespace-conflict-detection.md`
  - 专讲 `DirectoryNamespace` 的 `put_if_not_exists` 能检测什么、不能检测什么
- `docs/external-manifest-commit-handler-boundary.md`
  - 专讲 `ExternalManifestCommitHandler` / `create_table_version(...)` 在 namespace-managed commit 里到底防什么、不防什么

### 示例与测试

- `examples/read_with_namespace.py`
- `examples/distributed_write_with_namespace.py`
- `examples/write_dataset_with_managed_versioning.py`
- `examples/write_fragments_append_with_managed_versioning.py`
- `examples/probe_namespace_managed_versioning.py`
- `examples/directory_namespace_conflict_matrix.py`
- `examples/create_table_with_namespace.py`
- `examples/update_with_namespace.py`
- `examples/create_index_with_namespace.py`
- `examples/external_manifest_commit_handler_boundary.py`
- `tests/test_external_manifest_commit_handler_boundary.py`
- `tests/test_namespace_portability_recovery.py`

## 这个仓库里的分布式写示例代表什么

`examples/distributed_write_with_namespace.py` 不是“真实多机部署”，但语义上就是：

- `CN`：先通过 namespace resolve 表上下文
- `DN`：各自写 fragment
- `CN`：收集 fragment，统一 commit

所以它代表的是：

> **带 namespace 感知的分布式写协议最小闭环**

而不是完整生产级运行时。

## 上游源码根目录

- `../_lance_src_v6.0.0`
- `../_lance_namespace_src_v0.7.6`
