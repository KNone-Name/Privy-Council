# V3 AI 可发布版

版本：2026-11-20  
执行桥：v3bridge 0.3 / build 158 / Victoria 3 1.15.1

## 运行要求

- Windows 10/11
- Python 3.10+（项目只用标准库）
- Victoria 3 1.15.1，已启动并进入存档
- 本地或云端 OpenAI 兼容接口（默认 `http://127.0.0.1:18200/v1`）

## 启动

双击根目录的 `启动项目.bat`。

它会请求管理员权限，确认游戏已进入存档，启动宿主 web、注入 `bridge/v3inject.dll`、
启动 watch，并打开 `http://127.0.0.1:8765`。

等页面出现 `扫描 100% / 类型表已就绪` 后，再按需要打开“连续”。

## 目录

```text
v3.py                 统一入口
v3ai/                 AI 决策、审查、网页、宿主编排
v3calc/               数据/计算层（含 advisor）
v3live/               内存读取层
tests/                离线回归测试（52 项）
examples/             示例快照（离线可算）
bridge/               v3bridge 0.3 DLL、injector、源码、文档
  v3inject.dll
  injector.exe
  docs/
  src/
启动项目.bat
```

## 配置

`v3ai/config.json` 使用相对路径：

```json
"service": {
  "injector": "bridge/injector.exe",
  "dll": "bridge/v3inject.dll"
},
"executor": {
  "bridge": { "dir": "bridge" }
}
```

API Key 已清空。接入云端模型时在网页设置里填写，Key 只保存在本机配置中。
连续模式默认关闭。

## 数据端

本发布包只含运行时；`Victoria3-RE` 逆向资料、`v3panel` 实验面板、备份和浏览器缓存
没有打进来。`examples/` 附一份已落盘的示例快照，游戏没开也能直接跑计算；
`tests/` 附离线回归测试（`python -m unittest discover -s tests`）。运行时会从游戏安装目录读取规则/本地化，并使用
`v3ai/data/` 下的映射。

## 注意

- 如果游戏里加载过旧版 DLL，先完全重启游戏，再启动本包；
- 不要和旧 `v3inject_v109.dll`/旧桥接 DLL 同时注入；
- `unload.flag` 是一次性的；DLL 卸载超时会回滚，不会强行拆代码段。
