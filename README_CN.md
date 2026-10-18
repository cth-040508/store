# 全球市场微信快讯

脚本定时读取 CNBC Finance、CoinDesk、美联储和 SEC 的公开 RSS，将尚未推送的标题、来源、时间和原文链接汇总后推送到微信。首次运行后会生成本地 SQLite 去重记录；RSS 标题不等于已核实事实，重要信息请打开原文。

## 重点筛选

默认只推送标题符合重大事件规则的新闻：主要央行政策结果、美国关键经济数据、重点大公司的业绩和重大经营事件、加密监管决定与安全风险、重大贸易地缘事件和金融系统风险。过滤预测、一般评论、推广以及未命中规则的文章，不再按时间凑满条数。每次默认最多 5 条，按事件类别的重要程度排序，推送中附入选原因。若已有 `market_monitor.json`，其中的 `max_items` 会覆盖默认值，建议改为 5。

相同链接和高度相似的标题会去重；标题去重记录保留 36 小时。改写幅度较大的同一事件仍可能重复，新进展标题高度相似也可能被跳过。已有 URL 去重记录保持不变，新标题去重从升级后首次推送开始积累。

筛选完全在本地执行，无模型 token 费用。规则仅分析英文标题，按事件类型初筛，不是实际市场影响预测；可能漏掉重要消息，也可能保留影响有限的事件，且不会扩大现有 RSS 来源的覆盖范围。

运行 `python -m unittest test_market_monitor.py` 可检查筛选和去重规则。

## 配置推送

1. 安装 Python 3.10 或更新版本，安装时勾选添加到 PATH。
2. 在 Server酱（sct.ftqq.com）登录并绑定微信，创建 SendKey。Server酱可能调整免费额度或服务规则，请以其当前页面为准。
3. 在 Windows PowerShell 中为当前账户设置密钥：

```powershell
[Environment]::SetEnvironmentVariable('SERVERCHAN_SENDKEY', '你的SendKey', 'User')
```

设置完成后，关闭并重新打开 PowerShell。SendKey 是私密凭据，不要写进脚本或提交到 Git。

## 测试

在本目录打开 PowerShell，运行：

```powershell
python .\market_monitor.py --test
python .\market_monitor.py
```

第一条应向微信发送测试消息。第二条读取各 RSS 并发送新消息。第一次运行可能推送各源当前保留的最近文章，之后只推送尚未发送的内容。

## 每天定时运行

下面示例每天运行四次，时间为本机 08:00、12:00、16:00、20:00。PowerShell 使用任务计划程序创建当前 Windows 账户的定时任务：

```powershell
$scriptPath = (Resolve-Path '.\market_monitor.py').Path
$pythonPath = (Get-Command python).Source
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument "`"$scriptPath`""
$triggers = @(
  (New-ScheduledTaskTrigger -Daily -At '08:00'),
  (New-ScheduledTaskTrigger -Daily -At '12:00'),
  (New-ScheduledTaskTrigger -Daily -At '16:00'),
  (New-ScheduledTaskTrigger -Daily -At '20:00')
)
Register-ScheduledTask -TaskName 'GlobalMarketWeChatMonitor' -Action $action -Trigger $triggers -Description '定时读取全球市场 RSS 并推送微信' -Force
```

任务执行时电脑需要开机并能联网。若环境变量未被任务计划读取，请在任务属性的“操作”中使用包装脚本，或改为在启动任务的 PowerShell 会话中设置变量。任务可在“任务计划程序”中查看运行结果和历史记录。

停止任务：

```powershell
Unregister-ScheduledTask -TaskName 'GlobalMarketWeChatMonitor' -Confirm:$false
```

## 调整来源

复制 `market_monitor.example.json` 为 `market_monitor.json`，修改 `feeds`、`max_items` 或 `lookback_hours`。本地配置文件不会覆盖示例文件。RSS 源的可用性和内容可能变化；来源读取失败时，程序会继续处理其他来源并在运行输出中报告。

注意：此方案是定时 RSS 摘要，不是交易级实时行情，也不能保证所有重大新闻都被捕获。若电脑经常关机，可把脚本放在始终在线的主机或云服务器上运行。
