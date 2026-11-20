# V3AI 御前 AI 面板

维多利亚 3（1.15.x）游戏内 LLM 对话框：侧栏按钮打开面板，输入问题后经外部服务调用 OpenAI 兼容接口，回复显示在面板里，可带当前局势快照作为上下文。

## 目录

```
mod/v3ai_panel/         游戏 mod（GUI、脚本、本地化）
v3ai_panel.mod          启动器用的 mod 描述
bridge/
  v3panel_bridge37.dll  原生桥（读输入、写回复、按钮判定）
  injector.exe          注入器
  v3panel_config.ini    按钮判定框 / 流式参数
  run_bridge.ps1        注入桥并等待扫描完成
  reload_bridge.ps1     热重载（不重启游戏）
  unload_bridge.ps1     卸载当前桥
server/
  panel_server.py       外部服务（无 v3ai 依赖）
  test_client.py        命令行测试
  panel_server.json.example
README.md
```

## 前置

- Windows，Victoria 3 **1.15.x**
- Python 3.10+
- 一个 OpenAI 兼容的 `/v1/chat/completions` 端点（本地或远程）

## 1. 安装 mod

把 `mod/v3ai_panel` 整个目录复制到：

```
%USERPROFILE%\Documents\Paradox Interactive\Victoria 3\mod\v3ai_panel
```

（如果你的 Documents 在 D:，就是 `D:\Users\<用户名>\Documents\...`）

再把 `v3ai_panel.mod` 复制到：

```
...\Victoria 3\mod\v3ai_panel.mod
```

启动器 → 播放集 → 启用「V3AI 御前面板」。

## 2. 启动外部服务

```powershell
cd server
copy panel_server.json.example panel_server.json
notepad panel_server.json        # 填 base_url / model / api_key
python panel_server.py
```

窗口应显示：

```
V3AI panel service on 127.0.0.1:8766 backend=<model> @ <base_url>
```

命令行验证：

```powershell
python test_client.py --text "你好"
```

## 3. 注入桥

游戏运行并读档后，管理员 PowerShell：

```powershell
cd bridge
powershell -ExecutionPolicy Bypass -File .\run_bridge.ps1
```

脚本会注入 `v3panel_bridge37.dll`，等 `v3ai_input` / `v3ai_reply` 出现后退出。若 8766 没监听会直接报错，先启动第 2 步的服务。

## 使用

- 点侧栏「御前 AI」打开面板；
- 输入问题，按 Enter 或点「发送」；
- 回复逐段追加；「清空」或 F10 清空（F10 仅在面板打开时生效）；
- Esc 或右上角 X 关闭面板；
- 关闭面板后，点击原按钮位置或按 Enter 不会触发。

## 配置

`bridge/v3panel_config.ini`：

```ini
send_box=1540,1056,260,160
clear_box=1800,1080,160,100
chunk_chars=40
chunk_delay_ms=60
thinking=1
```

- `send_box` / `clear_box`：屏幕客户区像素框（x,y,w,h），按 2560×1600 标定；
- `chunk_chars` / `chunk_delay_ms`：流式追加的分段大小与间隔；
- `thinking=1`：发送后先显示「（思考中…）」。

面板窗口位置在 `mod/v3ai_panel/gui/v3ai_dialog.gui` 的 `position = { 981 145 }`，同样按 2560×1600 标定；换分辨率时同时调窗口位置和按钮框。

## 局势上下文（可选）

把 v3ai 生成的 `snap.json` 放在 `server/panel_server.py` 同目录，服务会把它压缩成局势上下文一起发给 LLM；快照超过 10 分钟会附加“已过期”提示。

没有 `snap.json` 也能用，只是 AI 只能给通用建议。

## 热重载 / 卸载

不需要重启游戏：

```powershell
# 卸载当前桥
powershell -ExecutionPolicy Bypass -File .\unload_bridge.ps1

# 卸载旧桥并注入新桥（换版本时）
powershell -ExecutionPolicy Bypass -File .\reload_bridge.ps1 -Old v3panel_bridge37.dll -New v3panel_bridge37.dll
```

注意：桥的 DLL 被加载时不能覆盖同名文件；新版本编译成新文件名后再 `reload_bridge.ps1`。

## 限制

- 桥按 1.15.1 的对象偏移实现，游戏更新后可能失效；
- 面板是固定位置窗口，按钮判定是屏幕像素框，换分辨率需重标；
- 回复超过 4000 字节会截断。
