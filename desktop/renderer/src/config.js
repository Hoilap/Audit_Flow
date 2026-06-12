export const navItems = [
  ['dashboard', '⌂', 'Dashboard'],
  ['projects', '□', '项目管理'],
  ['data', '▦', '数据源'],
  ['agent', '◌', 'Agent 工作流'],
  ['programs', '✓', '审计程序'],
  ['workpapers', '▤', '工作底稿'],
  ['reports', '↗', '分析报告'],
  ['settings', '⚙', '设置'],
]

export const workflowTasks = [
  {
    id: 'bank-ledger-match',
    name: '序时账银行流水匹配',
    description: '清洗银行流水和序时账，执行匹配、校验并填入工作底稿。',
    risk: 'High',
    prompt: '生成一段 Python 代码，读取 outputs/bank_ledger_match/clean 和 matches 下的 CSV，检查银行流水与序时账未匹配项目，并输出审计说明。',
    steps: [
      {
        id: 'clean',
        label: 'Clean',
        title: '清洗原始数据',
        endpoint: '/workflow/clean',
        outputs: ['outputs/bank_ledger_match/clean/bank_transactions.csv', 'outputs/bank_ledger_match/clean/ledger_entries.csv'],
      },
      {
        id: 'match',
        label: 'Match',
        title: '执行流水匹配',
        endpoint: '/workflow/match',
        outputs: ['outputs/bank_ledger_match/matches/matches.csv', 'outputs/bank_ledger_match/matches/unmatched_bank.csv', 'outputs/bank_ledger_match/matches/unmatched_ledger.csv'],
      },
      {
        id: 'verify',
        label: 'Verify',
        title: '校验匹配结果',
        endpoint: '/workflow/verify',
        outputs: ['outputs/bank_ledger_match/matches/matches.csv', 'outputs/bank_ledger_match/matches/unmatched_bank.csv', 'outputs/bank_ledger_match/matches/unmatched_ledger.csv'],
      },
      {
        id: 'fill',
        label: 'Fill',
        title: '填入工作底稿',
        endpoint: '/workflow/fill',
        outputs: ['outputs/bank_ledger_match/working_paper/资金流水专项核查工作底稿-自动填报.xlsm'],
      },
    ],
  },
  {
    id: 'outbound-check',
    name: '出库表核对',
    description: '核对出库明细、销售订单和开票记录的一致性。',
    risk: 'Medium',
    prompt: '生成一段 Python 代码，核对出库表、销售订单和发票清单，识别数量、金额和客户名称不一致记录。',
    steps: [
      { id: 'profile', label: 'Profile', title: '识别字段结构', endpoint: null, outputs: ['outputs/llm_code/generated_from_llm.py'] },
      { id: 'reconcile', label: 'Reconcile', title: '执行三表核对', endpoint: null, outputs: ['outputs/llm_code/generated_from_llm.py'] },
      { id: 'verify', label: 'Verify', title: '校验差异清单', endpoint: '/workflow/verify', outputs: ['outputs/matches/matches.csv'] },
      { id: 'report', label: 'Report', title: '生成核对结论', endpoint: null, outputs: ['outputs/llm_code/generated_from_llm.py'] },
    ],
  },
  {
    id: 'cash-flow-review',
    name: '资金流水专项复核',
    description: '围绕大额、频繁、异常对手方和摘要进行资金流水风险识别。',
    risk: 'High',
    prompt: '生成一段 Python 代码，分析银行流水 CSV 中的大额交易、同日多笔交易和异常对手方，并输出风险清单。',
    steps: [
      { id: 'profile', label: 'Profile', title: '生成数据画像', endpoint: null, outputs: ['outputs/llm_code/generated_from_llm.py'] },
      { id: 'detect', label: 'Detect', title: '识别异常流水', endpoint: null, outputs: ['outputs/llm_code/generated_from_llm.py'] },
      { id: 'verify', label: 'Verify', title: '复核风险规则', endpoint: '/workflow/verify', outputs: ['outputs/matches/unmatched_bank.csv'] },
      { id: 'paper', label: 'Paper', title: '生成审计说明', endpoint: null, outputs: ['outputs/llm_code/generated_from_llm.py'] },
    ],
  },
]

export const llmCodePath = 'outputs/llm_code/generated_from_llm.py'
