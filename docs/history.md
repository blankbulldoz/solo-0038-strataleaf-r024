# 历史差异与提交定位

两种互补的观察入口，均由 `Database.history` 提供：

- `history.diff(collection, since=None, until=None, limit=100, cursor=None)`：内容差异。比较两个读版本 `since` 与 `until` 之间可见文档的**端点结果**，按文档键升序分页。
- `history.commits(since=None, until=None, collection=None, key=None, limit=100, cursor=None)`：操作历史。列出同一区间内触及指定集合和/或文档键的提交，以及匹配到的操作。用于回答“差异从哪几次提交来”。

区间是半开区间 `(since, until]`：`since` 版本本身的写入视为起点状态，不重复计入差异。`since=None` 从当前保留水位 floor 开始；`until=None` 固定在调用时的 head，并在后续分页中保持该值。

## 内容差异的判定口径

`DocumentChange.change` 只根据两个端点分类：

| change | 端点前 | 端点后 | 说明 |
| --- | --- | --- | --- |
| `added` | 不存在 | 存在 | 净新增；即使区间内先建后删再建也归为 `added` |
| `modified` | 存在 | 存在 | 端点内容或到期时间不同，且区间内没有删除墓碑 |
| `deleted` | 存在 | 不存在 | 端点已删除（含到期清理产生的删除提交） |
| `recreated` | 存在 | 存在 | 区间内出现过删除墓碑又被写入，即使最终内容与起点相同 |
| `reverted` | 存在 | 存在且端点相同 | 区间内有写入但净效果为零，内容已回到起点 |
| `reverted` | 不存在 | 不存在 | 区间内创建后又删除；`writes`/`deletes` 仍描述这些版本 |

关键点：最终内容回到原值不会被当作“无变化”而静默丢弃。此类键仍出现在结果中：`net_changed=False`、`document_changed=False`，`change` 为 `reverted`（若中间有墓碑则为 `recreated`），并通过 `writes`/`deletes` 计数说明区间内确实发生过版本写入。用户据此可以区分“完全没碰过”和“改过又改回来”；后者还会在 `history.commits` 中看到对应提交。`expiry_changed` 单独标注到期时间是否变化；到期时间变化也会使 `net_changed=True` 并归类为 `modified`。`before`/`after` 是两端点的 `Record`（不存在时为 `None`），直接来自 `Database.get` 相同的可见性规则：墓碑隐藏当前值，历史保留旧值。区间内完全没有版本写入的键不会出现在结果中——它既不是 `reverted`，也不带 `writes`/`deletes` 计数；结果只覆盖“被触及过的键”。

## 分页稳定性

差异游标签名并携带数据库身份、集合、`since`、`until` 与最后文档键。后续页复用游标里的两个版本：

- 分页期间的新提交只会产生大于 `until` 的修订，不可能进入已固定区间的结果集。
- 分页期间显式调用 `retention.compact` 把 floor 推过 `since` 时，下一页明确失败 `SnapshotTooOld`，而不是返回一个悄悄缩小的集合。
- 换集合或换端点复用游标失败 `InvalidCursor`。

`history.commits` 同样把区间与过滤条件固定在签名游标中。

## 起点已回收

`since` 低于当前 floor 时：

- 显式传入旧版本：抛 `SnapshotTooOld`，错误信息提示改用 `since=None`。
- 传 `since=None`：自动从当前 floor 开始，并对 floor 版本建立起点基线，因此区间内各键的 `before` 仍然可解释。
- 分页游标里的 `since` 在后续被回收：下一页抛 `SnapshotTooOld`，调用方需以新水位重新开始。

回收只删除被更新版本覆盖的旧版本并保留每个键的基线版本，所以 `since=None` 从 floor 出发时，floor 之前的最后状态仍可作为 `before` 使用。

## 内容差异与操作历史的关系

- 内容差异是**结果**：只回答两个读版本之间每个键的净变化，不解释中间步骤。一个 `reverted` 键在内容上不可见，但在操作上可能有多次提交。
- 操作历史是**过程**：按提交列出匹配的操作，`operation_count` 始终是该提交的完整操作数，`operations` 只是按集合/键过滤后的投影。过滤后的提交视图不能被误读为部分提交。
- 两者不是互相推导关系：没有触及某集合的提交也可能改变目录 epoch；被过滤器排除的操作仍与匹配操作同属一个原子提交。要证明内容差异，仍以 `diff` 的端点和 `Database.get(revision=...)` 为准；要理解成因，用 `commits`。
- 内容差异不比较模式或索引定义。模式与目录事实由 `Database.catalog` 与 `Database.diagnostics` 观察。

## 边界

- `diff` 要求集合存在；`commits` 在给出集合过滤时也要求集合存在，但可以只按 `key` 过滤（跨集合）。
- 页大小 1–1000；提交和差异都是按键/修订升序的稳定游标。
- 差异不自动注册保留点。长时间跨页时需要稳定结果，调用方应先 `retention.pin`。
