# Codex 悬浮监测窗

Windows 桌面置顶小窗，Python 3.10+，仅用标准库，无需安装依赖。

## 下载与历史版本

[下载 Windows 最新版](https://github.com/Yao-JJ/Codex-Plugin/releases/latest/download/CodexMonitor.exe) · [历史版本下载](https://github.com/Yao-JJ/Codex-Plugin/releases) · [v1.0.1 更新说明](RELEASE_NOTES.md)

本仓库为私有仓库，访问源码、发行版和下载附件需要登录具有仓库访问权限的 GitHub 账号。

也可从 GitHub 仓库首页右侧的 **Releases** 进入版本列表，在对应版本的 **Assets** 中下载 `CodexMonitor.exe`。下载旧版本时，选择相应的版本号即可。本地打包结果位于 `dist\CodexMonitor.exe`。

## 使用说明

屏幕分辨率、缩放或主屏幕工作区变化时，每半秒检查并自动重新贴边，将猫爪留在可见范围内，保留左右侧偏好并按比例调整垂直位置。意外缩小或隐藏时会在 Codex 仍运行的情况下恢复显示；Codex 退出导致的主动隐藏保持有效。完整面板无法适应很小的工作区时会自动退回猫爪。当前贴边目标为主屏幕工作区。

跟随模式连续三次确认未检测到 Codex 才隐藏，无法确认进程状态时保留当前显示状态。界面刷新遇到异常后会继续尝试刷新和恢复显示，每 5 秒重新设置置顶。运行状态、隐藏/恢复原因和异常位置写入程序同目录的 `monitor-runtime.log`（每份最大 512 KiB，保留两份备份）；每分钟记录一次可见状态。Python 可捕获的原生崩溃栈写入 `monitor-fault.log`。诊断日志不记录聊天正文和异常消息；窗口位置、进程号和代码路径可能出现在日志中。

EXE 版：直接双击 `dist\CodexMonitor.exe`，无需另装 Python，可单独复制 EXE 到其他文件夹使用。设置保存在 EXE 所在文件夹的 `monitor-settings.json`，建议放在自己可写的普通文件夹。单文件版启动时会临时解包运行库。

双击 **启动悬浮窗.vbs**。也可运行 `python codex_monitor.py`。默认是贴屏幕右侧的 **72×64 简笔猫爪挂签**，灰色细线轮廓搭配深色底，掌心显示带状态颜色的无活动秒数；超过 9999 秒显示 `9999+s`，精确时长见完整面板。点击挂签向屏幕内展开 **320 像素宽**的完整面板，顶部 `−` 或 Esc 收回；退出按钮位于完整面板底部。展开面板连续 60 秒失去焦点后自动退回挂签，重新获得焦点会清零计时；设置对话框和聊天下拉框也算面板内部焦点。拖动挂签后松手，吸附到主屏幕工作区最近的左右边缘，可调整垂直位置；侧边和位置会在退出时保存。完整面板的文字自动换行，高度随内容适配。下拉框默认跟随最近有活动的聊天，可固定监测某个聊天。阈值按钮可调整无活动提醒时间。

- 绿色：任务中且近期有活动。
- 黄色：默认 60 秒没有新活动。
- 红色：默认 180 秒没有新活动，或无活动同时出现连接错误/HTTPS 探测失败。
- 灰色：任务完成、已中止或 Codex 进程未运行。

即使没有确认任何网络故障，无活动时长也会持续显示。工具运行期间同样计时，长计算、模型思考、等待审批或用户输入都可能触发提醒。完成的任务显示正常空闲；没有明确任务边界的旧记录显示状态待确认。

## 状态来源与限制

只读 `%USERPROFILE%\.codex`（遵循 `CODEX_HOME`，或用 `--home 路径` 指定）里的会话 JSONL、`state_*.sqlite` 和 `logs_*.sqlite`。SQLite 以只读模式连接，保留 WAL 可见性；不读取认证文件，不修改 Codex，不发送聊天内容。根据任务开始/结束、模型输出和工具调用/返回更新时间；usage 等后台记账不算活动。首次只读超大会话最后 4 MiB，因此缺失的任务边界可能无法判断。默认枚举最近更新的 100 个未归档聊天；自动模式按最后活动选择，不等于当前前台聊天。需要监测特定聊天时请在下拉框固定选择。

会话和进程每 2 秒刷新，索引和近期连接错误每 10 秒刷新。只有关联到该聊天且在最近 5 分钟内的连接错误才影响其状态；无聊天归属的日志不用于推断故障。旧的未结束任务记录不代表任务仍真实运行。

启用网络探测时，每轮完成后间隔 10 秒向 `https://chatgpt.com/backend-api/codex` 和 `https://www.microsoft.com` 发出无认证 HTTPS GET 请求（单次超时 5 秒），使用 Python/Windows 系统可识别的代理配置，保持 TLS 校验。401/403 等状态只表示 HTTPS 可达，不能证明模型服务正常。探测路径与 Codex 内部连接、VPN 分流可能不同，探测失败只表示疑似网络问题，不能确认 VPN 是原因。“代理已配置”不表示 VPN 一定运行；TUN 模式也未必表现为系统代理。

程序保存自己的窗口位置和阈值到同目录 `monitor-settings.json`。

## 随 Codex 自动显示

运行 `powershell -File .\install-autostart.ps1`：为当前 Windows 用户创建启动目录快捷方式，并立即启动 `CodexMonitor.exe --follow-codex`。登录 Windows 后在后台待命，每 2 秒检查桌面应用；Codex 启动时显示悬浮窗，退出时隐藏，下次启动重新显示。隐藏期间暂停会话读取和新的网络探测；已发出的探测可能仍会完成。防重复启动适用于同一 Windows 登录会话中的新版程序。

检测当前桌面应用的 `ChatGPT.exe` 进程及 `OpenAI.Codex` 安装路径，不把独立 CLI 或 VS Code 扩展当作桌面应用。此方式跟随进程，Codex 若保留后台进程，关闭主窗口后监测窗也可能继续显示。点击监测窗的退出按钮会结束监测，下次 Windows 登录时恢复，也可手动带 `--follow-codex` 重启。

保持 `dist\CodexMonitor.exe` 路径不变，移动程序后重新运行安装脚本。取消自动启动：`powershell -File .\install-autostart.ps1 -Uninstall`，不影响 Codex 自身或当前已运行的监测窗。

## 检查

```powershell
python -m unittest discover -s tests -v
python codex_monitor.py --once
python codex_monitor.py --once --probe
python codex_monitor.py --self-test
```

`--self-test` 打开单独标注的模拟窗口，约 17 秒内依次展示运行、无活动提醒、长时间无活动、模拟连接异常、恢复和任务完成，并检查收起/展开和布局。测试结束自动关闭，结果保存到同目录 `self-test-report.json`；不读取或改写真实会话、不探测网络，也不保存窗口设置。

界面不显示原始错误正文、代理地址或密钥。诊断输出包含聊天 ID 和工具名，分享前按需检查。Codex 的本地存储属于内部格式；未来更新可能需要调整读取逻辑，读取失败会显示在窗口内。

## 构建 EXE

在装有 Python、Tk 和 PyInstaller 的 Windows 上运行 `powershell -File .\build-exe.ps1`。生成无控制台的单文件 `dist\CodexMonitor.exe`，包含 Python、Tk、SQLite 和必要运行库，不包含本机 Codex 配置、认证文件或会话。

EXE 支持 `--self-test`，结果写入 EXE 同目录 `self-test-report.json`；也可用 `--once --output diagnostic-report.json` 导出实时状态。无控制台环境下 `--once` 未指定输出路径时默认保存到 EXE 同目录 `diagnostic-report.json`。

## 项目结构与版本发布

Git 仓库根目录为 `Codex-Plugin`，管理源码、测试、说明和构建脚本：

```text
Codex-Plugin/
├─ .gitignore
├─ .gitattributes
├─ README.md
├─ codex_monitor.py
├─ clash_failover.py
├─ build-exe.ps1
├─ install-autostart.ps1
├─ 启动悬浮窗.vbs
├─ tests/
├─ build/                  # 本地构建中间文件，Git 忽略
└─ dist/                   # 本地打包结果，Git 忽略
   └─ CodexMonitor.exe
```

`dist/` 和 `build/` 不提交到源码仓库。本地设置、运行报告、日志和 Python 缓存同样由 `.gitignore` 排除。

发布新版本时：

1. 运行测试，执行 `powershell -File .\build-exe.ps1`，并检查生成的程序。
2. 提交并推送本次版本的源码。
3. 在 GitHub 的 **Releases → Draft a new release** 中，为对应源码提交创建版本标签，例如 `v1.0.0`。
4. 填写更新说明，将 `dist\CodexMonitor.exe` 添加为附件后发布。
5. 后续版本使用新的标签和 Release，保留旧版本及其附件；附件统一命名为 `CodexMonitor.exe`，供最新版下载链接使用。

## Clash Verge 自动换节点

完整面板中“连接连续失败时自动换日本节点”默认开启，可随时取消。需要同时满足：有明确开始且未结束的任务、所有进行中任务至少达到无活动提醒阈值（默认 60 秒）没有新活动、连续三轮 Codex 连接探测失败。每轮完成后间隔 10 秒，探测本身也需要时间，因此不会一到 60 秒就立刻切换。新活动、任务结束、成功/401/403/429 等仅可达结果、探测中断都会重置连续失败次数；证书校验配置错误不作为换节点依据。缺少明确开始记录或开始超过 24 小时的旧任务不会单独触发自动切换。

程序从 Clash Verge Rev 本地配置读取控制地址和密钥，只连接回环控制接口，不保存或显示密钥。自动换节点开启时，探测明确经过 Clash 的 mixed-port/HTTP 代理端口。仅支持规则模式下，`chatgpt.com` 显式域名规则指向的专用 AI Selector 组；不会自动改共享的通用代理组、GLOBAL、订阅或 Clash 配置文件。本机已验证该组为“🤖AI网站”。此组下的其他 AI 服务也会受到组内节点变化影响。

只从该 AI 组直接包含的日本实际节点中选候选（按日本国旗、日本/东京/大阪等地区名或 JP/Japan 标记识别），不使用其他地区或自动分组；无可用日本节点时保持原选择。每次最多检测 8 个其他日本实际节点，找到 HTTPS 可达的候选后，再检查任务状态和用户手动选择是否变化，才选择新节点。切换后重新探测，若仍明确失败则尝试回退；若用户已手动选了其他节点则保留用户选择。不主动断开已有连接，所以 Codex 仍依赖自身重连机制。候选连通性不能保证账号、地区或模型服务一定可用。

两次尝试至少间隔 5 分钟，每小时最多 3 次；取消和失败尝试也占用冷却。冷却记录保存在 EXE 同目录 `failover-state.json`，重启不会清零。操作结果保存在 `node-switch-log.jsonl`，包含组名、节点名、时间及结果，不包含密钥。面板会显示连续失败计数和操作结果。找不到专用组、控制接口不可用、无备用节点或任务恢复时保持原选择，并显示原因。

实现依据：[Mihomo 官方控制 API](https://wiki.metacubex.one/en/api/)。

猫爪肉垫表示所有已读取聊天中的进行中任务数：第 1 项点亮掌心的小粉色肉垫，第 2～5 项从左到右点亮四个脚趾肉垫，任务减少时逆序熄灭；超过 5 项保持全亮，精确数量在完整面板显示。肉垫使用低饱和粉色，秒数仍单独显示。任务数依据本地开始/结束记录，缺失结束事件的旧记录可能仍计为进行中。
