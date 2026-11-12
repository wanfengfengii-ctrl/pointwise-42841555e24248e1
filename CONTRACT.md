# 分支湖表基线与讨论交付约定

## 已有仓库与入口
主模块 branchlake.py 只使用 Python 标准库与 SQLite。Lake(catalog, objects) 接收两个独立 SQLite 文件路径，默认各用 :memory:；不共享事务。samples.py 提供真实业务输入与 replay()，checks.py 提供执行入口。运行 python3 checks.py normal 核对业务回归；python3 checks.py trace 输出完整 JSON 时序证据；python3 checks.py demo 展示每个引用代次的实际逻辑净变化及孤立对象。正常回归必须保留。

Objects.put(rows) 按规范 JSON 的 SHA-256 去重并单独提交；get(id) 返回完整对象，缺失抛 MISSING_OBJECT。对象中的行格式为 [table,key,value]：table/key 是非空字符串，value 是 JSON 对象或 null；null 是整行删除，{} 是存在的空对象。一个发布批次内 (table,key) 不重复。行键跨表独立；值比较按 JSON 内容，字段顺序无意义。非有限数字会被 encode 拒绝。

Lake.ref(name) 返回 {"ref":name,"epoch":整数,"snapshot":整数}。rows(snapshot) 返回按 (table,key) 排序的 [table,key,value] 列表；read(name) 在返回的引用代次固定快照，增加 rows 字段。每个快照的 manifest 是有序对象列表，按列表顺序覆盖同键，删除不存在的键为空操作。parents 只记血缘，不参与读取。

Lake.publish(name,expected,request,rows,merge_parent=None,rewrite=False,fail_after_object=False) 返回引用回执。expected 是该引用的代次，不是快照号。publish 在对象库先写对象，再在 Catalog 的 BEGIN IMMEDIATE 事务里创建快照、更新引用和保存请求回执。rewrite=True 将当前状态加 rows 后完整重写物理对象，允许 rows=[]；merge_parent 只增加第二父血缘，调用者提供的 rows 才是显式合并决议，不能自动并入第二父的行。
Lake.fork(name,snapshot) 创建 epoch=0 的引用。Lake.reset(name,expected,request,snapshot) 将引用指向任一存在快照，即使目标等于当前快照，也增加引用代次；不创建快照。所有成功发布和 reset 都恰好增加该引用的 epoch 一次。不同引用的 epoch 不能比较。
请求号在整个 Catalog 唯一，绑定方法、引用、expected、业务参数的规范内容。重放先于代次判断，相同请求返回原回执；异内容抛 REQUEST_CONFLICT，新请求旧代次抛 STALE_EPOCH。旧回执不是当前 head。绑定和回执目前永久保存。fail_after_object=True 在对象已提交、Catalog 未开始时抛 AFTER_OBJECT，此参数不属于请求身份。

Lake.delta(before,after) 全量读取两个快照，再返回按 (table,key) 排序的 [table,key,before_value,after_value]；不存在用 null 表示，仅含值不同的键。它是现有小样本参照，不是可直接复用的增量订阅实现。当前没有订阅、租约、引用事件日志、逻辑键索引、到期或 GC。没有暗含的可供使用的外部消息系统。

## 样例的固定业务事实
INITIAL、CORRECTION、SIDE、MERGE、LATE 均在 samples.py。sample 表为采样，station 表为归属；此次同步不额外 JOIN 或聚合，两张表在一个引用代次中同步。
main 的 (epoch,snapshot) 依次为 (1,1)、(2,2)、(3,4)、(4,5)、(5,1)、(6,5)、(7,6)；side 为 (1,3)。
更正后 a.v=11，b 删除，c.v=30，east.region=south；重写后行不变，delta=[]。
合并后 a.v=12、c.v=30、d.v=40、east.region=merged，b 仍不存在；side 的 b 仍存在。
回退到快照1会恢复 b、删除 c/d，并恢复 a 和 east 的旧值；重新前进到快照5必须再次产生相反的真实变更。
旧 initial 请求在 main 已前进后仍返回 epoch=1 的原回执。
late 对象写入后失败时，main 保持 epoch=6，z 不可见，但对象库中已有孤立对象；同请求重试后 z.v=99，main 为 epoch=7。
normal 还核对冲突拒绝、非法重复键整批拒绝和两个 SQLite 镜像重新打开后的读取与回执。

## 文档范围与现有验证方式
DESIGN.md 是唯一方案交付，不实现新订阅服务、数仓执行器、GC 或故障注入框架。方案可提出后续表结构和内部实现改动，公开旧方法须保持兼容；不要求永久运行完全未升级的二进制。所有新增业务目标以题面为准。
先按源代码追踪 publish/reset/read/delta 的调用和跨库事务边界，解释哪些现成机制可复用、哪些不能，不能把全量 delta 当成在线实现。引用源码采用 branchlake.py:L行号、samples.py:L行号或 checks.py:L行号。
方案须比较至少两种可行策略，并用同一不变量解释取舍；源端、对象库、订阅端与目标 SQLite 的状态、提交边界及恢复步骤须可落实到语句级伪代码。明确初始补数、正常更正、物理重写、显式合并和任意引用回退之间的关系。
首次建立索引可扫描被接纳的存量；首次补数可读一次固定完整快照。题面的增量约束针对此后的每次推进，不禁止读取实际发生改变的所有键。数据值需要从真实载荷得到，不能只靠摘要声称精确前后像。新增订阅起点已过期时可明确拒绝，已接纳且未确认的订阅不能被暗中跳过。
以下 A—F 是题面规则的具体交错，用于逐项审查，所有引用目标和输入均来自样例；讨论尚未实现的动作时须明确标为设计推演，不伪造基线输出：
A. 首次固定 main 的 epoch=1 补数，期间 correct、rewrite、merge 继续提交；补数分页之间发生发布，随后追赶到 epoch=4。
B. side 的发布先于 main 的 rewrite、merge；比较两个分支的 b，再解释 main 的重写零变化及合并结果。
C. main 经过 epoch=4 -> 5 -> 6，快照号为 5 -> 1 -> 5；订阅者已取到第5代部分页，另一请求重放 initial 原回执，随后继续拉取。
D. 第6代最后一页入目标数仓，目标事务提交后响应丢失；源确认也可能已提交但响应丢失，租约接替，旧工作者带原分页和确认恢复。
E. 在线建立索引时，correct 与 reset 竞争；对象已写未发布与 Catalog 已发布但索引未追平都可能中断。交接后必须解释已提交代次的发现和续建。
F. main 回退后历史快照申请到期；一位订阅者已确认第6代，另一位仍停在第4代且持第5代部分页；side 仍在使用共享对象。late 在对象提交后中断，此时回收器运行，随后 late 重试；必要保护最终解除后，讨论哪些载荷实际可删除。分别说明已可见状态和未来恢复所需状态。
时序中的持久化中断、重试、并发胜负须从设计的原子点推导，不接受只写“恰好一次”“加锁”或“用事务”。
文档至少说明：所有读到的分页属于哪个稳定代次；完整入仓的唯一身份；旧工作者能执行哪些操作；确认与目标提交的先后关系；在线索引交接的完整性条件；载荷保护何时生效和何时解除。给出这些结论的安全性论证、有限发布后的追平条件，以及按活跃状态描述的空间与访问上界。
不能用停掉全部发布、保留全部历史载荷、逐轮全量比较快照或永久拒绝落后订阅替代这些推导。

## 可重跑证据
在 DESIGN.md 的一个 json 围栏内嵌入对象，包含 baseline_trace 和 decisions。
baseline_trace 必须逐值等于 python3 checks.py trace 的实际 JSON 输出，不预先填充答案。
decisions 为对象，键 A、B、C、D、E、F 的值分别是对应交错的实质结论字符串。结论须定位文档中的状态和原子点；检查器只确认字段存在，人工复核其推理。
运行清单最终 verify 改为 ["python3","checks.py","design"]，demo 保持 ["python3","checks.py","demo"]。
design 会重跑原正常回归，核对 baseline_trace 的全部实际输出及代码行号范围，不会把报告结构通过当成协议正确性证明。
保留 branchlake.py、samples.py、checks.py 的源码与原断言。允许修改的已有文件只有 .pointwise-runtime.json；新增 DESIGN.md。
