# v3bridge 0.3（精简包）

Victoria 3 的 DLL 注入式外部控制层。目标版本 **1.15.1**（internal 4.5.2 / 3270）。
不改游戏文件、不装 mod。完整说明见 `交接文档.md`。

```
bin\v3inject.dll     主 DLL
bin\injector.exe     注入器
交接文档.md           完整说明（能力、协议、架构、偏移表、排错）
```

## 快速开始

```powershell
# 1. 启动游戏，进入存档
# 2. 注入（DLL 路径必须是绝对路径）
$dll = (Resolve-Path .\bin\v3inject.dll).Path
.\bin\injector.exe victoria3 $dll

# 3. 等就绪：日志出现 capabilities: ... on，同时响两声
Get-Content v3inject.log | Select-String "capabilities"
```

就绪后同目录出现 `world.current.txt`、`player.current.txt`。

## 命令

写一个 txt → DLL 处理完把它改名成 `.done.txt` → 读结果文件。

| 输入 | 内容 | 输出 |
|---|---|---|
| `requests.txt` | `<州id> <建筑键> <等级>` | 日志（成功返回队列增量） |
| `query.txt` | `building <建筑键> [all]` | `states.csv` |
| `pms.txt` | `<州id> <建筑键>` | `pms.csv` |
| `pmm.txt` | `<州id> <建筑键> <pm键>` | `pmm.result.csv` |
| `queue.txt` | 任意 | `queue.csv` |
| `unload.flag` | 空文件 | 卸载 DLL |

例：

```powershell
"18 building_university 1" | Set-Content requests.txt -Encoding ASCII
Get-Content v3inject.log | Select-String "req:"
```

## 注意

- **煤矿等资源类建筑只能建在有该资源的州**，否则引擎会报错（DLL 会接住，但该类型本会话不再尝试）
- 写操作只允许作用于**玩家自己的国家**，这是硬性限制
- `unload.flag` 是一次性的，DLL 读到即删除；注入前不要留着
- 游戏更新后直接注入即可：指纹不匹配时会自动改用签名定位，日志会显示 `build UNKNOWN ... resolving by signature`

## 指纹（1.15.1）

```
size = 100221048
hash = 0xF8A08D788306BC2F
tds  = 0x6AF3094F
```
