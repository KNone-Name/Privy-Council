# v3live 版本说明

- 版本：0.1.0
- 打包日期：2026-11-20
- 本目录 13 个文件 = 读取层全部内容

## 入口

```
python tools\v3live\run.py <命令>        # 直接跑
python -m v3live <命令>                  # 需先 sys.path 指向 tools\
```

## 与 2026-11-12 版相比的改动

| 改动 | 影响 |
|---|---|
| GDP 默认口径 = 市场价 + 实际就业 + 只算生产建筑 | 五国校准误差从 27% 降到 2% 以内（美国 +106% → +0.6%） |
| `--compare` 多国并排比对 | 新增 |
| `--by-type` 显示每类建筑的 GDP 贡献 | 新增 |
| `CDecree` 扫全部 vtable + 基址自校验 | 法令从 1 条 → 65 条 |
| `capacity` 州级修正按州分组 | 圣彼得堡 10.4% 可复现 |
| 输出编码加固（`−`/`£` 在 GBK 控制台会崩） | 已修 |
| 新增 `verify_all.py` / `selftest.py` / `diffscan.py` | 自检能力 |

## 升级方式

整目录替换即可，不要逐文件改。
替换后跑：

```
python tools\v3live\verify_all.py
```
