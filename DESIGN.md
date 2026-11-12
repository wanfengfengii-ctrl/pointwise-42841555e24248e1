# 分支湖 → 独立 SQLite 数仓：多订阅持续同步设计

> 交付边界：本文是唯一方案交付，不实现订阅服务、数仓执行器、GC 或故障注入框架（CONTRACT.md 第 27 行）。所有尚未实现的动作均标注【设计推演】。`branchlake.py`、`samples.py`、`checks.py` 的源码与原断言保持不变；运行清单仅改 `.pointwise-runtime.json` 的 verify。

## 1. 基线代码追踪：现成机制、跨库边界与不可复用项

### 1.1 两个独立 SQLite，无跨库事务

- 对象库 `Objects` 在独立连接上建表 `blobs(id PRIMARY KEY, body)`（branchlake.py:L18-L19），`put` 按规范 JSON 的 SHA-256 内容寻址，`INSERT OR IGNORE` 后回读校验碰撞（branchlake.py:L22-L28），**每次 put 是自动提交的独立事务**；`get` 缺失抛 `MISSING_OBJECT`（branchlake.py:L30-L35）。
- Catalog 在另一个连接上，表为 `snapshots`、`refs`（外键指向 snapshots）、`requests`，并预置 sid=0 空快照与 `main epoch=0`（branchlake.py:L44-L55）。
- `publish` 的顺序是：**先写对象并已提交**（branchlake.py:L162），随后才进入 Catalog 的 `BEGIN IMMEDIATE` 事务 `_install`（branchlake.py:L96-L114，调用点 branchlake.py:L166）。两个库之间没有、也不可能有共享事务。`fail_after_object` 制造的正是"对象已提交、Catalog 未开始"的崩溃窗口（branchlake.py:L163-L164），基线用它产生孤立对象（samples.py:L37-L40）。

### 1.2 Catalog 事务内的判定顺序（可直接复用的幂等模式）

`_install`（branchlake.py:L95-L118）在一个 `BEGIN IMMEDIATE` 内严格按序执行：

1. 请求重放先于代次判断：命中相同 binding 直接提交并返回原回执（branchlake.py:L98-L101）；`receipt` 对异内容抛 `REQUEST_CONFLICT`（branchlake.py:L87-L93）。
2. 事务内重读 head，epoch 不符抛 `STALE_EPOCH`（branchlake.py:L102-L104）。
3. 插入快照行（branchlake.py:L107-L109）、更新 ref 的 epoch/sid（branchlake.py:L110-L112）、写请求回执（branchlake.py:L113），同一事务 `COMMIT`（branchlake.py:L114）；任何异常 `ROLLBACK`（branchlake.py:L116-L117）。

由此得到两个可复用事实：**ref 推进与回执是同一个原子点**；**请求号重放先于一切判断**，订阅协议的确认接口将复刻这一模式。

### 1.3 publish / rewrite / merge / reset 的物理语义

- 普通发布：manifest 追加新对象 oid（branchlake.py:L165），新对象只含本批 rows；同键按 manifest 列表顺序覆盖、删除不存在的键为空操作（branchlake.py:L73-L81 的折叠规则；CONTRACT.md 第 8 行）。
- 物理重写：`rewrite=True` 先完整物化当前状态再叠加 rows，重写成一个**全新的单对象**，manifest 变为 `[新oid]`，允许 rows=[]（branchlake.py:L152-L159, L165）。基线证明重写后行不变、`delta=[]`（checks.py:L22-L23），重写本身付出了一次全状态读取（samples.py:L28-L30 测得 `rewrite_payload_reads=2`）。
- 显式合并：`merge_parent` 只经 `manifest()` 校验存在并追加第二父血缘（branchlake.py:L149-L151），**调用者给的 rows 才是决议**，第二父的行不会自动并入。基线合并结果与父血缘 `[4,3]` 见 checks.py:L24-L30。
- `reset` 不创建快照，可把 ref 指向任一存在快照；即使目标等于当前 sid 也走 `_install`，epoch 照样 +1（branchlake.py:L168-L174）。所以 main 的 sid 序列出现 5→1→5 而 epoch 是 4→5→6（checks.py:L14-L15），回退与重新前进各自产生真实反向变更（checks.py:L39）。
- 所有成功 publish/reset 恰好令该 ref 的 epoch +1（branchlake.py:L110）；不同 ref 的 epoch 不可比较。

### 1.4 不可复用项：`delta` 为什么不能当在线实现

`delta` 对两个快照分别调用 `rows()` 全量物化、在内存里按键集合并比较（branchlake.py:L176-L181），而 `rows()` 要读取 manifest 上的**全部对象**并折叠（branchlake.py:L73-L81）。重写快照的 manifest 不与祖先共享前缀（branchlake.py:L165 的 `[oid] if rewrite`），对它做 delta 必然全量。CONTRACT.md 第 14 行明确它只是小样本参照。在线订阅若每轮调它，正好违反"不能逐轮全量比较快照"。可复用的是它的**前后像四元组语义** `[table,key,before,after]`（不存在为 null、仅含值不同的键），本设计把这套语义固化为持久台账，而不是复用其全量算法。

### 1.5 已验证的对象共享与孤立对象事实

对基线库实际读取 manifest 得到：sid 1/2/3 共享对象 `1ab0321b…`；sid 4/5 共享 `b0a478d…`；sid 6 的 manifest 为 `[b0a478d…, d0302a1…, ce04ca…]`——`ce04ca…` 正是首次 late 失败后记录在 `orphan_ids` 里的那个对象（checks.py:L43-L44），重试时 `put` 同内容命中同一 oid（branchlake.py:L25 的 INSERT OR IGNORE），发布成功后它已被 sid 6 正常引用，不再孤立。这是后文 GC 自愈与共享保护的事实基础。

## 2. 统一不变量（策略比较与全部正确性论证的同一标尺）

- **I1 代次确定**：每个 ref 的 epoch 严格单调，每次成功 publish/reset 恰好 +1（branchlake.py:L110-L114）。订阅身份只认 `(subscription, ref, epoch)`，绝不以 sid 为序。
- **I2 变更集确定**：对每个 `(ref, epoch=e)` 存在唯一确定的精确前后像集合
  `Δe = {(table,key,before,after) | before ≠ after}`，由 epoch e-1 与 epoch e 两个引用状态决定；null 表示不存在或整行删除。物理形态（追加/重写/第二父血缘）不影响 Δe：重写可使 Δe 为空，reset 到旧快照使 Δe 非空且方向相反。
- **I3 真实载荷**：Δe 中的值只能来自 `Objects.get` 返回的真实 JSON（branchlake.py:L30-L35）；哈希仅用于寻址与完整性校验，不能用摘要冒充前后像。
- **I4 原子且幂等入仓**：一代的所有页在目标库要么全可见、要么全不可见；`(sub,ref,epoch)` 是入仓唯一身份，任何重放的净变化为零。
- **I5 保留安全**：已接纳订阅未完成的代次、未确认分页与租约、活跃 ref 快照、索引构建水位、在途发布宽限所需要的对象，一律不可回收；保护解除后才允许物理删除，且删除分阶段、崩溃可重入。
- **I6 无跨库事务**：Objects、Catalog、订阅元数据库（substore）、目标数仓（warehouse）是四个独立 SQLite（前两个沿用基线分库），一致性只靠"提交顺序 + 幂等重放 + 对账"，不引入分布式事务。

## 3. 两种可行策略的比较（用 I1–I6 同一标尺）

### 策略 A：源端代次变更台账 + 按代次分页订阅（选定）

在 Catalog 内新增逻辑键台账，于 ref 推进的**同一事务**内写入 Δe（复用 `_install` 的原子点，branchlake.py:L96-L114）。页是台账行的确定性、不可变切片；warehouse 按代次原子 apply。

### 策略 B：订阅端拉相邻快照自行 diff（落选，保留为对照）

源端只暴露现有 `manifest`/`rows`，订阅者对相邻 sid 自行实现 branchlake.py:L176-L181 式 diff。

| 不变量 | 策略 A | 策略 B |
|---|---|---|
| I2 | Δe 在代次创建时一次性固化，所有订阅者共享同一份 | 每个订阅者重复计算；rewrite 快照 manifest 无前缀共享（branchlake.py:L165），必然全量物化，违反增量约束 |
| I4 | 页为不可变台账切片，重复拉取字节相同，天然幂等 | diff 在订阅端临时生成；分页期间源快照若到期、对象被 GC（题面允许历史到期），缺页无法重建 |
| I5/空间 | 保留单位是订阅水位与在途页，可按水位实际回收 | 每个落后订阅者都要保住其涉及的全部快照/对象，等价于永久留全历史，被题面禁止 |
| I1/ABA | 身份是 epoch，sid 5→1→5（checks.py:L15）天然无碍 | 以 sid 对为序会把重复出现的 sid=5 混同，必须再挂 epoch 标注，退化成 A |
| 代价 | 需在线建台账/索引（新增表，旧方法签名与行为不变） | 源端零新状态，但把全量成本转嫁给每个订阅者，且无法支持到期回收 |

结论：**选 A**。B 仅在"台账尚未建好"的短暂窗口作为源端内部兜底（对未就绪代次返回 RETRY_LATER，绝不交付不完整页），不对外作为协议。新增订阅起点已过期时 A 在注册环节明确拒绝（CONTRACT.md 第 30 行）；已接纳且未确认的订阅由 I5 卡住回收水位，不可能被暗中跳过。

## 4. 总体架构与最少持久状态

四个独立 SQLite，箭头表示调用方向，不存在反向耦合：

```
Objects.db (blobs)                Catalog.db (snapshots/refs/requests + 新增台账)
      ↑ put/get(AP1)                    ↑ _install 事务(AP2) 内含台账写入(AP3)
      |                                  |
      +---------- Lake.publish ----------+
                                         | 只读拉取(不可变页)
                       subscription service (无状态 worker + substore.db)
                                         | 分页/确认(AP6)，fence 租约
                                         v
                                    warehouse.db (目标数仓, AP4/AP5)
```

### 4.1 Catalog 新增表（与 refs 同库，可与 ref 推进同事务）

```sql
-- 代次变更台账：Δe 的持久形态，I2 的物化
CREATE TABLE key_log(
  ref TEXT NOT NULL, epoch INTEGER NOT NULL,
  tbl TEXT NOT NULL, key TEXT NOT NULL,
  before_val TEXT,          -- encode(JSON) 或 NULL(不存在)
  after_val  TEXT,          -- encode(JSON) 或 NULL(整行删除)
  PRIMARY KEY(ref, epoch, tbl, key)) WITHOUT ROWID;

-- 逻辑键当前态索引：发布时点查旧像、建索引后的稳态读路径
CREATE TABLE ref_key_state(
  ref TEXT NOT NULL, tbl TEXT NOT NULL, key TEXT NOT NULL,
  val TEXT, epoch INTEGER NOT NULL,
  PRIMARY KEY(ref, tbl, key)) WITHOUT ROWID;

-- 快照键目录：某快照每个键由哪个对象提供，供 reset 枚举与 GC 引用计数
CREATE TABLE snap_dir(
  sid INTEGER NOT NULL, tbl TEXT NOT NULL, key TEXT NOT NULL,
  oid TEXT NOT NULL,
  PRIMARY KEY(sid, tbl, key)) WITHOUT ROWID;

-- 在线建索引进度（第 7 节）
CREATE TABLE index_build(
  ref TEXT PRIMARY KEY, watermark_epoch INTEGER NOT NULL, phase TEXT NOT NULL);

-- 订阅水位登记（由订阅服务通过 Catalog API 上报；GC 只信任库内水位）
CREATE TABLE sub_watermark(
  sub_id TEXT PRIMARY KEY, ref TEXT NOT NULL,
  acked_epoch INTEGER NOT NULL, state TEXT NOT NULL);

-- GC 墓碑与周期记账（第 9 节）
CREATE TABLE gc_tombstone(oid TEXT PRIMARY KEY, cycle_id INTEGER NOT NULL);
CREATE TABLE gc_cycle(cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
                      decided_at TEXT NOT NULL, finished_at TEXT);
```

### 4.2 substore（订阅服务自有 SQLite，最少状态）

```sql
CREATE TABLE subscription(
  sub_id TEXT PRIMARY KEY, ref TEXT NOT NULL,
  start_epoch INTEGER NOT NULL, start_sid INTEGER NOT NULL,
  state TEXT NOT NULL,           -- BOOTSTRAP | STREAMING | CLOSED | REJECTED
  fence INTEGER NOT NULL DEFAULT 0,
  created_request TEXT NOT NULL);
CREATE TABLE lease(
  sub_id TEXT NOT NULL, scope TEXT NOT NULL,      -- 'BOOT' 或 epoch 整数
  fence INTEGER NOT NULL, worker TEXT NOT NULL,
  expires_at INTEGER NOT NULL, state TEXT NOT NULL, -- ACTIVE | SUPERSEDED
  PRIMARY KEY(sub_id, scope));
CREATE TABLE delivery(          -- 在途/已交付页记账
  sub_id TEXT NOT NULL, epoch TEXT NOT NULL, page_no INTEGER NOT NULL,
  content_hash TEXT NOT NULL, status TEXT NOT NULL, -- LEASED | ACKED
  lease_fence INTEGER NOT NULL,
  PRIMARY KEY(sub_id, epoch, page_no));
CREATE TABLE ack_record(        -- 复刻 requests 表的重放先于判断模式
  request_id TEXT PRIMARY KEY, binding TEXT NOT NULL, receipt TEXT NOT NULL);
```

### 4.3 warehouse（目标数仓，最少状态）

```sql
CREATE TABLE wh_data(
  tbl TEXT NOT NULL, key TEXT NOT NULL, val TEXT NOT NULL,
  epoch INTEGER NOT NULL, PRIMARY KEY(tbl, key)) WITHOUT ROWID;
CREATE TABLE wh_staging(
  sub_id TEXT NOT NULL, epoch INTEGER NOT NULL, page_no INTEGER NOT NULL,
  tbl TEXT NOT NULL, key TEXT NOT NULL, before_val TEXT, after_val TEXT,
  content_hash TEXT NOT NULL,
  PRIMARY KEY(sub_id, epoch, page_no, tbl, key)) WITHOUT ROWID;
CREATE TABLE wh_ledger(         -- 完整入仓的唯一身份(I4)
  sub_id TEXT NOT NULL, epoch INTEGER NOT NULL,
  pages_total INTEGER NOT NULL, state TEXT NOT NULL, -- LOADING | APPLIED
  manifest_hash TEXT NOT NULL,
  PRIMARY KEY(sub_id, epoch));
```

## 5. 源端写入路径：把 Δe 嵌进现有原子点（语句级伪代码）【设计推演】

所有新增写都发生在 `_install` 的 `BEGIN IMMEDIATE`（branchlake.py:L96）与 `COMMIT`（branchlake.py:L114）之间，因此"ref 已推进但台账缺失"的部分状态在结构上不存在；异常照旧走 branchlake.py:L116-L117 的 ROLLBACK。

### 5.1 普通 publish / merge（含 merge_parent）

在 branchlake.py:L107-L113 之间插入（仅当 index_build.phase 为 SCANNING/ACTIVE；见第 7 节）：

```text
for (t, k, after) in rows:                       # merge 的 rows 即显式决议
    row = SELECT val FROM ref_key_state
          WHERE ref=:name AND tbl=:t AND key=:k
    before = row.val if row else NULL
    if before != encode_or_null(after):          # 与 delta 同一"仅含值不同"规则
        INSERT INTO key_log VALUES(:name,:new_epoch,:t,:k, before, encode_or_null(after))
    UPSERT ref_key_state SET val=encode_or_null(after), epoch=:new_epoch
      WHERE ref=:name,tbl=:t,key=:k              -- after 为 NULL 时 DELETE 该行
# snap_dir：复制父快照目录中本批键以外的行，本批键指向新 oid（INSERT INTO … SELECT）
INSERT INTO snap_dir(sid,tbl,key,oid)
  SELECT :new_sid, tbl, key, oid FROM snap_dir
   WHERE sid=:parent_sid AND (tbl,key) NOT IN (:batch_keys)
INSERT INTO snap_dir VALUES(:new_sid, :t, :k, :new_oid) -- 每个批键一行
```

要点：旧像来自 `ref_key_state` 的**点查**（首次建索引前见第 7 节兜底），只访问本批键；merge_parent 的行不参与（branchlake.py:L149-L151 的语义保持），台账只反映调用者 rows 的真实决议，与 checks.py:L24-L29 的合并事实一致。

### 5.2 物理重写 rewrite

重写的全状态物化是发布路径既有的一次性成本（branchlake.py:L152-L159），台账在同事务内逐键比较物化结果与 `ref_key_state`：

```text
state = 现有 rows() 折叠 + rows 覆盖            # branchlake.py:L153-L159，已含真实载荷
for (t,k,v) in state 的键 ∪ ref_key_state 的键:
    before = ref_key_state 点查; after = state 取值或 NULL
    仅当 before != after 才写 key_log            # rows=[] 时集合为空 → Δe 为空
UPSERT/DELETE ref_key_state 到 state
# 目录整体指向重写新 oid（共享 oid 不变；新 oid 只此一个）
DELETE FROM snap_dir WHERE sid=:new_sid;
INSERT INTO snap_dir SELECT :new_sid,tbl,key,:rewrite_oid FROM <state 键集>
```

因此重写一代对订阅者交付空变更集（对照 checks.py:L22 的 `delta == []`），物理重写不产生行变更；订阅者只入一条空 Δ 的代次边界记录（wh_ledger 一行，wh_data 零改动），代次不跳。

### 5.3 任意 reset（含回退再前进，消除 ABA）

reset 不造快照（branchlake.py:L168-L174），目标快照的键集由 `snap_dir` 枚举，**只对实际不同的键取真实载荷**：

```text
target_keys = SELECT tbl,key,oid FROM snap_dir WHERE sid=:target_sid
cur_keys    = SELECT tbl,key,val,epoch FROM ref_key_state WHERE ref=:name
for key in target_keys ∪ cur_keys:
    after  = 目标键: 仅当需要时值取自 objects.get(其 snap_dir.oid)（I3）
    before = 当前键: ref_key_state.val，缺失为 NULL
    if before != after:
        INSERT INTO key_log VALUES(:name,:new_epoch,t,k,before,after)
    UPSERT ref_key_state ... ；目标缺失的键 DELETE
# reset 不新建 snap_dir 行：目标 sid 的目录已存在且不可变
```

样例 main epoch 4→5（sid 5→1）由此得到 5 条反向变更、epoch 5→6（sid 1→5）得到 5 条正向变更，与 checks.py:L39 及 trace 中两个非空 delta 逐值一致。sid 重复出现但 epoch 不同，台账以 `(ref,epoch)` 为主键，ABA 在结构上不可能混淆。

### 5.4 五类动作的关系一览

| 动作 | 新快照 | 台账 Δe | 订阅者效果 | 基线事实 |
|---|---|---|---|---|
| 首次/正常 publish | 是，追加 oid | 本批键前后像 | 按页 UPSERT/DELETE | samples.py:L24, L26 |
| 物理 rewrite | 是，单 oid | 可能为空（rows=[] 必空） | 空 Δ 也要确认，代次 +1 | checks.py:L22-L23 |
| 显式 merge | 是，双父血缘 | 仅决议 rows 的键 | 第二父行不自动入账 | checks.py:L24-L30 |
| reset 回退 | 否 | 目标态与当前态的真实反向差 | 恢复删除、撤销新增 | trace epoch5 |
| reset 再前进 | 否 | 再产生相反方向真实差 | 不漏代，不把同 sid 当无变化 | trace epoch6 |

## 6. 订阅协议：注册、首次补数、分页、租约、确认

### 6.1 注册与起点过期

```text
register(sub_id, ref, request_id):
  Catalog 只读事务: head = Lake.ref(ref)                      # branchlake.py:L57-L61
  若 start_sid（默认 head.snapshot）已不在 GC 保留集 → 返回 REJECT_START_EXPIRED（明确拒绝）
  substore 事务: 插 subscription(state=BOOTSTRAP, start_epoch=head.epoch, start_sid=…)
                绑定 request_id（重放返回原记录，复刻 branchlake.py:L98-L101）
  Catalog 事务: UPSERT sub_watermark(state=BOOTSTRAP, acked_epoch=head.epoch-1)
                —— 该订阅即刻进入 GC 保护集合(I5)
```

### 6.2 首次固定完整引用代次补数（只读一次固定快照）

```text
bootstrap(sub_id) [substore 事务取 fence，租约 scope='BOOT']:
  sid = subscription.start_sid                      # 注册时固定，之后发布不动它
  页 = 对 snap_dir WHERE sid=:sid 按 (tbl,key) 排序、每 P 键切片
  每页值仅经 objects.get(该键 snap_dir.oid) 取得(I3)，同对象内多键只 get 一次
  页身份 (sub_id,'BOOT',page_no)，附 content_hash=sha256(规范页字节)
  目标库逐页 INSERT OR REPLACE INTO wh_staging …   # AP4，可多事务、可重放
  末页触发 AP5（6.4）单事务把整快照写入 wh_data，wh_ledger 记 epoch=start_epoch-1 的基线
  substore 事务: state=STREAMING；delivery 的 BOOT 页置 ACKED
```

补数期间 correct/rewrite/merge 继续提交无影响：sid 是不可变快照（snapshots 行只 INSERT，branchlake.py:L47-L48, L107-L109），目录与对象受 I5 保护。补数完成后游标停在 start_epoch-1，再从第 start_epoch 代起走增量，不会把补数后发布的代次算进全量。

### 6.3 稳态分页拉取与租约接替

```text
pull(sub_id, epoch, page_no, worker, fence):
  substore 事务:
    校验 lease.fence == fence 且未过期，否则 REJECT_FENCE（旧 worker 在此被挡）
    若 delivery 已有 (sub,epoch,page_no,ACKED) → 重放原 content_hash（幂等）
  Catalog 只读事务:
    断言 index_build.phase IN (SCANNING 且水位覆盖 epoch, ACTIVE)，否则 RETRY_LATER
    按 (tbl,key) 排序切 key_log WHERE ref AND epoch=:epoch → 确定性生成页字节
  substore 事务: UPSERT delivery(status=LEASED, lease_fence=fence)

renew/takeover(sub_id, scope):
  仅当旧租约 expires_at 已过: fence+=1，旧 lease 置 SUPERSEDED，发新租约
```

- 所有读到的分页都属于**某个稳定代次**：bootstrap 页属于注册时固定的 start_sid；增量页属于 key_log 中已提交的确定 epoch（AP2/AP3 同事务，读到即不可变）。
- 重复拉取：页由不可变台账确定性重生成，content_hash 恒定；delivery/staging 的主键使重复写零净变化。
- 旧工作者恢复后能做的操作：重放它手里**内容不变**的页、读 warehouse/substore 状态做对账；不能领新页、不能推进游标、不能确认（fence 不符一律 REJECT_FENCE）。它对 wh_staging 的迟到写因 `(sub,epoch,page_no,tbl,key)` 主键与 content_hash 校验而幂等，且 AP5 之后 staging 已清空，迟到写只会进入一个已 APPLIED 代次的孤儿 staging 区，由对账清理，绝不改 wh_data。

### 6.4 入仓原子点 AP5 与确认顺序（无跨库事务的恰好一次）

```text
apply_and_ack(sub_id, epoch, 末页):
  # —— warehouse 单库事务，先于一切确认 ——
  BEGIN IMMEDIATE
    若 wh_ledger 已有 (sub,epoch,APPLIED): COMMIT; 走幂等重放路径
    校验 staging 页数 == 页清单、每页 content_hash 与 Catalog 台账摘要一致
    对 staging 按 (tbl,key) 折叠（同代次内键不跨页重复）：
       after_val IS NULL → DELETE wh_data；否则 UPSERT wh_data(…, epoch)
    INSERT wh_ledger(state='APPLIED', manifest_hash=全代次哈希)
    DELETE FROM wh_staging WHERE sub_id,epoch
  COMMIT                                                 # AP5：代次可见的唯一原子点
  # —— 之后才确认，确认丢失不影响已提交事实 ——
  substore 事务(带 fence):
    ack_record 重放判定（request_id 先于状态判断，同 branchlake.py:L98-L101）
    delivery 全部置 ACKED；推进 sub_watermark 上报队列；lease 关闭
  Catalog 事务（可由上报批次异步执行）:
    UPSERT sub_watermark(acked_epoch=epoch)              # 放松 GC 水位，只朝安全方向
```

确认与目标提交的先后关系是硬性的：**warehouse COMMIT → substore ACK → Catalog 水位上报**。反向（先确认后入仓）会在崩溃时丢代；本顺序下任何崩溃点都可凭 wh_ledger 重放收敛。

### 6.5 四种故障交错的收敛（D 的完整推演）

1. **末页入仓、warehouse 已提交，响应丢失**：wh_ledger=APPLIED 已持久。worker 重试先读 wh_ledger，命中即跳过 apply，补发幂等 ack；无重复行变更。
2. **源端 ack 已落 ack_record，响应丢失**：request_id 重放返回原回执（同 requests 表机制），游标不重复推进。
3. **租约接替**：fence+1 后旧 worker 所有写被 substore 拒；新 worker 以 wh_ledger/delivery 为准决定"补页还是补 ack"，不会跳过该 epoch（未 APPLIED 则页可凭不可变台账重拉）。
4. **旧 worker 带原分页恢复**：页内容仍与台账一致（I2 不可变），其 staging 写幂等；若新 worker 已完成 AP5，旧写不触达 wh_data；若未完成，两份相同 staging 汇合后一次 AP5。两条路径结果相同。

## 7. 在线建立逻辑键索引（不暂停发布）

### 7.1 两阶段构建与完整性条件

```text
升级安装新表后，Catalog 事务: INSERT index_build(ref, watermark_epoch=0, phase='SCANNING')
builder 循环（每轮一个短事务，不持长锁，与 _install 的 BEGIN IMMEDIATE 串行）:
  head = ref(name).epoch
  for e in watermark_epoch+1 .. head:
    由 snapshots/manifest + objects.get 重建该代 Δe（内部兜底，等价 delta 语义但只做一次）
    INSERT OR IGNORE INTO key_log …      # 与发布路径双写，内容由 I2 唯一确定，必然相同
    UPSERT ref_key_state …；补 snap_dir（sid 已存在，目录缺失则按 manifest 补建）
  Catalog 事务: watermark_epoch = head
交接（同一 Catalog 事务内判定完整性条件）:
  C1: watermark_epoch >= ref(name).epoch
  C2: 每个键在 ref_key_state 的值 == key_log 中该键 max(epoch) 行的 after_val
  C3: snap_dir 对每个已存在 sid 的键数与 manifest 折叠键数一致
  全部满足 → phase='ACTIVE'；此后仅发布路径(第 5 节)维护台账
```

`INSERT OR IGNORE` 让 builder 与并发发布者谁先写同一 `(ref,epoch,tbl,key)` 都安全：I2 保证内容逐字节相同。构建期发布不中断，correct 与 reset 的竞争由 `_install` 的 `BEGIN IMMEDIATE`（branchlake.py:L96）与事务内 epoch 判定（branchlake.py:L103-L104）串行化，输家收 STALE_EPOCH，台账只随赢家事务落盘。

### 7.2 两个中断点的恢复

- **对象已写未发布**（AFTER_OBJECT，branchlake.py:L163-L164）：Catalog 无快照、无台账、无目录痕迹，builder 与订阅者都看不到它；它只作为对象库孤立行存在，归第 9 节在途宽限保护。
- **Catalog 已发布、索引未追平**：builder 重启后重读 `refs.epoch` 得到新高水位继续补建；期间订阅者拉取未覆盖代次得到 **RETRY_LATER**（不跳过、不发半截页），水位越过该代次后按序交付。已接纳订阅的保护水位已在注册时写入 sub_watermark，回收器不会删掉补建所需对象。

在线索引交接的完整性条件即 C1∧C2∧C3 在同一事务内成立；未成立前 phase 不翻转，发布双写持续有效，不存在"索引宣称就绪但有洞"的状态。

## 8. 历史快照到期与载荷回收（保护有效，解除后真删）

### 8.1 回收保护谓词

对象 oid 仅当以下谓词**全部不成立**时才可进入回收候选：

- **P1 保留快照可达**：oid 出现在保留快照集 R 中某 sid 的 manifest 里。
  `R = {所有 ref 的当前 sid} ∪ {BOOTSTRAP 未完成订阅的 start_sid}
       ∪ {每个订阅 acked_epoch 与 next-epoch 之间引用的 sid} ∪ {snap_dir 水位内 sid} ∪ {sid 0}`
- **P2 在途分页/租约**：substore 中 LEASED 页所需 oid（delivery 经 lease 心跳同步进 Catalog 的在途视图；同步落后只会多保留，是安全方向）。
- **P3 共享对象**：被多个保留 manifest 引用（实际由 P1 的引用计数覆盖；如 `1ab0321b…` 同时被 sid 1/2/3 引用，side 存活即不可删）。
- **P4 在途发布宽限**：已 `put` 但尚无快照引用的对象，按请求宽限保留到请求终结或超时；late 重试同内容命中同 oid（branchlake.py:L25），成功后转 P1。
- **P5 索引构建**：index_build 水位未越过的快照所引用对象。

### 8.2 两阶段、崩溃可重入的回收（AP7 → AP8）

```text
GC 周期（Catalog 事务 AP7）:
  按 P1–P5 计算候选 oid 集；对每个候选:
     INSERT OR IGNORE gc_tombstone(oid, cycle_id)
     摘除 snap_dir/保留清单中的可回收引用（引用计数归零才允许入墓碑）
  写 gc_cycle(decided_at)
物理删除（Objects 库，逐条自动提交 AP8，与 Catalog 无共享事务）:
  for oid in gc_tombstone WHERE cycle 已提交:
     DELETE FROM blobs WHERE id=:oid
  全部成功后 Catalog 事务置 gc_cycle.finished_at 并清理墓碑
重入对账:
  墓碑在、blob 在 → 继续删；blob 删了、周期未完结 → 续跑剩余条目
  不可能出现"blob 已删而墓碑未写"（顺序固定为先 AP7 后 AP8）
  若删除后迟到请求又需要同内容 → put 的 INSERT OR IGNORE 重新写回，自愈合（I3 内容寻址）
```

删除是 `DELETE` 真实载荷行而非永久标记；历史快照到期后，保护解除即实际回收，不保留全历史。key_log/snap_dir 的旧代次行同样按全局最低订阅水位在 AP7 内归档删除。

### 8.3 场景 F 的逐项判定（样例事实）

- sub1 已确认第 6 代、sub2 停第 4 代且持第 5 代部分页；side 仍在使用共享对象；late 在对象提交后中断、回收器运行、随后重试。
- **必须保留**：sid 1（sub2 的 epoch5 回退目标，main back 的目标）、sid 5（epoch6 forward 目标，sub1 追平边界）、sid 3（side head）；对象 `1ab0321b…` 因 sid 1/2/3 共享保留；`b0a478d…` 被 sid 5 manifest 直接引用而保留（实测 sid 5 manifest 含它）；sub2 的 LEASED 第 5 代页与 late 在途 oid 由 P2/P4 保护——回收器此时**不能删** late 的 oid。
- **late 重试后**：同 oid 被 sid 6 引用（1.5 节实测），P4 解除、P1 接续，重试成功；随后若 main 继续前进且所有订阅越过该边界，保护解除时该 oid 才与其他到期对象一起被 AP8 实际 DELETE。
- **最终可实际删除**：无任何 ref/订阅/目录/在途引用的快照对象（例如所有订阅越过 epoch5 后 sid 2 独占的 `ab1386…`），以及宽限外的失败在途对象；删除后 blobs 行消失、空间归还。
- **已可见状态**：refs/key_log/ref_key_state 与各 warehouse 已提交态（wh_ledger=APPLIED）。**未来恢复所需状态**：substore 的 lease/delivery/ack_record、warehouse 的 wh_staging、Catalog 的 gc_tombstone/gc_cycle、在途对象宽限标记。前者面向读者，后者面向重放，两者分离使回收不必保留全历史。

## 9. 持久化中断恢复伪代码（按原子点汇总）【设计推演】

```text
recover():
  # Objects / Catalog 边界
  若存在"put 已提交、无快照引用"对象: 保持不动，进入 P4 宽限；请求重试按 branchlake.py:L141-L143 重放
  Catalog 事务天然原子: snapshots/refs/requests/key_log/ref_key_state/snap_dir 要么全有要么全无
                                     （AP2+AP3 同事务，沿用 branchlake.py:L116-L117 回滚）
  # 索引构建
  读 index_build: SCANNING → 按 7.1 从 watermark_epoch 续建；ACTIVE → 仅发布路径维护
  # substore
  对每条 lease: 未过期保持；已过期置 SUPERSEDED，等待 takeover 翻 fence
  对 delivery: LEASED 且无有效租约 → 保持行，页可由 key_log 重拉（身份与 hash 不变）
  ack_record 持久 → 确认重放返回原回执
  # warehouse
  对 wh_ledger:
    APPLIED → 代次已生效；若 substore 未 ACK，补 ACK；若 Catalog 水位未上报，补上报
    LOADING 或无 ledger 但 wh_staging 有行 → 废弃/保留均可：分页幂等可重拉，
                末页 AP5 以 content_hash 全量校验后一次提交，未提交则 wh_data 永不可见
  # GC
  gc_cycle 未 finished → 按 8.2 对账续跑；墓碑与 blob 的差集即剩余工作
```

## 10. 正确性论证

**引理 1（台账等价）**：key_log 中 `(ref,e)` 的行集合等于对 epoch e-1、e 两状态按 branchlake.py:L176-L181 规则求得的差集。
证明：发布路径（5.1/5.2）与 reset 路径（5.3）对每个键都以 ref_key_state 点查取 before、以 objects.get 真实载荷取 after，且仅在 `before != after` 时写行；ref_key_state 按同一折叠规则（branchlake.py:L77-L80 的 None 删除/覆盖）逐代维护，归纳基为 sid 0 空态（branchlake.py:L53）。故折叠不变量成立，差集规则与 delta 逐条相同。∎

**引理 2（不可变与可重放）**：已提交代次的页字节永久确定（在保留期内），重复拉取/重复确认/租约接替都收敛到同一结果。
证明：snapshots/key_log 只 INSERT 不可变（branchlake.py:L47-L48, L107-L109）；页是确定性切片加 content_hash；delivery、ack_record、wh_ledger 均以请求身份或 `(sub,epoch)` 为主键，重放先于状态判断（6.3–6.5，复刻 branchlake.py:L98-L101）；fence 保证只有现任租约能推进游标，旧 worker 的迟到写幂等且不触达已 APPLIED 的 wh_data。∎

**引理 3（代次原子与不漏代）**：warehouse 对每个 `(sub,epoch)` 恰好发生一次有效状态转移，且 epoch 严格按序。
证明：AP5 是 wh_data 可见的唯一事务，wh_ledger 主键保证 APPLIED 唯一；未 APPLIED 的代次在 wh_data 完全无痕（staging 不参与读取）；拉取以 acked_epoch+1 为唯一下一代，空 Δ 的 rewrite 代次也落 ledger 边界，故无跳代、无重代。∎

**引理 4（回收安全）**：任何被读取或恢复所需的对象都不满足回收候选。
证明：8.1 的 P1–P5 枚举了全部持有方——活跃 ref、订阅代次窗口、在途页、共享引用、在途发布、构建水位；sub_watermark 只可能落后于真实 ack（上报在 ACK 之后），误差方向是多保留。两阶段删除保证墓碑先于 DELETE，恢复对账可重入；内容寻址使误删后的迟到 put 自愈合。∎

**定理（无跨库事务下的恰好一次同步）**：在 I6 约束下，协议对每个已接纳订阅交付与源端逐代一致的状态，重复拉取、确认丢失、租约接替、旧 worker 恢复、任意持久化中断均不产生重复入账或跳代。
证明：提交顺序固定为 AP1（对象）→ AP2+AP3（Catalog 代次与台账）→ AP4（staging）→ AP5（warehouse 提交）→ AP6（ACK）→ 水位上报 → 解除保护后 AP7/AP8（回收）。由引理 1，交付内容精确；由引理 2/3，重试与接替幂等且按序；由引理 4，重放所需载荷在完成前受保护。每个崩溃点之后续跑的动作都是该序列上第一个未持久化步骤的幂等重放，归纳可知最终状态与无故障执行相同。∎

**有限发布后的追平条件**：设发布停止后订阅者落后 d 代、待应用台账行共 K 行、页大小 P。当（a）index_build 已 ACTIVE（或水位覆盖所请代次）、（b）相关对象满足 P1–P5、（c）租约能在有限次 expire/takeover 内续期，则拉取轮次为 O(d + K/P)、apply 为 d 个单事务，有限步内追平，不要求发布侧停机。若注册时 start_sid 已不在保留集，明确返回 REJECT_START_EXPIRED；**已接纳**订阅不受过期影响——其水位卡住回收，不存在暗中跳过。

**引用竞争与 ABA**：竞争发布在 `_install` 事务内串行，epoch 判定失败方收 STALE_EPOCH（branchlake.py:L103-L104），请求重放优先（branchlake.py:L98-L101）；sid 的任何重复（5→1→5）都处于不同 epoch 之下，台账与 ledger 以 epoch 为身份，ABA 不产生错误合并。

## 11. 空间与访问上界（按活跃状态描述）

| 活动 | 对象库读取 | Catalog/台账访问 | warehouse 访问 |
|---|---|---|---|
| 稳态发布一代 | 仅本批新对象 1 次 put | O(批键数) 点查 + 同序台账行 | 不参与 |
| 物理重写 | 一次性全状态（既有成本，samples.py:L28-L30） | 逐键比较一次，Δ 常为空 | 空代次 1 行 ledger |
| reset | 仅实际变化键的必要旧像 | snap_dir 枚举目标键 + 差异键台账 | 按差异键 UPSERT/DELETE |
| 订阅拉取一代 | 0（台账已含值）；bootstrap 每对象至多 1 次 get | O(变更键数/P) 只读页 | staging 幂等写 |
| 代次 apply | 0 | 0 | O(变更键数)，单事务 |
| GC 一周期 | 候选 oid 的 DELETE | 一次谓词扫描 + 墓碑事务 | 0 |

空间上界：warehouse ≤ 当前逻辑状态 + 单代 staging + 每代一行 ledger；Catalog 新增量 ≤ 保留窗口内 key_log/snap_dir 行 + 每订阅常数行；Objects 保留集由最低订阅水位与活跃 ref 界定，保护解除即按墓碑实际删除，不累积全历史。首次建索引仅扫描一次被接纳存量，首次补数仅读一次固定快照；此后每轮只触受影响键与必要旧像，无全量快照比较。

## 12. 契约时序 A–F 走查（输入均取自 samples.py；未实现动作为【设计推演】）

### A. 固定 epoch=1 补数期间持续发布，追赶到 epoch=4

注册时 head 为 main epoch=1/sid=1（samples.py:L24），bootstrap 固定 sid=1。期间 correct（epoch2/sid2）、rewrite（epoch3/sid4）、merge（epoch4/sid5）经 AP1→AP2+AP3 正常提交，sid 1 不可变且受 P1 保护，补数页字节不变。补数末页 AP5 写入 INITIAL 三行（samples.py:L3-L6），游标=1。随后按 key_log 拉 Δ2（4 键，对照 checks.py:L16-L21）、Δ3（空，checks.py:L22）、Δ4（3 键，checks.py:L24-L29）；三代三个 AP5，wh_data 终态等于 trace 的 epoch4 rows。分页之间的发布只影响后续代次，不回插补数。

### B. side 先于 rewrite/merge；两分支的 b、重写零变化、合并结果

side 从 sid 1 fork（samples.py:L25），发布 SIDE 后 sid 3 仍含 b（trace side rows，checks.py:L31-L36）——side 不知道 main 对 b 的删除。main rewrite 的 Δ3 为空（5.2 节，checks.py:L22-L23），物理对象换成 `b0a478d…` 但逻辑行零变化，订阅者只过一个空代次。merge 的 rows 是 SIDE+east 决议（samples.py:L18, L31），merge_parent=sid 3 仅入血缘 `[4,3]`（checks.py:L30），side 的 b **不**自动进入 main：main 台账 Δ4 无 b 行，b 在 main 仍不存在；side 订阅者各自的台账里 b 始终存在。共享对象 `1ab0321b…` 被两边共同引用，GC 引用计数不归零（P3）。

### C. epoch4→5→6（sid 5→1→5）；部分页 + initial 重放 + 继续拉取

订阅者已取 epoch5 部分页（delivery=LEASED）。另一请求重放 initial：由 branchlake.py:L98-L101 与 samples.py:L34，返回 epoch=1 原回执，head 不动、不生成新代次、与订阅游标无交互。epoch5 台账是回退差 5 键（trace），剩余页凭 `(main,5,page_no)` 从不可变 key_log 重拉，hash 恒定；AP5 后 wh_data 回到 INITIAL（checks.py:L37）。再拉 epoch6（sid 同为 5 但 epoch 不同，I1 防 ABA），Δ6 是 5 键反向差（checks.py:L39），wh_data 恢复 epoch4 状态。两个 sid=5 绝不合并为一个代次。

### D. 第 6 代末页提交后响应丢失、确认丢失、接替、旧 worker

按 6.5 四种情形逐一收敛：AP5 已 COMMIT 则 wh_ledger=APPLIED 为事实源，补 ACK 不改数据；ack_record 命中则重放回执；takeover 翻 fence 后旧 worker 领新页/确认被 REJECT_FENCE，其手持页仍可幂等重放 staging；新 worker 凭 ledger/delivery 判断补页或补确认。第 6 代恰好一次入账，无第 7 代被提前消费（游标只认 acked_epoch+1）。

### E. 在线建索引与 correct/reset 竞争；两类中断

竞争经 `_install` 的 BEGIN IMMEDIATE 串行（branchlake.py:L96），赢家台账随 AP3 同事务提交，输家 STALE_EPOCH。对象已写未发布：Catalog 无痕，builder 跳过，对象入 P4 宽限。已发布未追平：builder 重启按 refs.epoch 续建，订阅者对未就绪代次收 RETRY_LATER 而非跳代。交接须在同一事务满足 C1∧C2∧C3（7.1），否则 phase 不翻转、双写不停止。旧写接口 `publish/reset/read/rows/delta` 签名与行为不变，新表只增不改。

### F. 到期回收、两订阅错位、side 共享、late 中断与重试

见 8.3 的完整判定：慢订阅与未确认分页经 sub_watermark/P1/P2 保住 sid 1/3/5 与共享对象；late oid 先受 P4、重试后转 P1（实测进入 sid 6 manifest）；保护解除后独占对象按 AP7→AP8 真实 DELETE，key_log/snap_dir 旧行按最低水位清理。已可见状态与恢复状态分离列举，无任何永久全历史保留。

## 13. 兼容性、验证与运行清单

- `branchlake.py`、`samples.py`、`checks.py` 不改一行；`normal`/`trace`/`demo` 原断言原样保留（checks.py:L11-L79, L107-L114）。
- 新增能力全部以新表与新模块形态存在，公开旧方法签名与返回值不变；旧的全量 `delta`（branchlake.py:L176-L181）保留作小样本参照与建索引期内部兜底。
- 运行清单：verify 改为 `["python3","checks.py","design"]`，demo 保持 `["python3","checks.py","demo"]`（`.pointwise-runtime.json`）。`design` 重跑 normal 回归、逐值比对下方 evidence 的 baseline_trace 与实际 `trace` 输出、校验 A–F decisions 字段与全部源码行号范围（checks.py:L82-L98）；结构通过不等于协议正确性证明，协议正确性以第 10 节论证与第 12 节时序人工复核为准。
- 可重跑命令：`python3 checks.py normal` → NORMAL_REGRESSION_OK；`python3 checks.py trace` → 下方 baseline_trace 的来源；`python3 checks.py demo` → 七代净变化与孤立对象展示；`python3 checks.py design` → 证据与行号校验。

## 14. 可重跑证据

下方围栏内对象的 `baseline_trace` 逐值等于 `python3 checks.py trace` 的实际 JSON 输出（由脚本用 `replay()` 直接序列化注入，未手工预填）；`decisions` 的 A–F 结论定位到上文原子点 AP1–AP8 与对应章节。

```json
{
  "baseline_trace": {
    "current": {
      "epoch": 7,
      "ref": "main",
      "rows": [
        [
          "sample",
          "a",
          {
            "station": "east",
            "v": 12
          }
        ],
        [
          "sample",
          "c",
          {
            "station": "west",
            "v": 30
          }
        ],
        [
          "sample",
          "d",
          {
            "station": "west",
            "v": 40
          }
        ],
        [
          "sample",
          "z",
          {
            "station": "east",
            "v": 99
          }
        ],
        [
          "station",
          "east",
          {
            "region": "merged"
          }
        ]
      ],
      "snapshot": 6
    },
    "epochs": [
      {
        "delta": [
          [
            "sample",
            "a",
            null,
            {
              "station": "east",
              "v": 10
            }
          ],
          [
            "sample",
            "b",
            null,
            {
              "station": "west",
              "v": 20
            }
          ],
          [
            "station",
            "east",
            null,
            {
              "region": "north"
            }
          ]
        ],
        "epoch": 1,
        "label": "initial",
        "ref": "main",
        "rows": [
          [
            "sample",
            "a",
            {
              "station": "east",
              "v": 10
            }
          ],
          [
            "sample",
            "b",
            {
              "station": "west",
              "v": 20
            }
          ],
          [
            "station",
            "east",
            {
              "region": "north"
            }
          ]
        ],
        "snapshot": 1
      },
      {
        "delta": [
          [
            "sample",
            "a",
            {
              "station": "east",
              "v": 10
            },
            {
              "station": "east",
              "v": 11
            }
          ],
          [
            "sample",
            "b",
            {
              "station": "west",
              "v": 20
            },
            null
          ],
          [
            "sample",
            "c",
            null,
            {
              "station": "west",
              "v": 30
            }
          ],
          [
            "station",
            "east",
            {
              "region": "north"
            },
            {
              "region": "south"
            }
          ]
        ],
        "epoch": 2,
        "label": "correct",
        "ref": "main",
        "rows": [
          [
            "sample",
            "a",
            {
              "station": "east",
              "v": 11
            }
          ],
          [
            "sample",
            "c",
            {
              "station": "west",
              "v": 30
            }
          ],
          [
            "station",
            "east",
            {
              "region": "south"
            }
          ]
        ],
        "snapshot": 2
      },
      {
        "delta": [],
        "epoch": 3,
        "label": "rewrite",
        "ref": "main",
        "rows": [
          [
            "sample",
            "a",
            {
              "station": "east",
              "v": 11
            }
          ],
          [
            "sample",
            "c",
            {
              "station": "west",
              "v": 30
            }
          ],
          [
            "station",
            "east",
            {
              "region": "south"
            }
          ]
        ],
        "snapshot": 4
      },
      {
        "delta": [
          [
            "sample",
            "a",
            {
              "station": "east",
              "v": 11
            },
            {
              "station": "east",
              "v": 12
            }
          ],
          [
            "sample",
            "d",
            null,
            {
              "station": "west",
              "v": 40
            }
          ],
          [
            "station",
            "east",
            {
              "region": "south"
            },
            {
              "region": "merged"
            }
          ]
        ],
        "epoch": 4,
        "label": "merge",
        "ref": "main",
        "rows": [
          [
            "sample",
            "a",
            {
              "station": "east",
              "v": 12
            }
          ],
          [
            "sample",
            "c",
            {
              "station": "west",
              "v": 30
            }
          ],
          [
            "sample",
            "d",
            {
              "station": "west",
              "v": 40
            }
          ],
          [
            "station",
            "east",
            {
              "region": "merged"
            }
          ]
        ],
        "snapshot": 5
      },
      {
        "delta": [
          [
            "sample",
            "a",
            {
              "station": "east",
              "v": 12
            },
            {
              "station": "east",
              "v": 10
            }
          ],
          [
            "sample",
            "b",
            null,
            {
              "station": "west",
              "v": 20
            }
          ],
          [
            "sample",
            "c",
            {
              "station": "west",
              "v": 30
            },
            null
          ],
          [
            "sample",
            "d",
            {
              "station": "west",
              "v": 40
            },
            null
          ],
          [
            "station",
            "east",
            {
              "region": "merged"
            },
            {
              "region": "north"
            }
          ]
        ],
        "epoch": 5,
        "label": "back",
        "ref": "main",
        "rows": [
          [
            "sample",
            "a",
            {
              "station": "east",
              "v": 10
            }
          ],
          [
            "sample",
            "b",
            {
              "station": "west",
              "v": 20
            }
          ],
          [
            "station",
            "east",
            {
              "region": "north"
            }
          ]
        ],
        "snapshot": 1
      },
      {
        "delta": [
          [
            "sample",
            "a",
            {
              "station": "east",
              "v": 10
            },
            {
              "station": "east",
              "v": 12
            }
          ],
          [
            "sample",
            "b",
            {
              "station": "west",
              "v": 20
            },
            null
          ],
          [
            "sample",
            "c",
            null,
            {
              "station": "west",
              "v": 30
            }
          ],
          [
            "sample",
            "d",
            null,
            {
              "station": "west",
              "v": 40
            }
          ],
          [
            "station",
            "east",
            {
              "region": "north"
            },
            {
              "region": "merged"
            }
          ]
        ],
        "epoch": 6,
        "label": "forward",
        "ref": "main",
        "rows": [
          [
            "sample",
            "a",
            {
              "station": "east",
              "v": 12
            }
          ],
          [
            "sample",
            "c",
            {
              "station": "west",
              "v": 30
            }
          ],
          [
            "sample",
            "d",
            {
              "station": "west",
              "v": 40
            }
          ],
          [
            "station",
            "east",
            {
              "region": "merged"
            }
          ]
        ],
        "snapshot": 5
      },
      {
        "delta": [
          [
            "sample",
            "z",
            null,
            {
              "station": "east",
              "v": 99
            }
          ]
        ],
        "epoch": 7,
        "label": "late",
        "ref": "main",
        "rows": [
          [
            "sample",
            "a",
            {
              "station": "east",
              "v": 12
            }
          ],
          [
            "sample",
            "c",
            {
              "station": "west",
              "v": 30
            }
          ],
          [
            "sample",
            "d",
            {
              "station": "west",
              "v": 40
            }
          ],
          [
            "sample",
            "z",
            {
              "station": "east",
              "v": 99
            }
          ],
          [
            "station",
            "east",
            {
              "region": "merged"
            }
          ]
        ],
        "snapshot": 6
      }
    ],
    "late_retry": {
      "epoch": 7,
      "ref": "main",
      "snapshot": 6
    },
    "merge_parents": [
      4,
      3
    ],
    "orphan_ids": [
      "ce04ca75a1cd3e4f47881c3a96af0ce918d635b06c7558b4505c612974789643"
    ],
    "replayed_initial": {
      "epoch": 1,
      "ref": "main",
      "snapshot": 1
    },
    "rewrite_payload_reads": 2,
    "side": {
      "epoch": 1,
      "ref": "side",
      "rows": [
        [
          "sample",
          "a",
          {
            "station": "east",
            "v": 12
          }
        ],
        [
          "sample",
          "b",
          {
            "station": "west",
            "v": 20
          }
        ],
        [
          "sample",
          "d",
          {
            "station": "west",
            "v": 40
          }
        ],
        [
          "station",
          "east",
          {
            "region": "north"
          }
        ]
      ],
      "snapshot": 3
    },
    "unpublished": {
      "epoch": 6,
      "ref": "main",
      "rows": [
        [
          "sample",
          "a",
          {
            "station": "east",
            "v": 12
          }
        ],
        [
          "sample",
          "c",
          {
            "station": "west",
            "v": 30
          }
        ],
        [
          "sample",
          "d",
          {
            "station": "west",
            "v": 40
          }
        ],
        [
          "station",
          "east",
          {
            "region": "merged"
          }
        ]
      ],
      "snapshot": 5
    }
  },
  "decisions": {
    "A": "注册时固定 main epoch=1/sid=1 为补数快照(6.1/6.2，samples.py:L24)；bootstrap 期间 correct/rewrite/merge 经 AP1->AP2+AP3 继续提交但 sid1 不可变且受 P1 保护，补数分页字节不变；末页 AP5 原子落 INITIAL 三行后游标=1，再按 key_log 顺序交付 Delta2(4键,checks.py:L16-L21)、Delta3(空,checks.py:L22)、Delta4(3键,checks.py:L24-L29)，三个 AP5 追平 epoch4，发布不回插补数、不跳代。",
    "B": "side 从 sid1 fork 后 sid3 仍含 b(checks.py:L31-L36)，main 对 b 的删除不跨 ref 传播；rewrite 按 5.2 逐键比较得 Delta3 为空(checks.py:L22-L23)，物理换对象 b0a478d 而零行变更，订阅者仅过空代次边界；merge_parent=sid3 只入血缘 [4,3](branchlake.py:L149-L151,checks.py:L30)，Delta4 仅含调用者决议键(samples.py:L18,L31)，main 的 b 仍不存在而 side 的 b 保留；共享对象 1ab0321b 经 P3 引用计数保护，GC 不可删。",
    "C": "epoch 身份是 (ref,epoch) 而非 sid(I1)；sid 5->1->5 对应 epoch5/6 两条非空反向台账(5.3,checks.py:L39)，同 sid 不同 epoch 不被合并，ABA 在结构上消除。已取 epoch5 部分页(delivery=LEASED)期间重放 initial 命中 requests 重放先于代次判断(branchlake.py:L98-L101,samples.py:L34)，返回 epoch1 原回执且不产生新代次、不动游标；剩余页凭不可变 key_log 按 (main,5,page_no) 重拉且 content_hash 恒定，AP5 后继续 epoch6，逐代无重无漏。",
    "D": "提交顺序固定 AP5(warehouse COMMIT)->AP6(substore ACK)->Catalog 水位上报(6.4)；末页已提交而响应丢失时 wh_ledger=APPLIED 为事实源，重试只补幂等 ACK；确认已落 ack_record 而响应丢失则按 request_id 重放回执(复刻 branchlake.py:L98-L101)；租约接替 fence+1 后旧 worker 领页/确认均被 REJECT_FENCE，其手持旧页对 wh_staging 的迟到写因主键与 content_hash 幂等、且 AP5 后 staging 已清空而不触达 wh_data；新 worker 凭 ledger/delivery 决定补页或补确认，第6代恰好一次入账。",
    "E": "建索引两阶段(7.1)：SCANNING 期 builder 短事务续建且发布路径双写 key_log，INSERT OR IGNORE 与 I2 保证并发写内容逐字节相同；correct/reset 竞争由 _install 的 BEGIN IMMEDIATE(branchlake.py:L96) 与事务内 epoch 判定(branchlake.py:L103-L104)串行，输家 STALE_EPOCH。对象已写未发布在 Catalog 无痕、归 P4 宽限；已发布未追平则重启按 refs.epoch 续建，订阅者对未就绪代次收 RETRY_LATER 不跳代。交接完整性条件为同一事务内 C1(水位>=head)∧C2(ref_key_state 与 key_log 末像一致)∧C3(snap_dir 键数与 manifest 折叠一致)，不满足则 phase 不翻转。",
    "F": "保护谓词 P1-P5(8.1)：sub2 停 epoch4 且持 epoch5 部分页，经 sub_watermark 与 P2 保住 sid1(sid5->1 回退目标)及在途页；sub1 已确认 epoch6 保住 sid5 边界；side head sid3 及共享对象 1ab0321b 经 P1/P3 保留；late 中断对象先受 P4 在途宽限，回收器运行时不可删，重试时 put 命中同 oid(branchlake.py:L25) 并被 sid6 manifest 引用转为 P1，实测 orphan oid=ce04ca 在 sid6 manifest 中(1.5节)。保护最终解除后，无任何 ref/订阅/目录/在途引用的独占对象(如越过 epoch5 后 sid2 独占的 ab1386)由 AP7 墓碑事务到 AP8 真实 DELETE blobs 行，key_log/snap_dir 旧行按最低水位清理；已可见状态(refs/台账/wh_data)与恢复所需状态(lease/delivery/ack_record/staging/gc_tombstone)分离，不保留永久全历史。"
  }
}
```
