# 13F 季度更新

从 SEC EDGAR 抓取监控名单的本季与上季持仓，在仓库根目录生成 `report-YYYYQn.html`。脚本按报告期处理修订。抓取失败时保留上一次报告。

## 手动更新

安装 Python 3.10 或更高版本，然后在仓库目录运行：

```bash
python3 -m pip install -r requirements.txt
python3 update_13f.py
```

首次可按提示输入用于 SEC 请求的姓名和联系邮箱，也可预先设置 `SEC_USER_AGENT`。指定历史季度时可运行 `python3 update_13f.py --quarter 2026Q3`。

`--scheduled` 只在美东日期距上季结束 40–45 天时更新。遇到 SEC 截止日顺延，可在截止日后手动再跑一次。
