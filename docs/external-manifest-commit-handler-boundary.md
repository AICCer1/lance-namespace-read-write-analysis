# ExternalManifestCommitHandler 到底在防什么：边界、作用与实验

## 版本范围

- `pylance` / `lance`：`v6.0.0`
- `lance-namespace`：`v0.7.6`

---

## 1. 先回答你的问题

你问的这条链路是：

- `write_fragments(...)`
- `LanceDataset.commit(...)`
- `managed_versioning=true`
- 安装 `ExternalManifestCommitHandler`
- 写 staging manifest 到 object store
- 调 `create_table_version(...)`
- 再 finalize 到标准 `_versions/{version}.manifest`

这整套流程**不是为了提供“严格 read_version 乐观锁”**。

它主要是在防：

> **当多个 writer 要发布同一个 target version 时，如何在“版本发布占位”这一层做原子 CAS，避免同一个版本号被重复发布。**

以及：

> **当 object store 本身不具备足够原子的 rename-if-not-exists / put-if-not-exists 语义时，如何借助外部 manifest store 完成并发 commit。**

如果更直白一点说：

- 它防的是：**version publish race**
- 它不是在防：**所有 stale writer 都失败**

这个边界非常关键。

---

## 2. `ExternalManifestCommitHandler` 的设计目标是什么

官方事务文档写得很直白：

> If the backing object store does not support atomic operations (rename-if-not-exists or put-if-not-exists), an external manifest store can be used to enable concurrent writers.

也就是：

> **如果底层对象存储不支持足够强的原子条件写，Lance 就引入 external manifest store 来支撑并发 writer。**

对应文档：

- `../_lance_src_v6.0.0/docs/src/format/table/transaction.md`

所以这层的第一目标不是“替代 Lance 全部事务语义”，而是：

> **给 manifest 发布这一跳，提供一个外部的、可做 put-if-not-exists 的真 CAS 点。**

---

## 3. 为什么要 staging manifest + create_table_version + finalize

因为 external manifest store 不是用来替代 object store 保存 manifest 内容的。

它只是在并发提交时，充当：

- “版本占位裁判”
- “latest version 真相来源”
- “final manifest 路径指针”

manifest 内容本身最终还是要落回 object store，这样表才可移植、可离线复制。

所以设计才会拆成这几步。

---

## 4. 这条链路做了什么

## 4.1 官方 external manifest 协议

事务文档里的四步是：

1. **Stage manifest**
   - 先把新 manifest 写到一个带 UUID 的 staging 路径
   - 例如：`_versions/{version}.manifest-{uuid}`

2. **Commit to external store**
   - 把“这个 version 对应 staging manifest 路径”原子写进 external manifest store
   - 这个 `put-if-not-exists` 一旦成功，**commit 在逻辑上就已经成立**

3. **Finalize in object store**
   - 把 staging manifest 拷到标准 final path
   - 例如：`_versions/{version}.manifest`

4. **Update external store pointer**
   - 把 external store 里的路径，从 staging path 更新成 final path

对应文档：

- `../_lance_src_v6.0.0/docs/src/format/table/transaction.md`

---

## 4.2 `ExternalManifestCommitHandler` 自己怎么描述

`ExternalManifestCommitHandler` 注释里直接写了：

- staging manifest 一旦写到 object store
- 且那个 staging path 被 external store 原子认领
- 这个 manifest 就可以被视为“已经 commit”

后面的：

- copy 到 final path
- external store 指针切到 final path
- 删除 staging manifest

这些是**完整收尾**，但逻辑上的 commit 点已经发生在“外部存储成功占位”那一步。

对应源码：

- `../_lance_src_v6.0.0/rust/lance-table/src/io/commit/external_manifest.rs`

---

## 5. namespace 接进来之后，external manifest store 是谁

当 `managed_versioning=true` 时，Lance 不再用默认 object-store-native 的 commit handler，而是会装上：

- `ExternalManifestCommitHandler`

但此时它背后的 external store，不是 DynamoDB 之类，而是：

- `LanceNamespaceExternalManifestStore`

对应源码：

- `../_lance_src_v6.0.0/rust/lance/src/io/commit/namespace_manifest.rs`

这个适配层最关键的一句话是：

> `create_table_version reads staging manifest and writes to final location`

也就是说，在 namespace-backed 这条路里，真正的“外部 manifest store put”不是默认实现那一套 `put_if_not_exists/put_if_exists` 两段式，而是**直接委托给 namespace 的 `create_table_version(...)`**。

---

## 6. 所以 `create_table_version(...)` 在 namespace 路里到底干嘛

它做的不是：

- 检查“你是不是从某个 old read_version 出发”
- 检查“latest version 现在是不是还等于你最初读到的 old version”

它做的是：

> **我要发布 version=N；如果 version=N 已经有人发布过了，那你这次就不能再发。**

对 `DirectoryNamespace` 而言，关键实现是：

- 根据 `version` 算 final manifest path
- 对 final path 做 `copy_if_not_exists(...)`
- 不支持时 fallback 到 `PutMode::Create`
- 如果 final path 已存在，则报并发修改 / 已存在错误

对应源码：

- `../_lance_src_v6.0.0/rust/lance-namespace-impls/src/dir.rs:2791+`

所以这层 CAS 点，本质上是：

> **“这个目标 version 的 final manifest 文件路径是否已经被别人占了”**

不是：

> **“当前 latest 是否仍等于我当初读到的 read_version”**

---

## 7. 它到底防住了什么

## 7.1 防住：同一个 target version 的重复发布

这是它最核心、最明确的保护。

例如两个 writer 都想发布：

- `version = 2`

那么 namespace / external manifest 这一层会确保：

- 第一个能占到 `version=2`
- 第二个再来发 `version=2` 会失败

也就是说它防的是：

> **duplicate version claim**

这就是 `put-if-not-exists` / `copy_if_not_exists` 语义真正起作用的地方。

---

## 7.2 防住：对象存储自身不够原子时的 manifest publish race

如果底层 object store 本身不支持足够强的条件写，那么单靠直接往 `_versions/{version}.manifest` 写文件，不足以安全完成并发 commit。

external manifest store 的意义，就是把“这次发布 version=N 的胜负判定”挪到一个**更可靠的 CAS 层**。

也就是：

- object store 负责存内容
- external store / namespace 负责判“谁拿到了这个 version”

---

## 7.3 防住：writer 在 commit 途中死掉时，manifest 完全丢失

官方协议里一个很重要的点是：

- 如果 writer 在“staging path 已写入、external store 已认领”之后挂了
- 读者或后续 writer 可以继续把 staging manifest finalize 到 final path

也就是说：

- commit 的“真相”先写到 external store
- object store 的标准 final manifest 可以延后补齐

这个设计是为了：

> **既能并发 commit，又不让表的最终可移植性依赖 external store 永远在线。**

---

## 8. 它没有防住什么

## 8.1 没有防住：strict read_version CAS

这层**没有**提供下面这种严格语义：

> “我最初读到的是 version=10；只有当前 latest 还是 10，才允许我提交；否则一律失败。”

原因很简单：

`CreateTableVersionRequest` 里没有类似：

- `expected_current_version`
- `if_latest_version_equals`

这种字段。

它只有：

- `version`
- `manifest_path`
- `manifest_size`
- `e_tag`
- `naming_scheme`

所以 namespace 这一层表达的是：

> **“我要创建 version=N”**

不是：

> **“我要在 latest 仍等于 M 的前提下创建 N”**

---

## 8.2 没有防住：append vs append 的 stale writer 都成功

这正是很多人最容易误解的地方。

假设：

- writer A 和 writer B 都从同一个旧版本出发
- A 先 append 成功
- B 再 append

在 Lance 的事务语义里，append vs append 本来就是兼容的；后提交者可以 rebase 后继续提交到下一个版本。

所以你会看到：

- A 成功发到 version 2
- B 没被 external manifest handler 拦掉，而是 rebase 后发到 version 3

这不是 external manifest store 失效，而是：

> **它本来就不是拿来阻止这种兼容事务的。**

---

## 8.3 没有防住：所有 stale writer 一律失败

更准确的说法应该是：

- **namespace / external manifest handler** 负责版本发布占位冲突
- **Lance conflict resolver / rebase** 负责事务语义冲突

所以最后的写结果，要同时看：

1. 版本号占位是否冲突
2. 事务语义是否兼容
3. 是否允许 rebase

而不是只看 namespace 这一层。

---

## 9. 这次专门补了一个实验脚本

见：

- `examples/external_manifest_commit_handler_boundary.py`

这个脚本就做两件事。

---

## 9.1 case 1：没防住的

### 现象

- 两个 stale append writer 都从同一个 `read_version` 出发
- 第一个 append 提交成功
- 第二个 append 也成功

### 说明什么

说明：

- `ExternalManifestCommitHandler`
- `create_table_version(...)`
- `managed_versioning=true`

**并不等于 strict read_version CAS**。

它没有把第二个 stale append 一刀砍掉。

原因不是保护失效，而是：

- append vs append 在 Lance 语义里本来可兼容
- 后提交者允许 rebase 后发新版本

---

## 9.2 case 2：防住了的

### 现象

- 准备两个不同 staging manifest
- 都尝试发布成同一个 `target version=2`
- 第一个 `create_table_version(version=2, ...)` 成功
- 第二个再发 `version=2` 失败

### 说明什么

说明 namespace / external manifest 这层真正防住的是：

> **同一个 target version 的重复发布。**

这就是它的核心保护边界。

---

## 10. 这两个实验为什么正好回答你这个问题

因为你问的不是“Lance 全局事务到底怎么冲突”，而是更具体地问：

> `write_fragments + commit` 走到 `ExternalManifestCommitHandler` 之后，这层到底在防什么？

这两个实验恰好把边界钉死了：

### 没防住的实验告诉你

它**不是**在防：

- stale append writer 一律失败
- 所有 read_version 过期写都失败

### 防住了的实验告诉你

它**是在防**：

- 两个人抢同一个 target version 的发布占位
- object store 不够原子时的 manifest publish race

---

## 11. 再补一个你提的关键对比：不用 namespace 时，同一个 version 争抢会怎样

这个问题非常关键，而且答案是：

> **在支持原子条件写的默认 commit handler 上，不用 namespace 时，两个 writer 抢同一个 target version，也应该只有一个成功。**

也就是说：

- `namespace` **不是**“让第二个失败”的唯一来源
- Lance 默认 commit handler 本来就要保证：
  - 同一个 next version 只能有一个 winner

官方 commit 抽象本身就写了：

> Commit implementations ensure that if there are multiple concurrent writers attempting to write the next version of a table, only one will win.

对应源码：

- `../_lance_src_v6.0.0/rust/lance-table/src/io/commit.rs`

而默认本地/常见对象存储路径，通常会选：

- `RenameCommitHandler`
- 或 `ConditionalPutCommitHandler`

它们本质上也是在 final manifest path 上做：

- `rename_if_not_exists`
- 或 `PutMode::Create`

所以如果你把场景收窄成：

> **两个 writer 都要抢同一个 target version=2，而且禁止 retry/rebase 到 version=3**

那么：

- **不用 namespace**：第二个也应该失败
- **用了 namespace**：第二个也应该失败

两边的差别不在“是否拦住 duplicate version=2”，而在：

- **不用 namespace**：CAS 点在 object store final manifest path
- **用了 namespace**：CAS 点在 external manifest / `create_table_version(...)`

换句话说，真正该对比的是：

### 不用 namespace

- 依赖 object store 自己支持足够强的原子条件写
- 第二个 writer 抢同一个 version，靠默认 commit handler 拦

### 用 namespace + managed_versioning

- 把 CAS 点外提到 namespace / external manifest store
- 第二个 writer 抢同一个 version，靠 `create_table_version(...)` 拦
- 这对“不支持原子条件写的 object store”尤其重要

---

## 12. 对应测试脚本现在已经补成 3 个视角

`tests/test_external_manifest_commit_handler_boundary.py` 现在覆盖：

1. **不用 namespace**：两个 writer 都想占同一个 target version=2，第二个失败（通过 `max_retries=0` 固定在 strict 对比语义）
2. **用 namespace，默认 retry 打开**：stale append -> append 最终可变成 version 2 / version 3，说明它不是 strict read_version CAS
3. **用 namespace，直接测 `create_table_version(version=2)` 重复发布**：第二个失败，说明 external manifest 这一层确实在 version claim 处做保护

这样就能把：

- “默认 Lance commit handler 已经能防什么”
- “namespace external manifest 额外把 CAS 点搬到了哪”
- “它并不等于 strict stale writer 阻断”

这三件事分开看清楚。

---

## 13. 最后一句话结论

如果只用一句话概括：

> **`ExternalManifestCommitHandler` + namespace-managed `create_table_version(...)` 的主要作用，是把“manifest 发布 version=N”这一步变成一个外部可做 CAS 的原子占位动作；它防的是重复发布同一个 target version，而不是提供基于 `read_version` 的严格乐观锁。**

所以：

- **同 version 重复发布**：这层能防
- **stale append 全部失败**：这层不负责，很多情况下也不会防

这就是它的真实边界。
