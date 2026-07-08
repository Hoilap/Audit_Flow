# Bank_ledger_match (5017)

## 设计原则
这套工作流用于把银行流水和新纪元序时账清洗成标准 CSV，自动匹配银行流水与账面记录，并把结果填入资金流水专项核查底稿。
- 数据清洗
  - 银行流水格式不稳定：优先使用已知固定解析器；未知格式可让 LLM 生成 Python 解析脚本，并缓存到 `generated_parsers/` 复用。
  - 新纪元序时账格式稳定：使用固定解析器 `xinjiyuan_bank_ledger`，不调用 LLM。
- 匹配流程
  - 匹配前引入月度总数匹配，判断数据是否缺漏
  - 匹配时引入LLM
  - 匹配结果保留置信度、未匹配清单和低置信信息，方便审计人员复核。
  - 原始 Excel 不改动，输出统一写入 `outputs/`。
- 填入底稿
  - 使用固定解析器，填入底稿


## 匹配原则
```mermaid
---
title: BLM 银企对账匹配流程
---

flowchart TD
    subgraph API["路由层 routes_blm.py"]
        A["POST /workflow/bank_ledger_match/match"] --> B["_build_full_config() 合并 llm.yml + matching.yml + task.yml"]
        B --> C["_apply_parser_to_config() 根据 parser 设置匹配策略"]
    end

    subgraph PARSER["Parser → 策略映射 _apply_parser_to_config()"]
        C --> D{"parser 类型?"}
        D -->|"llm_init / llm_regenerate"| E["full_rematch = True\nLLM enabled = True\ndeduplicate_candidates = True\n所有规则 Pass 跳过"]
        D -->|"llm / llm_step"| F["full_rematch = False\nLLM enabled = True\n规则 Pass 全部执行"]
        D -->|"script / icbc_historydetail 等"| G["full_rematch = False\nLLM enabled = False\n仅执行规则 Pass"]
    end

    subgraph IO["match_to_csv() — 数据加载与输出"]
        H["读取 clean/bank_transactions.csv"] --> I["读取 clean/ledger_entries.csv"]
        I --> J["match_transactions() 核心匹配"]
    end

    E --> H
    F --> H
    G --> H

    subgraph BUILD["_records() — 构建 Record 对象"]
        J --> K["bank_records = _records(bank_df, 'bank', 'txn_id')"]
        J --> L["ledger_records = _records(ledger_df, 'ledger', 'entry_id')"]
        K --> M["bank_by_id = {r.id: r} — ID 唯一性保证无覆盖"]
        L --> N["ledger_by_id = {r.id: r}"]
        M --> O["初始化 used_bank / used_ledger / groups / heuristic_stats"]
        N --> O
    end

    subgraph GATE["full_rematch 分叉"]
        O --> P{"full_rematch == True?"}
        P -->|"YES (llm_init/llm_regenerate)"| Q["🛑 跳过全部规则匹配\n打印: '完全重新匹配模式...'"]
        P -->|"NO (llm/script 等)"| R["✅ 执行全部规则匹配\nPass 0 → 0.5 → 1 → 2 → 3 → 3.5 → 4"]
    end

    subgraph PASSES["规则匹配 Pass（仅 full_rematch=False）"]
        subgraph P0["Pass 0 — 手续费聚合"]
            R --> P0_S["_fee_aggregation_match()\n聚合多笔小额手续费\n匹配类型: fee_aggregation"]
        end
        subgraph P05["Pass 0.5 — 跨账号调拨"]
            P0_S --> P05_S["_cross_account_transfer_match()\n匹配同名不同账号之间的转账\n匹配类型: cross_account_transfer"]
        end
        subgraph P1["Pass 1 — 一对一匹配"]
            P05_S --> P1_S["遍历 bank × ledger 全量成对"]
            P1_S --> P1_C{"_candidate_ok()\n同月 + 同方向 + 同账号 + 日期容差 ≤ 30天"}
            P1_C -->|"通过"| P1_SC["_pair_score()\n0.52×金额 + 0.25×日期 + 0.23×文本相似度"]
            P1_SC --> P1_TH{"score ≥ one_to_one_min (0.58)?"}
            P1_TH -->|"YES"| P1_ADD["加入候选池, 按分数降序排列"]
            P1_TH -->|"NO"| P1_SKIP["丢弃"]
            P1_C -->|"不通过"| P1_SKIP
            P1_ADD --> P1_GM["贪心匹配: 从高分到低分,\n双方均未被使用时建组"]
            P1_GM --> P1_MG["_make_group(config, 'one_to_one', ...)\n标记 used_bank / used_ledger"]
        end
        subgraph P2["Pass 2 — 一对多 (1 Bank → N Ledger)"]
            P1_MG --> P2_S["对每个未使用 bank:\n查找未使用 ledger 候选"]
            P2_S --> P2_C{"_candidate_ok(..., ignore_amount=True)\n使用 group_date_tol"}
            P2_C -->|"通过"| P2_RANK["_rank_group_candidates() 排序\n截断到 pool_limit (默认 24)"]
            P2_RANK --> P2_SS["_find_subset() 子集和搜索\n匹配 ledger 合计 = bank 金额\n最大组大小: group_max_size (默认 6)"]
            P2_SS --> P2_FOUND{"找到子集?"}
            P2_FOUND -->|"YES"| P2_MG["_make_group(config, 'one_to_many', ...)"]
            P2_FOUND -->|"NO"| P2_NEXT["下一个 bank"]
            P2_C -->|"不通过"| P2_NEXT
        end
        subgraph P3["Pass 3 — 多对一 (N Bank → 1 Ledger)"]
            P2_MG --> P3_S["对称逻辑: 对每个未使用 ledger\n查找未使用 bank 候选"]
            P3_S --> P3_SS["_find_subset() 子集和搜索\n匹配 bank 合计 = ledger 金额"]
            P3_SS --> P3_FOUND{"找到子集?"}
            P3_FOUND -->|"YES"| P3_MG["_make_group(config, 'many_to_one', ...)"]
            P3_FOUND -->|"NO"| P3_NEXT["下一个 ledger"]
        end
        subgraph P35["Pass 3.5 — 多对多 (窗口聚合)"]
            P3_MG --> P35_S["_daily_many_to_many()\n按日窗口枚举 bank/ledger 组合"]
            P35_S --> P35_MG["_make_group(config, 'many_to_many', ...)"]
        end
        subgraph P4["Pass 4 — 清理残差"]
            P35_MG --> P4_S["_cleanup_small_unmatched()\n放宽规则匹配小额未命中记录"]
        end
    end

    Q --> LLM_ENTRY
    P4_S --> LLM_ENTRY

    subgraph LLM["LLM 补充匹配 apply_llm_supplemental_matches()"]
        LLM_ENTRY["LLM enabled?"]
        LLM_ENTRY -->|"NO"| OUTPUT
        LLM_ENTRY -->|"YES"| LLM_FR{"full_rematch?"}
        LLM_FR -->|"YES"| LLM_CLR["清空 groups / used_bank / used_ledger\n全部记录参与候选构建"]
        LLM_FR -->|"NO"| LLM_SEL["仅未匹配记录参与候选构建"]
        
        LLM_CLR --> LLM_CAND["_build_candidates() 枚举候选"]
        LLM_SEL --> LLM_CAND

        subgraph STRATEGIES["7 种候选枚举策略"]
            S1["① one_to_one: 金额精确匹配 (≤8个/ledger)"]
            S2["② period many_to_one: 汇总入账整段匹配"]
            S3["③ subset-sum many_to_one: DFS 子集和 (≤22条)"]
            S4["④ relaxed amount_group: 宽窗口子集和 (≤200k节点)"]
            S5["⑤ manual time-window many_to_one: 365天窗口"]
            S6["⑥ manual many-to-many: 滑动窗口分段匹配"]
            S7["⑦ monthly_balance: 月度分桶全枚举"]
        end

        LLM_CAND --> STRATEGIES
        STRATEGIES --> LLM_DEDUP["去重 + 排序 + 截断到 max_candidates (60)"]

        LLM_DEDUP --> LLM_CACHE{"reuse_decisions\n且非 full_rematch?"}
        LLM_CACHE -->|"YES"| LLM_REPLAY["回放 llm_decisions.csv 缓存"]
        LLM_CACHE -->|"NO"| LLM_API["批量调用 LLM API\nbatch 发送候选给大模型判定"]
        LLM_REPLAY --> LLM_MERGE["合并缓存 + 新判定结果"]

        LLM_API --> LLM_MERGE
    end

    subgraph DECISION["LLM 判定分派"]
        LLM_MERGE --> DEC_D{"候选判定结果"}
        DEC_D -->|"review_only + 人工已审批"| DEC_ACC["_try_accept_candidate()\n验证无冲突 → 建组 → 标记已用"]
        DEC_D -->|"LLM approve + confidence ≥ 0.72"| DEC_ACC
        DEC_D -->|"LLM reject / confidence < 0.72"| DEC_REV["进入 manual_review_candidates.csv\n待人工复核"]
        DEC_D -->|"无 LLM 判定"| DEC_REV
        DEC_D -->|"候选间记录冲突"| DEC_REV
        
        DEC_ACC --> DEC_CHK{"_try_accept_candidate 成功?"}
        DEC_CHK -->|"YES (无冲突/金额OK)"| DEC_MG["_make_group() 建组\nstats.llm_approved += 1"]
        DEC_CHK -->|"NO (冲突/金额差)"| DEC_REV
    end

    DEC_MG --> OUTPUT
    DEC_REV --> OUTPUT

    subgraph OUTPUT["结果输出 match_transactions() 返回"]
        OUTPUT --> O1["matches_df = pd.DataFrame(groups)"]
        O1 --> O2["unmatched_bank = bank_df[txn_id not in used_bank]"]
        O2 --> O3["unmatched_ledger = ledger_df[entry_id not in used_ledger]"]
        O3 --> O4["写入 matches/ 目录"]
        O4 --> O5["matches.csv"]
        O4 --> O6["unmatched_bank.csv"]
        O4 --> O7["unmatched_ledger.csv"]
        O4 --> O8["llm_candidates.csv / llm_decisions.csv / manual_review_candidates.csv"]
        O4 --> O9["monthly_flow_check.csv"]
    end

    style E fill:#e74c3c,color:#fff
    style Q fill:#e74c3c,color:#fff
    style LLM_CLR fill:#e74c3c,color:#fff
    style P fill:#f39c12,color:#fff
    style DEC_ACC fill:#27ae60,color:#fff
    style DEC_REV fill:#f39c12,color:#fff
    style O5 fill:#2ecc71,color:#fff
    style O6 fill:#e67e22,color:#fff
    style O7 fill:#e67e22,color:#fff
```
```mermaid
flowchart TD
    A[输入文件] --> A1[银行流水: 格式可能不稳定]
    A --> A2[序时账: 新纪元导出, 格式稳定]

    A1 --> B1{银行流水解析器}
    B1 -->|稳定格式| C1[规则清洗为 bank_transactions.csv]
    B1 -->|不稳定格式| C2[LLM 生成/辅助 Python 清洗脚本]
    C2 --> C1

    A2 --> C3[固定解析器清洗为 ledger_entries.csv]

    C1 --> D[匹配前月度校验]
    C3 --> D
    D --> D1[按 账号 + 月份 + in/out 汇总]
    D1 --> D2{月度总额是否一致}
    D2 -->|不一致| D3[提前打印警告并写 monthly_flow_check.csv]
    D2 -->|一致| E[进入匹配流程]
    D3 --> E

    E --> P0
    subgraph "Heuristic 6-Pass Pipeline"
        P0["Pass 0: 手续费聚合<br/>银行逐笔扣费 → 序时账月度汇总"]
        P0 --> P05["Pass 0.5: 跨账号调拨<br/>摘要含'调拨' + 科目=银行存款"]
        P05 --> P1["Pass 1: 一对一<br/>同月 + 同方向 + 同账号 + 金额/日期/文本评分"]
        P1 --> P2["Pass 2: 一对多 (1:N)<br/>一条银行 = 多条序时账子集"]
        P2 --> P3["Pass 3: 多对一 (N:1)<br/>多条银行 = 一条序时账子集"]
        P3 --> P4["Pass 4: 同日多对多 (N:N)<br/>同日 + 同方向 + 同账号 + 总额相等"]
    end

    P4 --> G[得到初步 matches / unmatched]

    G --> H{仍有 unmatched<br/>且启用 LLM?}
    H -->|否| Z[如配置允许, 写入工作底稿]
    H -->|是| I[LLM 补充匹配]

    I --> I1[金额/日期/摘要候选]
    I --> I2[连续时间窗口枚举]
    I --> I3[月度平衡枚举: 月度残余总额相等时凑子集]

    I1 --> J1[LLM 判断是否可自动接受]
    I3 --> J2[LLM 写匹配失败原因和人工复核提示]

    I2 --> K[人工复核候选]
    J2 --> K

    J1 --> L{LLM 置信度足够?}
    L -->|是| M[自动写入 matches]
    L -->|否/拒绝/冲突| K

    K --> N[manual_review_candidates.csv]
    N --> O{人工 approve=true?}
    O -->|是| P[重新运行 match]
    P --> Q[批准候选写入 matches]
    O -->|否| R[保留 unmatched / 待解释]

    M --> S[更新 unmatched]
    Q --> S
    S --> T{是否全部匹配?}
    T -->|是| Z
    T -->|否| N

    classDef llm fill:#fff3cd,stroke:#d39e00,color:#111;
    classDef newpass fill:#d4edda,stroke:#28a745,color:#111;
    class C2,J1,J2 llm;
    class P0,P05 newpass;
```

## Heuristic 各 Pass 详细逻辑

```mermaid
flowchart LR
    subgraph "Pass 0 — 手续费聚合"
        direction TB
        F1["识别手续费记录<br/>关键词: 手续费 / SMSP / Service Charge<br/>金额 ≤ 1000 元"]
        F2["按 (账号, 月份, 方向) 分组"]
        F3["三级匹配策略"]
        F3a["Tier 1: 全池匹配<br/>池内所有bank手续费总和 ≈ ledger金额"]
        F3b["Tier 2: 贪心累加<br/>按日期近→远逐步加入直到金额吻合"]
        F3c["Tier 3: 子集DFS<br/>max_size=8, node_limit=2M"]
        F1 --> F2 --> F3
        F3 --> F3a
        F3 --> F3b
        F3 --> F3c
    end

    subgraph "Pass 0.5 — 跨账号调拨"
        direction TB
        T1["识别调拨记录<br/>摘要含'调拨' + 科目='银行存款'"]
        T2["在所有账号的bank中搜索<br/>反向flow + 同金额(±0.01) + ±3天"]
        T3["跨账号候选优先<br/>避免同账号false match"]
        T4["唯一候选直接匹配<br/>多候选按日期+相似度排序"]
        T1 --> T2 --> T3 --> T4
    end

    subgraph "Pass 1 — 一对一"
        direction TB
        O1["前置过滤<br/>同自然月 + 同flow + 同账号<br/>日期差 ≤ tolerance<br/>金额差 ≤ tolerance"]
        O2["评分<br/>0.52×金额 + 0.25×日期 + 0.23×文本"]
        O3["按评分降序贪心匹配<br/>score ≥ 0.58 才接受"]
        O1 --> O2 --> O3
    end

    subgraph "Pass 2/3 — 子集搜索"
        direction TB
        S1["对每条未匹配anchor<br/>收集同月同账号候选池"]
        S2["按评分排序取 top-24"]
        S3["DFS子集搜索<br/>max_size=6, node_limit=2M<br/>找金额总和 ≈ anchor的子集"]
        S1 --> S2 --> S3
    end

    subgraph "Pass 4 — 同日 N:N"
        direction TB
        N1["按 (日期, flow, 账号) 分组"]
        N2["同日同组 bank+ledger<br/>总额差 ≤ tolerance"]
        N3["整组匹配"]
        N1 --> N2 --> N3
    end
```

### 各 Pass 设计要点

| Pass | 解决的问题 | 触发条件 | 关键参数 |
|------|-----------|---------|---------|
| **0** | 银行逐笔扣手续费（4.5~100元×20笔），序时账按月汇总为1条 | 关键词 `手续费/SMSP/Service Charge` 且金额 ≤ 1000元 | 三级策略: 全池→贪心→DFS(8) |
| **0.5** | 跨银行账号资金调拨，同账号约束无法匹配 | 摘要含 `调拨` 且会计科目 = `银行存款` | 反向flow + ±3天 + 跨账号优先 |
| **1** | 最常见的逐笔对应交易 | 同月 + 同方向 + 同账号 + 金额/日期在容差内 | score ≥ 0.58, 权重 0.52/0.25/0.23 |
| **2/3** | 一笔银行对应多条序时账（或反之） | 未匹配的anchor记录 | pool=24, max_size=6 |
| **4** | 同日同账号同方向批量匹配 | 同日分组后bank+ledger总额相等 | 仅限同日 |

### 记录加载 (`_records`)

`_records()` 从清洗后的 CSV 加载记录，对 `amount=0` 的记录有 fallback 逻辑：

```
if amount_cents ≤ 0:
    检查 ledger_debit / ledger_credit (bank侧检查 bank_debit / bank_credit)
    debit > 0  → amount = debit,  flow = in(ledger) / out(bank)
    credit > 0 → amount = credit, flow = out(ledger) / in(bank)
```

此 fallback 用于处理清洗脚本将负金额错误设为 0 的情况。若原始 debit 和 credit 均为 0 则无法恢复（属清洗层数据质量问题）。

### 子集搜索 (`_find_subset`)

使用 DFS 在候选池中搜索金额总和等于目标的子集。按金额降序排列候选以加速剪枝。

安全机制：`node_limit=2,000,000` — 超过此搜索节点数即停止，返回已找到的最优结果（或空）。防止 `max_size` 较大时组合爆炸（如 C(50,8) ≈ 5.4亿）。

## 输入内容
- 底稿模板
- 银行流水
- 序时账
## 输出内容
- 清洗后的序时账 `clean/ledger_entries.csv`
- 清洗后的银行流水 `clean/bank_transactions.csv`
- 月度总数匹配  `matches/monthly_flow_check.csv`
- 匹配结果 `matches/matches.csv`
- 未匹配银行流水  `matches/unmatched_bank.csv`
- 未匹配序时账 `matches/unmatched_ledger.csv`
- 需要人工审核的条目 `matches/manual_review_candidates.csv`
- 最终的底稿`working_paper/资金流水专项核查工作底稿-自动填报.xlsm`

## 后端接口测试命令

```powershell
python -m audit_workflow clean -c config.example.yml
python -m audit_workflow match -c config.example.yml
python -m audit_workflow fill -c config.example.yml
```

## 格式扩展

新增稳定银行格式时，在 `audit_workflow/cleaners.py` 新增解析器函数，并在配置中指定 `parser`。

银行格式不稳定时，将配置中的银行流水 `parser` 设为 `llm_bank`。首次运行会根据 Excel 样例行生成 `generated_parsers/{输入id}.py`，之后同类格式会直接复用该脚本。
