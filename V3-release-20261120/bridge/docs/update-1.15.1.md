# 1.15.1 适配数据（差分结果 + 实测结论）

> **实测结论（2026-11-12）**：1.15.1 上所有数据偏移**均未变化**。
> 已用 v109 验证：玩家国 157、类型表 633、PM 读改写、建造入队、队列读取、写锁全部正常。
> 类型表改为全堆扫描后，首次可用约 125 秒（纯数据偏移无需改动，慢的只是定位类型实例）。

- 旧版：**1.15.0**，99,988,600 字节，FNV-1a64 `0x017B6FB764315747`，TDS `0x6AEA0F6D`
- 新版：**1.15.1**，100,221,048 字节，FNV-1a64 `0xF8A08D788306BC2F`，TDS `0x6AF3094F`
- ImageBase 两者都是 `0x140000000`
- 两个 exe 都已保存：
  - `_archive/victoria3_old_99988600.exe`
  - `_archive/victoria3_new_100221048.exe`

---

## 1. 指纹常量（v3bridge.cpp 第 36–38 行）

```cpp
static constexpr uint64_t kFileSize     = 100221048ULL;
static constexpr uint64_t kFileHash     = 0xF8A08D788306BC2FULL;
static constexpr uint32_t kTimeDate     = 0x6AF3094F;
```

---

## 2. 函数 RVA（`namespace rva`）

| 函数 | 旧 RVA | **新 RVA** | delta | 匹配度 |
|---|---|---|---|---|
| ResolveCountry | `0x0097B8D0` | **`0x00983A00`** | +33072 | 89% |
| ResolveState | `0x0097B530` | **`0x00983660`** | +33072 | 89% |
| CountLevels | `0x00954B40` | **`0x0095CB60`** | +32800 | 97% |
| BuildCommand | `0x0008BA770` | **`0x008C24B0`** | +32064 | 98% |
| BldLookup | `0x000DC0140` | **`0x00DCBD70`** | +48176 | 89%（768 字节窗口） |

`CountLevels` 的新地址被两种独立方法同时命中（24 字节签名的唯一命中 + 差分匹配），可信度最高。
不过它本来就是**坏的**（对所有输入返回 0），可以不管。

```cpp
namespace rva {
    constexpr uintptr_t ResolveCountry = 0x00983A00;
    constexpr uintptr_t ResolveState   = 0x00983660;
    constexpr uintptr_t CountLevels    = 0x0095CB60;
    constexpr uintptr_t BuildCommand   = 0x008C24B0;
    constexpr uintptr_t BldLookup      = 0x00DCBD70;
    constexpr uintptr_t DbGlobal       = 0x053FF778;   // ← 尚未重新定位，见 §5
    constexpr uintptr_t BuildCtxGlobal = 0x05C0DCA8;   // ← 新
}
```

---

## 3. 注册表全局（运行期从 resolver prologue 推导，不硬编码）

两个 resolver 的 prologue 与旧版**逐字节同构**：

```
48 83 EC 58            sub rsp, 0x58
48 8B 05 disp32        mov rax, [rip+disp32]     <- 全局
8B 11                  mov edx, [rcx]
48 85 C0               test rax, rax
75 67                  jne ...
```

| 全局 | 旧 RVA | **新 RVA** | 节 |
|---|---|---|---|
| regState | `0x05BD69C8` | **`0x05C0DCB8`** | .data |
| regCountry | `0x05C42C20` | **`0x05C0C460`** | .data |

DLL 里是 `GlobalFromResolver()` 现场算的，所以只要 resolver 地址对，这两个自动跟着走。

---

## 4. BuildCtx 全局（新）

**`0x05C0DCA8`**，在 regState 前 0x10（旧版是前 0x40）。

证据（调用点 `0x0088E765` 的指令序列，与 DLL 的 8 参数调用逐槽对应）：

```
0x0088E736  mov rax, [rip+0x537f56b]   -> 0x05C0DCA8
0x0088E73D  mov [rsp+0x38], rax        ; 第 8 个参数
0x0088E74E  mov [rsp+0x20], 1          ; 第 5 个参数（levels）
0x0088E75B  mov r8,  r12               ; btype
0x0088E75E  mov rdx, r13               ; country
0x0088E761  lea rcx, [rbp-8]           ; param
0x0088E765  call 0x1408c24b0           ; BuildCommand
```

DLL 的签名：`fn(&param, country, state, btype, levels, 1, 0xFFFFFFFF, ctx)`
→ rcx, rdx, r8, r9, [0x20], [0x28], [0x30], [0x38]，**完全对上**。

---

## 5. 仍然缺的：`DbGlobal`（类型表）

旧值 `0x053FF778`，尚未重新定位。

类型表是**运行期从 `common/buildings/*.txt` 构建**的，静态文件里没有实体，只能靠代码 xref 或运行期扫描。

**建议不要再找它**：`TypeByName` 完全可以改成「扫 CBuildingType 实例、按 `+0x18` 的键名建映射」——
`v3live` 已经在这么做（扫出 6932 栋建筑），而且这种方式**跨版本稳定**。

---

## 6. 新签名（位移全部通配，跨版本稳定）

旧签名全灭的原因：整段字节写死，含 `rel32` 位移。新签名把这些字节换成 `??`。

```cpp
// sub rsp,58 / mov rax,[rip+d] / mov edx,[rcx] / test rax,rax / jne / cmp edx,-1 / je
// lea rdx / lea rcx / call
static const uint8_t kSigResolveCountry[46] = { 48 83 EC 58 48 8B 05 ?? ?? ?? ?? 8B 11 48 85 C0 75 ?? ?? ?? ?? 0F 84 ?? ?? ?? ?? 48 8D 15 ?? ?? ?? ?? 48 8D 0D ?? ?? ?? ?? E8 ?? ?? ?? ?? };

static const uint8_t kSigResolveState[46] = { 48 83 EC 58 48 8B 05 ?? ?? ?? ?? 8B 11 48 85 C0 75 ?? ?? ?? ?? 0F 84 ?? ?? ?? ?? 48 8D 15 ?? ?? ?? ?? 48 8D 0D ?? ?? ?? ?? E8 ?? ?? ?? ?? };

// 注意：这两个只有 rip 位移不同，**单靠签名无法区分 State 和 Country**。
// 必须靠运行期探测（哪个全局里的条目首 qword 是 CState vtable）。

static const uint8_t kSigCountLevels[44] = { 4C 89 4C 24 20 55 56 57 48 83 EC 20 49 8B 00 48 8B E9 49 8B C8 49 8B F0 48 8B FA FF 50 08 84 C0 0F 84 ?? ?? ?? ?? 48 8B 07 48 8B CF };

static const uint8_t kSigBuildCommand[46] = { 48 89 5C 24 10 4C 89 4C 24 20 4C 89 44 24 18 48 89 4C 24 08 55 56 57 41 54 41 55 41 56 41 57 48 8D AC 24 E0 FE FF FF 48 81 EC 20 02 00 00 };

static const uint8_t kSigBldLookup[44] = { 48 89 5C 24 10 57 48 83 EC 30 83 3D 27 ?? ?? ?? ?? 48 8B DA 48 8B F9 75 ?? 48 8D 05 ?? ?? ?? ?? 48 8D 0D ?? ?? ?? ?? 48 89 44 24 28 };
```

> `kSigBuildCommand` 这次**有 0 个通配**且 98% 匹配——说明它的 prologue 里没有任何会随位移变化的字节，
> 这是最稳的一条。旧版之所以从没命中，是因为当年那 46 字节是凭记忆写的。

---

## 7. 数据偏移：需要实测

`+0x58`、`+0xED8`、`+0x1E8`、`+0x1F4`、`+0x98`、`+0xA4`、`+0xB00`、`+0xB78`、`+0x60/68/6C`、元素 `0x48`
——全部都**必须在新版上重新验证**，方法就是原来那套探针（`pmscan`/`pms`/`queue` + dump 反推）。

启动自检会先替我们筛一遍：`selfcheck: FAIL pm/queue` 说明对应那组偏移变了。

---

## 8. 下一步

1. 更新 `v3bridge.cpp` 里的 3 个指纹 + 5 个函数 RVA + 3 段签名 + `BuildCtxGlobal`。
2. 把 `TypeByName` 从「读 DbGlobal」改成「扫 CBuildingType 实例建映射」。
3. 编译 → 注入 → 看 `selfcheck:` 输出，按 FAIL 项逐个重测偏移。
4. 跑 `v3-测试.ps1` 全套。

---

## 附：差分方法（下次更新可以复用）

脚本在 `_archive/`：

- `match.py` —— 用多个 8 字节锚点投票 + 长窗口字节相等率打分，跨 rel32 位移匹配函数
- `gensig.py` / `gensig2.py` —— 用 capstone 反汇编，把跳转位移 / rip 位移 / 立即数替换成通配，生成稳定签名
- `ctx.py` / `ctx2.py` —— 找调用点、回溯 rip 引用定位相关全局

核心思路：**不要重新逆向，先拿旧版当模板做差分**。前提是保存了旧 exe。
