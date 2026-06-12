# Audit Workflow: 资金流水专项核查

这套工作流用于把银行流水和新纪元序时账清洗成标准 CSV，自动匹配银行流水与账面记录，并把结果填入资金流水专项核查底稿。

## 设计原则

- 银行流水格式不稳定：优先使用已知固定解析器；未知格式可让 LLM 生成 Python 解析脚本，并缓存到 `generated_parsers/` 复用。
- 新纪元序时账格式稳定：使用固定解析器 `xinjiyuan_bank_ledger`，不调用 LLM。
- 匹配结果保留置信度、未匹配清单和低置信信息，方便审计人员复核。
- 原始 Excel 不改动，输出统一写入 `outputs/`。

## 快速运行

```powershell
python -m audit_workflow.bank_ledger_match run -c config.example.yml
```

运行后会生成：

- `outputs/clean/bank_transactions.csv`
- `outputs/clean/ledger_entries.csv`
- `outputs/matches/matches.csv`
- `outputs/matches/unmatched_bank.csv`
- `outputs/matches/unmatched_ledger.csv`
- `outputs/working_paper/资金流水专项核查工作底稿-自动填报.xlsm`

## LLM / PydanticAI 配置

LLM 交互统一通过 PydanticAI agent 发起，支持 OpenAI 兼容接口。不要把 API key 写进聊天记录或提交到代码库。复制 `.env.example` 为 `.env`，按实际服务填入：

```text
OPENAI_API_KEY=你的key
OPENAI_MODEL=你要使用的模型名
OPENAI_BASE_URL=兼容接口地址
```

也可以在 `config.example.yml` 的 `matching.llm` 中使用 `api_key_env`、`base_url_env`、`model_env` 指向服务商环境变量。然后把需要的 `llm.enabled` 或 `matching.llm.enabled` 改为 `true` 即可。

## 常用命令

```powershell
python -m audit_workflow clean -c config.example.yml
python -m audit_workflow match -c config.example.yml
python -m audit_workflow fill -c config.example.yml
```

## 格式扩展

新增稳定银行格式时，在 `audit_workflow/cleaners.py` 新增解析器函数，并在配置中指定 `parser`。

银行格式不稳定时，将配置中的银行流水 `parser` 设为 `llm_bank`。首次运行会根据 Excel 样例行生成 `generated_parsers/{输入id}.py`，之后同类格式会直接复用该脚本。

## 匹配原则

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

    E --> F1[规则匹配: 一对一]
    F1 --> F2[规则匹配: 一对多 / 多对一]
    F2 --> F3[规则匹配: 同日多对多]
    F3 --> G[得到初步 matches / unmatched]

    G --> H{仍有 unmatched?}
    H -->|否| Z[如配置允许, 写入工作底稿]
    H -->|是| I[生成补充候选]

    I --> I1[金额/日期/摘要候选]
    I --> I2[连续时间窗口枚举]
    I --> I3[多银行流水合计 = 一条序时账]
    I --> I4[多银行流水窗口 = 多序时账窗口]
    I --> I5[月度平衡枚举: 月度残余总额相等时凑子集]

    I1 --> J1[LLM 判断是否可自动接受]
    I5 --> J2[LLM 写匹配失败原因和人工复核提示]

    I2 --> K[人工复核候选]
    I3 --> K
    I4 --> K
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
    class C2,J1,J2 llm;
```
