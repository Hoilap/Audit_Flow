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
    description: 'Detect扫描→LLM识别→确认配置→清洗→核查→匹配→填入底稿。',
    risk: 'High',
    dirName: 'bank_ledger_match',
    prompt: '请在数据源页面将银行流水和序时账文件上传到 inputs/{客户名}/bank_ledger_match/ 目录下，然后从 Detect 步骤开始执行。',
    reviewFiles: [
      'matches/manual_review_candidates.csv',
    ],
    steps: [
      {
        id: 'detect',
        label: 'Detect',
        title: '扫描文件并LLM识别',
        description: '扫描 inputs/ 下的 Excel 文件，自动识别银行流水/序时账、银行名称、时间段',
        endpoint: '/workflow/detect',
        outputs: ['task.yml'],
        formFields: [
          { name: 'customer_name', type: 'customer_name', label: '客户名称', required: true },
          { name: 'task_name', type: 'task_name', label: '任务名称', required: true },
          { name: 'use_llm', type: 'checkbox', label: '使用LLM智能识别', default: true },
        ],
        prompt: '请在数据源页面上传银行流水和序时账文件后，点击运行此步骤。系统将自动识别文件类型。',
      },
      {
        id: 'confirm',
        label: 'Confirm',
        title: '确认配置 YAML',
        description: '检查 LLM 生成的 task.yml 配置，修改并确认后保存',
        endpoint: '/workflow/config/save',
        outputs: ['task.yml'],
        isConfigStep: true,
        prompt: '请检查以下自动生成的配置，确认银行流水和序时账的文件路径、解析器、银行名称是否正确。修改后点击「保存配置」继续。',
      },
      {
        id: 'clean',
        label: 'Clean',
        title: '清洗原始数据',
        description: '按 task.yml 配置解析银行流水和序时账，生成标准化 CSV',
        endpoint: '/workflow/bank_ledger_match/clean',
        outputs: ['clean/bank_transactions.csv', 'clean/ledger_entries.csv'],
      },
      {
        id: 'check',
        label: 'Check',
        title: '核查数据完备性',
        description: '检查 monthly_flow_check.csv，确认银行流水和序时账数据是否完备、月度流入流出是否一致',
        endpoint: '/workflow/bank_ledger_match/check',
        outputs: ['matches/monthly_flow_check.csv'],
        isCheckStep: true,
      },
      {
        id: 'match',
        label: 'Match',
        title: '执行流水匹配',
        description: '对清洗后的银行流水和序时账执行自动匹配',
        endpoint: '/workflow/bank_ledger_match/match',
        outputs: ['matches/matches.csv', 'matches/unmatched_bank.csv', 'matches/unmatched_ledger.csv'],
      },
      {
        id: 'fill',
        label: 'Fill',
        title: '填入工作底稿',
        description: '将匹配结果填入 Excel 工作底稿模板',
        endpoint: '/workflow/bank_ledger_match/fill',
        outputs: ['working_paper/资金流水专项核查工作底稿-自动填报.xlsm'],
      },
    ],
  },
  {
    id: 'outbound-check',
    name: '出库表核对',
    description: '核对出库明细、销售订单和开票记录的一致性。',
    risk: 'Medium',
    dirName: 'outbound_check',
    prompt: '生成一段 Python 代码，核对出库表、销售订单和发票清单，识别数量、金额和客户名称不一致记录。',
    reviewFiles: [
      'review/discrepancies.csv',
      'review/manual_review_candidates.csv',
    ],
    steps: [
      { id: 'profile', label: 'Profile', title: '识别字段结构', endpoint: null, outputs: ['llm_code/generated_from_llm.py'] },
      { id: 'reconcile', label: 'Reconcile', title: '执行三表核对', endpoint: null, outputs: ['llm_code/generated_from_llm.py'] },
      { id: 'verify', label: 'Verify', title: '校验差异清单', endpoint: '/workflow/verify', outputs: ['matches/matches.csv'] },
      { id: 'report', label: 'Report', title: '生成核对结论', endpoint: null, outputs: ['llm_code/generated_from_llm.py'] },
    ],
  },
  {
    id: 'cash-flow-review',
    name: '资金流水专项复核',
    description: '围绕大额、频繁、异常对手方和摘要进行资金流水风险识别。',
    risk: 'High',
    dirName: 'cash_flow_review',
    prompt: '生成一段 Python 代码，分析银行流水 CSV 中的大额交易、同日多笔交易和异常对手方，并输出风险清单。',
    reviewFiles: [
      'review/risk_flags.csv',
      'review/manual_review_candidates.csv',
    ],
    steps: [
      { id: 'profile', label: 'Profile', title: '生成数据画像', endpoint: null, outputs: ['llm_code/generated_from_llm.py'] },
      { id: 'detect', label: 'Detect', title: '识别异常流水', endpoint: null, outputs: ['llm_code/generated_from_llm.py'] },
      { id: 'verify', label: 'Verify', title: '复核风险规则', endpoint: '/workflow/verify', outputs: ['matches/unmatched_bank.csv'] },
      { id: 'paper', label: 'Paper', title: '生成审计说明', endpoint: null, outputs: ['llm_code/generated_from_llm.py'] },
    ],
  },
]

export const llmCodePath = 'outputs/llm_code/generated_from_llm.py'

/**
 * 右栏（证据面板）区块顺序配置。
 * 调整数组顺序即可自定义右栏显示顺序，删除某条即隐藏该区块。
 * 每条: [id, 标题, 是否有刷新按钮]
 *   - id 对应 HTML 容器 id（如 'timeline'）
 *   - 特殊 id: 'step-files' / 'all-files' / 'git-log' / 'review-editor'
 */
export const evidencePanelSections = [
  ['timeline',    '执行时间线',   true],
  ['review-editor', '人工复核',    true],
  ['step-files',  '本任务生成文件', true],
  ['all-files',   '所有输出文件',  false],
  ['git-log',     'Git 历史',     true],

]
