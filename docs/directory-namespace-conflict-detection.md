# DirectoryNamespace 冲突检测：`put_if_not_exists` 能管到哪一层

## 版本范围

- `pylance` / `lance`：`v6.0.0`
- `lance-namespace`：`v0.7.6`

---

## 1. 先给结论

如果你用的是：

- `DirectoryNamespace`
- `table_version_tracking_enabled=true`
- `managed_versioning=true`

那么 `DirectoryNamespace` **确实支持版本发布层的冲突检测**，但它检测的是：

> **“目标 version 是否已经被别人占掉了”**

也就是 `create_table_version(...)` 这一层的：

> **`put_if_not_exists` / `copy_if_not_exists` 语义**

它**不等于**：

> **“只要两个 writer 都从相同 `read_version` 出发，后来的那个就一定失败”**

后面这种更完整的语义，不是 `DirectoryNamespace` 单独完成的，而是要结合 **Lance commit + TransactionRebase + conflict_resolver** 一起看。

---

## 2. `DirectoryNamespace` 自己到底检测什么

`DirectoryNamespace::create_table_version(...)` 的关键路径是：

- 先算出目标 manifest 的最终路径
- 再对这个最终路径做 `copy_if_not_exists(...)`
- 如果底层 store 不支持，再 fallback 到 `PutMode::Create`
- 如果目标已存在，就返回并发修改错误

也就是说，它的 CAS 点是：

> **最终 manifest 文件路径是否已存在**

而不是：

> **当前表的 latest version 是否仍然等于 writer 当初读到的 old version**

对应源码：

- `rust/lance-namespace-impls/src/dir.rs:2791-2890`

其中冲突分支非常直接：

- `AlreadyExists`
- `Precondition`

都会被映射成：

> `Version X already exists for table ...`

---

## 3. 上游已经有 `put_if_not_exists` 冲突测试

有，已经有一个很直接的测试：

- `rust/lance-namespace-impls/src/dir.rs:test_create_table_version_conflict`

测试做的事很朴素：

1. 先创建表
2. 准备 staging manifest
3. 第一次 `create_table_version(version=2, ...)` 成功
4. 第二次再创建同一个 `version=2`
5. 预期失败

源码位置：

- `rust/lance-namespace-impls/src/dir.rs:7685-7801`

所以如果你问：

> **DirectoryNamespace 对“同一个目标 version 被重复发布”有没有检测？**

答案是：

> **有，而且上游已经有测试覆盖。**

---

## 4. 但这还不是“完整乐观锁”

`CreateTableVersionRequest` 里，核心字段是：

- `version`
- `manifest_path`
- `manifest_size`
- `e_tag`
- `naming_scheme`

它没有“expected_current_version”这类字段。

所以 namespace 这层表达的是：

> **我要发布 version=N；如果 N 已经存在就失败。**

它表达不了：

> **我当初读的是 version=M；只有当前 latest 仍然是 M，才允许我发新版本。**

所以：

- **精确版本号占位冲突**：`DirectoryNamespace` 能管
- **基于 `read_version` 的完整陈旧写检测**：不能只靠 `DirectoryNamespace`

---

## 5. 写场景里的冲突，真正还要看 Lance 的 conflict resolver

在 `managed_versioning=true` 下，最终 manifest publish 走 namespace。

但**语义层面的写冲突**，还要看 Lance 自己的：

- `TransactionRebase`
- `conflict_resolver`

关键文件：

- `rust/lance/src/io/commit.rs`
- `rust/lance/src/io/commit/conflict_resolver.rs`

也就是说：

> **namespace 管“版本发布占位”，Lance 管“事务语义冲突”。**

---

## 6. `append vs overwrite`、`append vs restore` 是对称互斥吗？

**不是对称的。**

这是最容易看错的一点。

Lance 判断冲突时，是用：

> **“当前正在 rebase 的事务”** 去看 **“已经提交的其他事务”**

所以结果是**有方向性的**。

---

## 7. 冲突矩阵：重点看“后提交的人是谁”

下面这个表，假设两个 writer 都从同一个旧版本出发，但第一个先提交完成；第二个提交时会看到第一个已经成了“已提交事务”。

| 第二个提交的操作 | 第一个已提交的操作 | 结果 | 说明 |
| --- | --- | --- | --- |
| append | append | 可成功 | append 对 append 是兼容的，后者可 rebase 后提交成新版本 |
| append | overwrite | 失败 | append 看见 overwrite，会报 incompatible conflict |
| append | restore | 失败 | append 看见 restore，会报 incompatible conflict |
| overwrite | append | 可成功 | overwrite 看见 append，允许继续提交；后者是“更新的覆盖版本” |
| restore | append | 可成功 | restore 看见 append，允许继续提交；后者是“更新的恢复版本” |

对应源码：

- `append` 当前事务检查：`rust/lance/src/io/commit/conflict_resolver.rs:873-899`
- `overwrite` 当前事务检查：`rust/lance/src/io/commit/conflict_resolver.rs:825-871`
- `restore` 当前事务检查：`rust/lance/src/io/commit/conflict_resolver.rs:1015-1039`

所以这里真正的结论是：

> **append 是不能跟“已经提交的 overwrite / restore”并存的。**

但反过来：

> **overwrite / restore 作为后提交者，是可以压在 append 后面再形成一个新版本的。**

这不是 bug，而是事务语义本来就这样设计的。

---

## 8. 上游对并发 append 也有测试

有一个非常关键的测试：

- `rust/lance/src/io/commit.rs:test_concurrent_writes`

测试注释写得很直白：

> `Test concurrent appends - all should succeed`

源码位置：

- `rust/lance/src/io/commit.rs:1439-1492`

所以如果你看到：

- A 和 B 都从同一个旧版本出发
- A append 成功
- B append 之后也成功

这**通常不是锁坏了**，而是：

- append 和 append 本来就被视为兼容
- B 可以在 rebase 后提交到下一个版本

---

## 9. 这个仓库里新增了一个最小示例

见：

- `examples/directory_namespace_conflict_matrix.py`

这个示例不强依赖“真并发时序”，而是用更稳定的方式来模拟：

> **两个 stale writer 都从同一个 base version 打开，然后按不同顺序提交。**

这样更容易把“版本发布冲突”和“事务语义冲突”分开看清楚。

示例覆盖：

- append -> append
- overwrite -> append
- append -> overwrite
- restore -> append
- append -> restore

---

## 10. 最后一句话总结

如果只问：

> **DirectoryNamespace 的 `put_if_not_exists` 有没有冲突检测？**

答案是：

> **有，但它只保证“同一个目标 version 不会被重复发布”。**

如果你问：

> **它能不能单独提供完整的、基于 `read_version` 的乐观锁语义？**

答案是：

> **不能，写冲突最终还得结合 Lance 的 rebase / conflict resolver 语义一起看。**
