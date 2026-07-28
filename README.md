# 量化

公开的量化研究仪表盘。行情数据来自腾讯财经公开行情接口，由 GitHub Actions
在交易日每 15 分钟更新一次。网页中的“规则评分”仅使用涨跌幅、量比、换手率、
PE 和 PB 等公开字段计算，不是 AI 预测，也不构成投资建议。

数据更新脚本：`scripts/update_market.py`

自动更新流程：`.github/workflows/update-market.yml`
