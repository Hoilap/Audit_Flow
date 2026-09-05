export const navItems = [
  ['dashboard', '⌂', 'Dashboard'],
  ['projects', '□', '项目管理'],
  ['data', '▦', '数据源'],
  ['agent', '◌', 'Agent 工作流'],
  ['agent-loop', '⊛', 'Agent 对话'],
  ['programs', '✓', '审计程序'],
  ['settings', '⚙', '设置'],
  ['workpapers', '▤', '工作底稿'],
  ['reports', '↗', '分析报告'],
]

export const workflowTasks = [
  {
    id: 'bank-ledger-match',
    name: '序时账银行流水匹配',
    description: 'Detect扫描→LLM识别→确认配置→清洗→核查→匹配→人工复核→填入底稿。',
    //risk: 'High',
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
          { name: 'parser', type: 'select', label: '识别方式', options: [
            { value: 'llm', label: 'LLM 智能识别' },
            { value: 'script', label: '脚本（关键词）识别' },
          ]},
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
        id: 'clean-bank',
        label: 'Clean Bank',
        title: '清洗银行流水',
        description: '按选定的解析器清洗银行流水，生成标准化 CSV',
        endpoint: '/workflow/bank_ledger_match/clean_bank',
        outputs: ['clean/bank_transactions.csv'],
        formFields: [
          { name: 'parser', type: 'select', label: '解析器', options: [
            { value: 'llm', label: 'LLM 智能解析' },
            { value: 'llm_regenerate', label: 'LLM 重新生成' },
            { value: 'icbc_historydetail', label: 'ICBC historydetail' },
            { value: 'generated_bank', label: '预生成解析器' },
          ]},
        ],
      },
      {
        id: 'clean-ledger',
        label: 'Clean Ledger',
        title: '清洗序时账',
        description: '按选定的解析器清洗序时账，生成标准化 CSV',
        endpoint: '/workflow/bank_ledger_match/clean_ledger',
        outputs: ['clean/ledger_entries.csv'],
        formFields: [
          { name: 'parser', type: 'select', label: '解析器', options: [
            { value: 'llm', label: 'LLM 智能解析' },
            { value: 'llm_regenerate', label: 'LLM 重新生成' },
            { value: 'xinjiyuan_bank_ledger', label: '新纪元银行账' },
          ]},
        ],
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
        id: 'verify',
        label: 'Verify',
        title: '应用人工复核',
        description: '将人工复核通过的项目加入 matches.csv，从 unmatched 中移除',
        endpoint: '/workflow/bank_ledger_match/verify',
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
    id: 'outbound-settlement-match',
    name: '出库结算匹配',
    description: 'Detect扫描→清洗结算流水→清洗出库表→匹配出库与结算→一键全跑。',
    dirName: 'outbound_settlement_match',
    prompt: '请在数据源页面将平台结算流水和出库表文件上传到 inputs/{客户名}/outbound_settlement_match/ 目录下，然后从 Detect 步骤开始执行。',
    reviewFiles: [],
    steps: [
      {
        id: 'osm-detect',
        label: 'Detect',
        title: '扫描并识别文件',
        description: '扫描 inputs/ 下的文件，自动分类结算流水文件和出库表',
        endpoint: '/workflow/outbound_settlement_match/detect',
        outputs: [],
      },
      {
        id: 'clean-settlement',
        label: 'Clean Settlement',
        title: '清洗结算流水',
        description: '汇总各平台结算 CSV，生成合并结算流水和月度汇总',
        endpoint: '/workflow/outbound_settlement_match/clean_settlement',
        outputs: ['clean/settlement.csv', 'clean/monthly_summary.csv'],
      },
      {
        id: 'clean-outbound',
        label: 'Clean Outbound',
        title: '清洗出库表',
        description: '解析出库表 Excel 各 Sheet，生成标准化 CSV',
        endpoint: '/workflow/outbound_settlement_match/clean_outbound',
        outputs: ['clean/outbound_all.csv'],
      },
      {
        id: 'match',
        label: 'Match',
        title: '执行出库-结算匹配',
        description: '过滤净额出库并按订单号匹配结算流水',
        endpoint: '/workflow/outbound_settlement_match/match',
        outputs: ['matches/matched.csv', 'matches/unmatched_outbound.csv', 'matches/unmatched_settlement.csv', 'matches/net_outbound.csv', 'matches/monthly_summary.csv'],
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

/**
 * 自定义任务通用工作流模板。
 * 当用户创建的任务名称不在预定义 workflowTasks 中时，使用此模板替代，
 * 避免显示特定工作流（如 bank-ledger-match）的步骤。
 */
export const customWorkflowTask = {
  id: '__custom__',
  name: '自定义任务',
  description: '通过 LLM 代码生成完成自定义审计任务。请在下方输入自然语言指令，生成并执行处理代码。',
  risk: 'Medium',
  dirName: null,
  prompt: '请描述您的审计任务需求，并说明输入数据路径（如 inputs/{客户名}/ 下的文件）和期望的输出结果路径（如 outputs/{客户名}/ 下的文件）。',
  reviewFiles: [],
  steps: [
    { id: 'understand', label: 'Understand', title: '理解任务需求', endpoint: null, outputs: ['llm_code/generated_from_llm.py'] },
    { id: 'profile', label: 'Profile', title: '分析数据结构', endpoint: null, outputs: ['llm_code/generated_from_llm.py'] },
    { id: 'code', label: 'Code', title: '生成处理代码', endpoint: null, outputs: ['llm_code/generated_from_llm.py'] },
    { id: 'run', label: 'Run', title: '执行代码', endpoint: null, outputs: ['llm_code/generated_from_llm.py'] },
    { id: 'validate', label: 'Validate', title: '校验结果', endpoint: null, outputs: ['llm_code/generated_from_llm.py'] },
    { id: 'export', label: 'Export', title: '输出工作底稿', endpoint: null, outputs: ['llm_code/generated_from_llm.py'] },
  ],
}

/**
 * 右栏（证据面板）区块顺序配置。
 * 调整数组顺序即可自定义右栏显示顺序。
 * 每条: [id, 标题, 是否可见, 是否有刷新按钮, 是否可折叠]
 *   - id 对应 HTML 容器 id（如 'timeline'）
 *   - visible 为 false 时整个区块不渲染
 *   - collapsible 为 true 时标题栏显示折叠/展开按钮，默认收起
 *   - 特殊 id: 'project-file-tree' / 'git-log' / 'review-editor'
 */
export const evidencePanelSections = [
  ['review-editor',     '人工复核',      true,  true,  false],
  ['config-editor',     '配置确认',      true,  false, true],
  ['project-file-tree', '项目文件',      true,  true,  false],
  ['git-log',           'Git 历史',      true,  true,  true],
]
