# 出库表-平台结算流水匹配

## 输入文件
1. sellout_refund_return_report 月度收入报告。Recap汇总、Sellout-出库、refund only（仅退款/资损）、return、transfer退回保税仓上架
2. 平台结算流水
## 工作流设计
1. 平台结算流水清洗：常常格式不同，有不同的层级结构
- LLM读取代表性文件的前几行，清洗表格生成csv，所有字段统一为英文
- 多个csv汇总成年度总数据，并给出月度分析
2. sellout_refund_return_report：过滤掉refund/return/transfer后的净出库，检查每一笔是否与平台结算流水对应。理论上同一个平台的工作表结构是一致的。
- LLM读取不同的工作表名称，确认不同类型的表格是sellout/refund/return/transfer
- LLM生成清洗脚本得到csv，所有字段统一为英文
- 脚本对sellout表格做过滤（订单编号作为unique_id）过滤掉refund/return/transfer后的净出库
- 将净出库与处理好的平台结算流水用id进行匹配
- 生成未匹配的总额