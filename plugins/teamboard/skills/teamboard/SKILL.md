---
name: teamboard
description: 当用户问「我的团队现在在做什么 / 项目进度怎么样 / 谁卡住了 / 打开团队面板」时使用。读取 TeamBoard 的项目、成员、任务与进度，并可以新建项目、派任务、改任务状态。
---

# TeamBoard 团队看板

数据来自本机 TeamBoard 服务。**不用手动起服务**：MCP 进程会在首次调用时自动拉起看板
（默认 `http://127.0.0.1:8787`，端口被占则顺延，数据落在 `$TEAMBOARD_HOME` 或
`~/.teamboard/board.db`）。服务没起来时工具会直接报错并给出 `server.log` 路径。

## 工具

| 工具 | 用途 |
|---|---|
| `board_overview` | 全部团队 + 总进度 + 阻塞数（回答"我有哪些团队"） |
| `project_status` | 单个团队：每个员工在做什么、任务分布、阻塞项。**默认精简**（未关任务 + 最近 3 条完成 + 计数），要历史任务才传 `full=true` |
| `update_project` | 改目标文字 / 验收标准 / 截止时间——顶部数字过时了当场改 |
| `run_criteria` | 真跑验收命令并回写红绿（退出码 0=绿），读数自动带时间 |
| `open_board` | 返回看板链接，用于在 Codex 面板打开 |
| `create_project` | 新建团队/项目（`lead` 填「项目总监」） |
| `propose_plan` | 提交计划（成员 + 任务）给用户审批，批准后自动落库 |
| `plan_status` | 查审批状态：pending / approved / rejected |
| `add_member` | 加员工；`model` 字段决定派活时用哪个模型 |
| `add_task` | 派任务（岗位、负责人、产出、验收标准） |
| `set_member_model` | 调某员工的模型 + 思考档位（总监专用） |
| `list_models` | 真实模型与各自支持的档位 |
| `update_task` | 改状态/进度/阻塞原因 |
| `heartbeat` | 刷新员工心跳（在岗证明） |
| `goal_audit` | 目标合规体检：阶段/产出/验收/证据哪里断了 |
| `member_duty` | 在场租约：spawn 时开、收工时关 |
| `set_member_thread` | **必做**：把子代理 threadId 绑到成员，面板点成员才能跳会话 |
| `submit_retro` | 时间压力 critical 时的强制复盘（六问） |
| `claim_resource` / `release_resource` | 独占资源租约（如 emulator-5556），防两个代理撞设备 |
| `add_hypothesis` | 记录假设 + 证伪命令，避免重复验证同一条死路 |
| `set_criteria` / `mark_criterion` | 把目标验收拆成可判定条目并标红绿 |
| `tb_api` | 内部：面板经 MCP 代理取数用，总监不用直接调 |

## 省上下文（和进度同等重要）

上下文是总监唯一的稀缺资源：`project_status` 全量一次 ≈ 25k token，读三次就没法干活了。

- **默认用精简版**（不传 `full`）：实测 99.8KB → 14.3KB。
- **不要把工具 JSON 原样贴回对话**，只贴结论和数字。
- **`progress.stale=true` 时先 `run_criteria`**：几小时前的读数比没有数字更糟。
- **验收 `cmd` 必须是可执行命令**，不要写「A + B 都要过」这种自然语言。

## 全员停摆（`idle`）与自驱（`autopilot`）——硬规则

**这个团队最真实的故障不是「任务难」，是「一个人都没在干活，但没人发现」。**
总监的注意力锁在自己那条线上，成员卡是「看一眼就走」的存在。靠自觉一定失效，只能靠机制。

| 层 | 字段 / 接口 | 触发 | 后果 |
|---|---|---|---|
| 检测 | `project_state.idle` | 员工 0 个 `working` 且还有未收工任务 | 顶部红条 + `project_status` 第一行 |
| 硬闸 | `idle_gate` | 停摆 ≥15 分钟 | `propose_plan` / `add_member` / 导出报告 403 `need_idle_fix` |
| 自驱 | `project_state.autopilot` | 用户不说话 | 心跳到点把这一轮拉起来，醒来先看 `idle` |

```json
"idle": {"sec": 2734, "human": "45分钟", "alert": true,
         "workers": 4, "working": 0, "open_tasks": 9,
         "silent": [{"name": "quest3_detector", "silent": 2734, "load": 2}]}
```

- `alert=true` → **先派活，再干别的**。是这一轮的第一件事，不是「稍后处理」。
- `silent[]` 按停得最久排序，直接点名，别自己再翻五张成员卡。
- 总监不算劳动力（服务端已排除 `is_lead`）：总监自己 `working` 不代表团队在跑。
- 放行的永远是**解闸动作**：`update_task` / `heartbeat` / `bind_member` / `member_duty`。

救的顺序不能反：

```text
① 还在的会话 → member_duty(on=true) + heartbeat
② 停掉的工位 → spawn_agent 重开 → bind_member（换会话不算换人，不走审批）
③ backlog 里挑优先级最高的 → update_task(status="doing", assignee_id=<mid>)
④ 确认 idle.alert=false，再回去干自己那条线
```

**不要用 `list_agents` 判断团队死活。** 它只列正在跑的会话，停掉的会从列表里凭空消失——
看上去一片正常，其实一个人都没在干。唯一可信的判据是 `idle`。

### 自驱心跳（`autopilot`）

没有心跳，这个项目只在用户开口时存在。

| 状态 | 含义 | 修法 |
|---|---|---|
| `found=false` | 没有心跳 | `POST /api/projects/<pid>/autopilot {"action":"enable"}` |
| `has_board_check=false` | 有心跳但 prompt 没让看团队板 | `{"action":"patch"}` 只追加一段，原 prompt 不动 |
| `too_slow=true` | 间隔 > 30 分钟 | `{"action":"enable","interval_min":15}` |

拉起团队时必须同时把心跳建好：`propose_plan` 会在 `warnings` 里点名。批了计划却没心跳，
等于签了一份不会执行的合同。

### 全员对齐（`align`）

`idle` 管「有没有人在干活」，`align` 管**「干活的人挂的是哪一条」**。
有人跑着、看板上却没有他的在办任务时，`idle` 是绿的，而进度/工时/卡点全是空气。

```json
"align": {"ok": false, "checked": 4,
          "unlogged":  [{"name":"battle_srv","human":"54分钟"}],
          "ghost":     [{"name":"quest3_detector","task":"2001 客栈判定圈","human":"15分钟"}],
          "nosession": [{"name":"battle_e2e","task":"event_6820 剧本战斗"}]}
```

| 清单 | 含义 | 唯一解法 |
|---|---|---|
| `unlogged` | 会话在跑，名下却没有 `doing/blocked/review` 任务（≥10 分钟） | `update_task(status="doing", assignee_id=<mid>)` + `member_duty(on=true)` |
| `ghost` | 任务挂着 `doing`，主人已不在跑（≥2 分钟） | 收回 `backlog`，或重开子代理 + `bind_member` |
| `nosession` | 名下有活，工位没绑会话线程 | `bind_member(thread_id="<全 UUID>")` |

`ok=false` → `propose_plan` / `add_member` / 导出报告 403 `need_align`；
解闸动作（`update_task` / `member_duty` / `bind_member` / `heartbeat`）永远放行。
`align.derail` 只报不拦——脱轨时要允许总监重排计划。

三条纪律：**开工先挂号**（第一个动作是 `update_task(doing)`，不是先看代码）、
**收工就摘牌**（`done` + `member_duty(on=false)`）、**换会话不算换人**（`bind_member` 不走审批）。

### 审批回执（`need_ack` / `ack`）

用户在面板里点了批准/驳回，不等于总监知道了。那一轮常常已经结束，下一条消息
如果不强制回执，它会当没发生——照旧按旧编制跑，而用户以为已经生效。

```json
"need_ack": [{"kind": "staff", "id": "stf_1a2b3c", "label": "编制变更",
              "decision_cn": "已批准", "title": "加人：后端（缺人）"}]
```

`need_ack` 非空时：先在会话里回「收到」+ 下一步，再 `ack(kind, id, note)`。
欠回执期间 `update_task(status=...)` / `propose_plan` / 导报告 一律 403 `need_ack`；
只改 `progress` 不受影响（报进度不是推进）。

## 拉起团队的硬流程（缺一项，服务端直接拒绝）

用户说「拉起一个团队做 X」时按顺序走，不许跳步。

### 1. `create_project` —— 先定项目总时长
必填 `name` / `goal` / `goal_criteria` / **`deadline`**。
没有 `deadline` 就没有时间压力闸，也不会触发强制复盘——服务端会直接拒。

### 2. `propose_plan` —— 人数方案 + 任务清单 + 用时规划
必填 `goal` / `goal_criteria` / `members` / `tasks`。服务端 `validate_plan` 逐条核对：

| 检查项 | 规则 | 不满足 |
|---|---|---|
| 人数方案 | `members` ≥ 1，每人必须有**中文岗位** | 拒绝 |
| 编制上限 | 默认 5 人（不含总监），第 6 人起要 `size_approved` + 审批二次确认 | 拒绝 |
| 任务数量 | `tasks` ≥ 1；建议任务数 ≥ 人数 | warning |
| **任务用时** | 每条任务 `estimate` 必填且 > 0（人时） | **拒绝** |
| 依赖 | 每条任务显式写 `depends_on_title`（无依赖写空串） | 拒绝 |
| 负责人 | 必须出现在 `members` 里 | 拒绝 |
| 产出 / 验收 | `deliverable` + `dod` 必填 | 拒绝 |

返回值里的 `warnings` 可能是：工时超预算、任务数少于人数、项目已过截止时间。
**warnings 不阻塞提交，但必须转述给用户**，让他在批准前决定砍任务 / 加人 / 推迟截止。

### 3. 等用户批准（`plan_status` → approved）
审批面板弹出，用户点批准后成员和任务才落库。**没批准不许 spawn 子代理。**

### 4. spawn 后立刻落座（不落座 = 没开工）

子代理不会自己出现在看板上。看板靠两条事实认人：成员行上有 `thread_id`
（谁坐这把椅子），那条 thread 的会话在写（这个人还活着）。只 spawn 不登记 =
子代理在跑、在烧 token、在产出文件，但看板**一行都不会多**：派不了活、记不了工时、
点不开会话，用户看到的人数与进度全是悬空的。

拿到 threadId 后，**派任何活之前**先做这两条：

```
bind_member(member="<工位名>", thread_id="<全 UUID>")   # 落座
member_duty(member="<工位名>", on=true, minutes=45)     # 在岗租约
```

- 传**全 UUID**：短 id 只做查询便利，不参与存活判定，也打不开会话深链。
- 工位名 ≠ 会话。换会话接着干还是 `bind_member` 到原来那个人；新建同名员工会被服务端拒。
- 收工 `member_duty(on=false)`，否则看板会继续把这个人算在岗 45 分钟。
- 子代理 prompt 第一行写清它的工位名；重开时看板靠这个名字把新会话接回原工位。

`board_overview` / `resume_brief` 第一行出现「⚠ 有 N 个会话在跑但没登记工位」
就是在提醒这件事——先落座再干别的。

### 5. `member_duty` 开 / 关在场租约
spawn 时 `on:true`，收工 `on:false`。它决定面板显示「工作中」还是「摸鱼中」。

### 6. 时间压力到 critical（剩余 ≤30%）→ 先 `submit_retro`
服务端会硬拦「加人 / 新计划 / 导出报告」，返回 `need_retro`。
复盘六问必须答：提速动作（≥3 条，各带预计省多少分钟）、能否按期、加不加人、
谁在摸鱼、砍哪条、最大风险 + 兜底方案。

### 7. 动态调任务以匹配总时长
每次复盘、每次 `update_task` 后看 `pressure`：

| 档位 | 动作 |
|---|---|
| `watch`（≤50%） | 砍 backlog 里优先级最低的 |
| `critical`（≤30%） | 只留能验收主线的任务，其余标 `abandoned` 并写原因 |
| `overdue` | 先改 `deadline`，不要假装还能按期 |

## 在岗判定（硬规则，别把在干活的人显示成离线）

看板状态有两个证据源，优先级固定：

1. **任务事实优先**：成员名下有 `doing` 任务 → 显示「工作中 · 计时 Xm」，**不看心跳**。
2. **心跳只决定空闲/离线**：没有 doing 任务时，心跳 < 5 分钟＝「空闲」，否则「离线 Xm」。
3. 在干活但没上报心跳的，会额外标「心跳 Nm 前」——这是提示总监去刷新，不是离线结论。

所以派活时必做三件事：

1. `spawn_agent` / `create_thread` 的 prompt 里写明：**开工第一件事**
   `update_task(task="<任务id>", status="doing")`，交付时 `status="done"`，
   中途每完成一步 `heartbeat(members=["<成员名>"])`。
2. 总监自己每次查状态（`board_overview` / `project_status`）会自动给自己刷心跳。
3. `wait_agent` 返回后立刻 `heartbeat`，把刚干完活的成员标记为在岗；
   子代理确实停了才让它自然掉线。

**禁止**为了好看手动 `PATCH member state=working` 或批量刷心跳假装在线——
看板显示的必须是真实状态。

## 只读看板（硬规则）

用户侧的看板只用于看进度和审批计划：不能拖拽、不能点开编辑、不能加人加任务。
所有编辑由项目总监执行；用户只跟总监对话。调模型/档位用 `set_member_model`，
先用 `list_models` 确认真实档位，不虚构。

## 计划审批（硬规则）

1. 拉起团队先 `create_project` → `propose_plan`，**不要先建人建任务**。
2. 用户在看板里点「批准并开工」后才算授权；在此之前不派活、不 `create_thread`。
3. 默认团队上限 **5 人**，超过要在审批面板二次确认扩编；`add_member` 加第 6 人会返回
   `need_approval`，必须问过用户再带 `size_approved=true`。
4. 岗位名一律中文；主线程岗位叫「项目总监」。

## 员工与模型（硬规则）

员工字段：姓名、岗位、类型（AI 子代理 / 真人）、模型、思考档位、并行上限。

1. **负责人 = 当前会话模型**，不单独选模型、不在看板上编造模型名。
2. **`model` 为空 = 跟随当前会话模型**：子代理不传覆盖参数，直接继承父代理。
   这是默认值，也是最省的做法；看板会显示「跟随会话模型」。
3. **加员工前必须问用户**：用当前会话模型，还是指定模型 + 档位。
   用户没明确说之前，`add_member` 的 `model` / `effort` 留空。
4. 用户要指定时，先 `curl -s localhost:8787/api/models` 拿真实目录，并带 `model_confirmed=true`
   （服务端会拒绝未经确认的 model），
   只允许该模型真实支持的档位（每个模型不一样，不许虚构）。
5. 禁止负责人自作主张给成员挑模型；看板上写的模型必须等于实际调用时传的模型。
   一旦发现不一致，先把看板改成真实值，再继续干活。

派活规则：

1. 派活时 `spawn_agent` / `create_thread` 不传 `model` 就继承父模型；
   只有该成员被用户指定了模型，才传 `<该成员的 model>`。
2. prompt 里写明：项目 id、任务 id、验收标准（dod）、交付物路径。
3. 子代理交付后按 dod 逐条验证（跑测试 / 查文件 / 复现），通过才
   `update_task(task=<id>, status="done")`；不通过就 `send_input` 退回修改。
4. 同一成员已有 `doing` 任务且达到并行上限时，先排 `backlog`，不要硬塞。

## 回答方式

用户问"团队在做什么"时：先 `board_overview`，再对每个团队 `project_status`，
按「团队 → 成员 → 当前任务 → 进度」的结构回答，阻塞项单独列出。
不要把所有任务平铺，只讲正在发生的和卡住的。

## 内嵌面板

用户说「打开团队面板 / 看看团队」时，调用 `show_team_panel`（无参数）。
它会在对话里内嵌渲染一个可交互面板：每个团队一张卡，展开就是成员列表
（谁在做什么、任务进度、谁阻塞了）。不要再额外输出一遍长文本清单。

## 目标页（用户第一眼要看的东西）

看板第一个 tab 是「目标」，结构固定为：**目标 → 阶段（milestone）→ 任务 → 依赖**。

- 目标与验收标准来自 `projects.goal` / `projects.goal_criteria`，`propose_plan` 必须填。
- 每条任务必须有 `milestone`，目标页按阶段分组；没填的会落到「未分阶段」。
- 目标页顶部会亮红：卡住的环节、等上游的依赖、停滞任务、交付物缺失、资源冲突。

## 计划字段（硬校验，缺一项服务端就 400）

`propose_plan` 必须给全：

| 层级 | 必填 |
|---|---|
| 目标 | `goal`、`goal_criteria` |
| 成员 | `name`、`role`（中文） |
| 任务 | `title`、`role`、`assignee`（成员姓名）、`milestone`、`deliverable`、`dod`、`depends_on_title`（无依赖写 `""`） |

可选：`priority`、`estimate`（加权进度用）、`resource`、`evidence`。

## 八个硬闸

1. **交付物存在性**：`deliverable` 支持 `a|b`、`a + b`、`{X,Y}.md`、通配符；
   相对路径按 `project.workdir` 解析。一个文件都不存在时，`status=done` 会被拒绝（400）。
2. **证据等级**：`evidence=inferred` 的任务进度不能 ≥90%；要标完成得先补观测证据。
3. **资源独占**：`resource`（如 `emulator-5556`）同时被两个 `doing` 任务占用时，看板红条告警。
   派活前先查目标页的资源冲突区，别让两个子代理抢同一台设备。
4. **全员停摆**：员工 0 个在干活、且还有未收工任务，累计 ≥15 分钟 → `propose_plan` /
   `add_member` / 导出报告 一律 403（`need_idle_fix`）。解闸动作照常放行：
   `update_task` / `heartbeat` / `bind_member` / `member_duty`。
5. **全员对齐**：有人在跑却没挂号、任务主人已停、有活没绑会话 → 同样 403（`need_align`），
   返回三张清单 + `next_actions`；三项都空才算对齐（`align.ok=true`）。
6. **审批回执**：用户点过批准/驳回、总监没 `ack` → 改任务状态 / 提新计划 / 导报告 403
   `need_ack`。决定不落进脑子，等于没决定。
7. **执行痕迹**：从没进过 `doing`、也没计过时的任务不许标 `done`（400 `need_execution`）。
   没有 `task_sessions` 记录、`time_spent=0`、`doing_since=0`，就说明没人在这条任务上跑过，
   「写完了」不等于「做完了」。任务**不能建成 `done`**，建成 `doing` 也要过 `ack_gate`
   与依赖顺序。合法路径只有一条：派人 → 挂 `doing` → 真跑 → 带证据 `PATCH status=done`。
8. **续跑**：上次运行留下的 `doing` 任务主人已经不在了 → 派新活 403 `need_resume`。「继续」不等于「接着干」：先 `resume_brief`，把烂尾逐条处置（收回 backlog / 转给活人 / 重开子代理 + `bind_member` / 转 blocked），处置完闸自动放行。另外 `doing` 必须有 `assignee_id`，没有主在办任务一律 400 `need_assignee`。

## 催办（nudge）：插件必须自己开口

闸只在总监伸手时咬人；他不伸手，项目就停在那里——这是「总监从不看团队」的制度化成因。
所以除拦之外还要催：`project_state.nudge` 把「此刻必须有人做的事」排成一张清单，
**跟着每一次 MCP 工具返回**顶到最前面（`⏰ 催办 N 条`），不靠他记得去查。

| 字段 | 含义 |
|---|---|
| `items[]` | `who`（该谁做）/ `what`（什么事）/ `say`（可直接照做的那一句）/ `human`（挂了多久） |
| `level` | `ok` / `warn` / `shout`（停摆、没对齐、欠回执 → `shout`，同时触发 403） |
| `must` | 第一条的 `say`：这一轮该做的第一个动作 |
| `by_who` | 按人聚合，派活时按人顺手 |

触发源：任务主人已停 / 在跑没挂号 / 有活没绑会话 / 有人闲着还有待办 / `doing` 停滞 ≥30 分钟 /
脱轨 / 欠回执 / 没有自驱心跳 / 心跳不看团队板。

**没有静音开关**：不存在 ack/mute 接口，唯一让它变短的办法是把事做掉。
自驱心跳的 prompt 第 0 步就是「清空 nudge」——醒来先看这张单子，再谈别的。

## 断点续跑：不许从头来

任务被中断（软件退出、换人接手）后，「重开一遍」是这类项目里最贵的浪费：13 小时的活重做一遍，
等于团队凭空少一半人。所以三件事写死在服务端：

| 规则 | 触发 | 返回 |
|---|---|---|
| 复盘才能开工 | 任务已有 `progress>0` 或断点，重新 `doing` / 换负责人 | 400 `need_resume_note`（带 `checkpoint` 原文） |
| 断点留档 | 任何一次带 `resume_note` 的更新 | 写进 `checkpoint` + `checkpoint_at`，面板任务详情可见 |
| 进度只能往上 | 进度回退 | 400 `need_confirm_restart`；带 `confirm_restart=true` + `restart_reason` 才放行，并记 `task.restart` 事件 |

`resume_note` 写什么：**做到哪、哪条路已经排除、下一步是什么**。它是给下一个接手的人看的，
不是给自己看的。看板的「断点」字段就是它的存档。

### 宿主层钩子（可选，强烈建议）

插件管得住交付，管不住「先干两小时再说」。Codex 的 `PreToolUse` 钩子跑在工具执行之前：
`hooks/pretooluse.py` 会先问看板「这条会话挂号了吗」——没挂号就拦下，并给出该调的
`update_task(status='doing', assignee_id=...)`。总监、未登记会话、非动手类工具、任何异常全部放行。
注册方式见开源仓 README（Codex 会要求你信任这个 hook 一次）。

## 进度与停滞

- 总进度按 `estimate` 加权，不是任务数平均：新增 backlog 不会让进度倒退。
- `doing` 任务超过 30 分钟没更新 → 黄「停滞」，超过 90 分钟 → 红。
  子代理每完成一步就 `update_task`（哪怕只改 progress），停滞计时才会重置。

## 怎么保证任务真的围绕目标（三道闸，从软到硬）

**光靠 prompt 一定跑偏。** 目标要落到写路径上，模型才没法绕过：

| 层 | 机制 | 谁拦 |
|---|---|---|
| 计划 | `propose_plan` 必须带 `goal` + `goal_criteria`，每条任务必须带 `milestone` / `deliverable` / `dod` / `depends_on_title` | 服务端 400，列缺失字段 |
| 任务 | 项目有目标时，`add_task` 不给 `milestone+deliverable+dod` 直接拒绝；`update_task` 不许把 `milestone` 清空 | 服务端 400 |
| 交付 | `review/done` 的交付物必须真实存在（按 `workdir` 解析，支持 `{A,B}.md`、通配符）；`evidence=inferred` 不能 ≥90% | 服务端 400 |
| 审计 | `goal_audit` 给出目标合规分 + 不达标清单；目标页红条实时亮 | 总监每次派活/交付前跑 |

### 派活 prompt 模板（每条子代理都必须带这三段）

```text
目标：<项目 goal 原文>
你负责的阶段：<milestone>
你这条任务：<title>
产出（必须真的写文件）：<deliverable>
验收（逐条可判）：<dod>
依赖：<depends_on_title 或「无」>
开工先 update_task(task="<id>", status="doing")，每完成一步 update_task(progress=…, deliverable=…, evidence=observed)，
交付前跑 goal_audit，没通过就继续补，不要直接标 done。
```

### 目标偏离的三种典型信号

1. 目标页「未挂目标 / 未分阶段」出现新任务 → 有人绕过计划塞了私活。
2. `goal_audit` 里 `no_dod` / `no_deliverable` 变多 → 任务写得含糊，验收会扯皮。
3. `weak_evidence` → 拿推断当结论，交付前必须补实测。

总监每周（或每个里程碑收口时）跑一次 `goal_audit`，把不合规项补完再往下走；
用户只看目标页的合规分和红条，不需要自己翻任务。

## 在岗：用租约，不靠刷心跳

`spawn_agent` 之后立刻 `member_duty(member="<姓名>", on=true, minutes=45)`：
这段时间内该成员算在岗（空闲或工作中），不需要手工 curl 心跳。
`wait_agent` 回来、或确认子代理停了，`member_duty(member="<姓名>", on=false)`。

没有租约、也没有心跳、也没有 doing 任务 → 才显示离线。这是唯一的离线判定路径。

## 资源独占是硬约束（不是提示）

`resource` 被另一个 `doing` 任务占着时：

- `add_task` / `update_task` 直接返回 400 + `need_approval` + 占用方标题；
- 确实要共享（例如两个任务读同一份只读文件）才带 `force=true`，并且要在计划里写明原因；
- 任务收工（done/review/blocked）后占用自动释放。

## 剩余工时（estimate 的单位是人时）

目标页顶部固定显示：

- `Σ(estimate × (100-progress)/100)` = 剩余估算小时；
- `deadline − now` = 距截止小时；
- 剩余 > 距截止 → 红色「已超支」。

`estimate` 默认是 1，如果所有任务都是 1，页面会标「估算未填」，别拿这个数当真实工期。
派活时给每条任务填人时（0.5 / 2 / 8），这个数字才有意义。

## 项目倒计时（顶栏 + 目标页共用同一个时钟）

- 顶栏右侧和「目标」页顶部显示同一个每秒走字的倒计时：`剩 1d 23:37:58`。
- 截止只写日期（`2026-10-30`）按当天 **23:59:59** 算；写 `2026-10-30 18:00` 按该时刻算。
- 时间已用 ≥65% 转琥珀，≥90% 转珊瑚并呼吸；已过截止显示「逾期 」。
- 目标页同时给 `剩余工时估 6.5h · 可用 47.6h`：可用 < 估算即超支，标红。
- 倒计时只在 `estimate` 填过之后才有意义；全是默认 1 时别拿它承诺工期。
- **派活时把倒计时写进子代理 prompt 的第一行**（`距截止剩 9.4h / 剩余工时 9.0h`）；
  子代理自己不看看板，危机感只能由总监在任务说明里传达。
- 紧迫度算法：时间已用 ≥65% 或剩余时间 < 剩余工时 ×1.2 → 琥珀；≥90% 或已超支 → 珊瑚。

## 目标 ↔ 阶段 ↔ 任务（口径，别再搞错）

- 一个项目只有一个目标，挂在项目上。任务在项目里 = 在目标下，**不会出现「未挂目标」**。
- 任务级唯一会缺的是「阶段（`milestone`）」，它在目标页显示成「未归阶段」**一行汇总**，不刷满每张卡。
- `goal_progress` = 全部任务的加权完成度（目标达成）；
  `stage_coverage` = 已归阶段的任务占比（拆解质量，不是达成度）。
- 新任务必须带 `milestone + deliverable + dod`，缺一项 `add_task` 直接 400。

## 排期：每人串行、跨人并行（倒计时就是从这来的）

看板不再用「现在 + estimate」拍脑袋，而是按依赖和人力真实排一遍：

- 剩余工作量 = `estimate − 已烧净工时`（`time_spent` + 当前段）；
- **同一个人手里的任务排队**：第二件要等第一件交；
- **不同人并行**：两条线同时开工，各算各的；
- 有 `depends_on` 的，必须等上游的预计交付时刻；
- **项目收工时刻 = 所有并行分支里最晚的那条**，不是各分支相加；
- 目标页的「串行排期预计收工 05:37（比截止早 4.2h）」就是这个值。

每条任务卡片上的 `⏳ 2h15m` 是 `eta_at − now`；`⏳ … 待开工` 表示还在排队。
`due_at` 是硬截止（可选），一般不填；填了它优先于排期。

## 暂停：等设备/等审批不算工时

```json
{"task":"装备穿不上","paused":true}    // 等 emulator-5556 交回
{"task":"装备穿不上","paused":false}   // 拿回设备再继续
```

暂停会关掉计时、`elapsed` 归零，恢复后重新开一段；等待时长记在 `paused_sec`，
看板上写「暂停中 12m」。**别用 blocked 代替暂停**：blocked 是任务性质变了，暂停只是人不在。

## 超支了往哪砍

- `over_top`：超预算最多的 3 条任务（超多少秒、预算多少、已花多少），目标页直接列出来；
- 项目级 `over_budget` 只说明总量超了，砍哪条看 `over_top`；
- `estimate_bias`：已完成且计过时的任务「估算 vs 实花」偏差，用来校准下一次估时
  （只有真开过计时的任务才算，历史任务不计入）。

## 编制：5 个员工，不含项目总监

- `TEAM_CAP = 5` 指的是**员工**；项目总监（`is_lead=1`）不占编制；
- 第 6 个员工 `add_member` 返回 400 + `need_approval`，必须先问用户，确认后带 `size_approved=true`；
- 计划审批里的 `over_cap` 同样只算员工；
- 注意：编制是「人头」，并发执行槽可能更少；排在后面的子代理是排队状态，不是卡死。


## 设备租约（独占资源不是靠自觉）

```bash
POST /api/projects/{pid}/resources/{name}/claim   {"task_id":"…","member":"…","minutes":60}
POST /api/projects/{pid}/resources/{name}/release {"task_id":"…"}
```

- 没租约就拿不要碰设备；被占时返回 400 + `holder` + `left_sec`，**过期自动可抢**；
- 目标页顶部会出现 `🔒 emulator-5556 · 持有任务 · 剩 42m · 排队 2`；
- 真要共享才带 `force=true`，并在计划里写原因。

## 置 done 的三道硬闸

1. `evidence` 必填（observed / inferred / assumed）；
2. `evidence_path` 必填，且**文件/目录必须真实存在**（支持 `a|b`、通配符）；
3. **有执行痕迹**：必须真的进过 `doing`（`task_sessions` 有记录或 `time_spent>0`），
   否则 400 `need_execution`。

缺任一项 → 400 + `need_evidence`。确实要跳过才带 `force=true`。
`inferred` 仍然不许 ≥90%。

## 放弃也要留证据（abandoned）

- `{"status":"abandoned","abandoned_reason":"hook 打不到框架层"}`，原因必填；
- 看板多一列「已放弃」，没内容时自动隐藏；
- 换会话接手时先读这一列，别把排除过的路再走一遍。

## 假设记录（防重复验证）

```bash
POST  /api/tasks/{tid}/hypotheses  {"claim":"回弹是状态门 3002 未复位","command":"…","verdict":"refuted","by":"so_refresh"}
PATCH /api/hypotheses/{id}         {"verdict":"confirmed","note":"…"}
```

- `verdict` = open / confirmed / refuted；
- 同一说法（去空格标点后）第二次出现 → 返回 `dupe_of` + 警告「已证伪，别再跑」；
- 任务详情里按条列出：假设 / 证伪命令 / 结论 / 谁跑的。

## 验收闸门（红绿，替代口头汇报）

```bash
POST  /api/projects/{pid}/criteria {"items":[{"text":"冷启 ≤60s","cmd":"adb shell am start …"}, …]}
PATCH /api/criteria/{cid}          {"status":"green","output":"冷启 41s"}
```

- 目标页顶部 `验收闸门 3/7 绿`，红的直接列出来；
- 命令由总监自己跑（看板不自动执行命令），跑完把结论写回；
- 所有条目绿 = 可以交付，不用听任何人转述。

## 顶部那一行「卡点」

`blocker` 会从「阻塞 > 等上游 > 停滞」里挑影响面最大的一条：
`卡点 = quest3 触发无对白 · 影响 4 条链 · 已卡 3.2h`。
接手第一眼就看这行，不用读 KANBAN.md。

## 阻塞传播

A→B→C 链上，A 没完成时 B 和 C 都会显示「等 A / 等 N 个上游」，
不是只看直接父任务。判断"下一个能开工的是谁"看目标页，不要自己推。

## 时间压力闸（每次轮询先看这个）

看板按剩余时间 / 排期预测自动分级：`ok → watch(≤50%) → critical(≤30%) → overdue`。
到 **critical 或 overdue** 时看板会硬拦三件事：**加人扩编、新计划审批、导出交付报告**，
并返回 `need_retro: true`。这不是建议，是 403。

被拦时唯一出路：`retro_status` 看第几轮 → `submit_retro` 交六问：

| key | 必答内容 |
|---|---|
| speed | ≥3 条具体提速动作 + 每条省多少分钟（「加把劲」不算） |
| deadline | 能/不能按期 + 差几小时 + 卡哪条链（引用看板数字） |
| staffing | 加不加人、几个、什么岗位、接谁手上的哪条、省多少；不加写理由 |
| utilization | 谁在摸鱼、摸多久、怎么处置（看板给了实测利用率，编造会被对账） |
| scope | 砍哪条任务/验收项；不砍写「不砍」+ 理由 |
| risk | 最可能失败的一件事 + 兜底 |

每题 ≥8 字，短于 8 字直接 400。交完状态是 `submitted`，**必须等用户在面板点批准**
（`approved`）才解锁；用户驳回会带回原因，重写再交。批准后有效期 60 分钟（overdue 30 分钟），
到点还在高压就自动再来一轮。

## 员工的会话链接

spawn 子代理拿到 threadId 后立刻 `set_member_thread(member=…, thread_id=…)`。
登记过的员工，用户在看板里点一下就能打开他的会话、看他在做什么；
没登记的，看板会按「成员名出现在会话前 60 行」自动找候选，用户点「绑定并打开」即可。
不给链接 = 用户只能靠你转述，等于把看板最值钱的部分废掉。
