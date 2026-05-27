# Lance Namespace 读表、分布式写与 Commit 冲突分析

这个仓库聚焦回答 3 个问题：

1. `read lance` 表时，`namespace` 到底怎么参与
2. `write_fragments(...)` 这种低层写接口，怎么和 `namespace` 对接
3. `commit` 时的冲突，究竟是 `namespace` 处理，还是 Lance 自己处理

分析基于以下固定版本：

- `pylance` / `lance`：`v6.0.0`
- `lance-namespace`：`v0.7.6`

## 仓库内容

- `docs/namespace-read-write-fragments.md`：主分析文档
- `docs/managed-versioning-how-it-works.md`：专讲 `managed_versioning` 什么时候生效、怎么传递、写路径怎么用
- `docs/namespace-create-update-index.md`：专讲 `create / update / index` 怎么和 namespace 结合
- `docs/namespace-native-vs-dataset-api.md`：专讲 namespace 原生 API 与“先打开 dataset 再走 Lance API”的区别
- `docs/directory-namespace-conflict-detection.md`：专讲 `DirectoryNamespace` 的 `put_if_not_exists` 能检测什么、不能检测什么，以及 append / overwrite / restore 的冲突语义
- `docs/namespace-conflict-scope.md`：统一讲 namespace 能解决哪些冲突、完整事务冲突矩阵怎么读、先后提交顺序会怎样，以及这些语义在接入 namespace 后是否变化
- `examples/read_with_namespace.py`：通过 `namespace` 读表的最小示例
- `examples/distributed_write_with_namespace.py`：`CN 规划 -> 多个 DN 写 fragment -> CN commit` 的简化示例
- `examples/write_dataset_with_managed_versioning.py`：高层 `write_dataset(...)` 写路径示例
- `examples/write_fragments_append_with_managed_versioning.py`：低层 `write_fragments + commit` 写路径示例
- `examples/directory_namespace_conflict_matrix.py`：用 stale writer 方式演示 append / overwrite / restore 的冲突矩阵
- `examples/create_table_with_namespace.py`：`create` 场景下，高层 Lance 写法和 namespace 原生 `create_table(...)` 对照示例
- `examples/update_with_namespace.py`：通过 namespace 打开 dataset 后做 `update / delete / merge_insert` 的最小示例
- `examples/create_index_with_namespace.py`：`ds.create_index(...)` 与 `ns.create_table_index(...)` 的对照示例

## 先给结论

### 1. 读表时

`namespace` 主要负责：

- 把 `table_id` 解析成真实 `table_uri` / `location`
- 返回 `storage_options`
- 告诉 Lance 是否启用 `managed_versioning`

也就是说，读路径里 `namespace` 是：

- 表定位器
- 存储配置提供方
- 版本管理策略提供方

### 2. `write_fragments(...)` 时

`write_fragments(...)` 不是“只给 `table_id` 就能自动找到表”的高层接口。

你仍然需要先拿到真实的 `table_uri`，通常流程是：

1. `declare_table(...)` 或 `describe_table(...)`
2. 从返回里拿到 `location`
3. 把这个 `location` 当成 `table_uri`
4. 再调用 `write_fragments(...)`

所以 `write_fragments(...)` 这条路里，`namespace` 主要不是负责“自动找表”，而是负责：

- 提供表上下文
- 提供存储参数
- 提供 credential refresh / versioning 上下文

### 3. commit 冲突到底谁处理

这里要分两层看：

- **如果 `managed_versioning=True`**：
  - **表版本发布冲突** 主要走 `namespace` 的 table version API
  - 也就是 `create_table_version` / `describe_table_version` / `list_table_versions` 这套语义
- **如果 `managed_versioning` 没开**：
  - 冲突处理走 **Lance 原生 commit 机制**
  - 不由 `namespace` 接管版本提交

所以更准确的说法不是“commit 冲突都由 namespace 处理”，而是：

> **当 namespace 宣告自己管理版本时，commit 阶段的版本发布冲突主要由 namespace 这一层处理；否则仍由 Lance 原生提交层处理。**

## 这个仓库里的分布式写示例代表什么

`examples/distributed_write_with_namespace.py` 不是“真实多机部署”，但它在语义上就是：

- `CN`：先通过 `namespace` 拿到 `table_uri`
- `DN`：各自调用 `write_fragments(...)` 写出 fragment
- `CN`：收集 fragment，最后统一 `commit`

所以它可以看成：

> **带 namespace 感知的分布式写协议最小闭环示例**

而不是完整的生产级分布式运行时。

## 上游源码根目录

- `../_lance_src_v6.0.0`
- `../_lance_namespace_src_v0.7.6`

## Mermaid 说明

这次我把图改成了 **GitHub 更稳的 Mermaid 写法**，做了这些收敛：

- 节点里不再塞太长的函数签名
- 尽量避免括号、逗号、斜杠混在节点文本里
- 改成短中文标签
- 复杂说明放到图外正文里，不挤进节点里

之前 GitHub 没渲染出来，大概率就是 Mermaid 解析器对复杂节点文本比较挑。
