#!/usr/bin/env bash
# P2 Step 2：DWG → DXF 批量转换（ODA File Converter）。
#
# 用法：
#   bash scripts/convert_dwg_batch.sh --in DIR --out DIR [options]
#   bash scripts/convert_dwg_batch.sh --map FILE --out DIR [options]
#
# 选项：
#   --in DIR        输入目录（递归收集 *.dwg）
#   --map FILE      或：JSON 清单（每项含 path 与 sid），据此挑文件并 ASCII 暂存
#   --out DIR       输出目录（DXF），默认 /tmp/dxf_converted
#   --staging DIR   暂存目录，默认 <out>/.staging
#   --version VER   输出 DWG/DXF 版本，默认 ACAD2018（DXF 头 $ACADVER=AC1032）
#   --type T        输出类型，默认 DXF
#   --audit 0|1     转换前审计修复，默认 1
#   --oda PATH      ODAFileConverter 可执行文件，默认自动探测
#   --log FILE      转换日志 TSV，默认 <out>/conversion_log.tsv
#
# 行为与诚实性约定：
#   * ODA 对"部分文件失败"仍返回 exit 0，**退出码不可信**。本脚本以
#     "输出文件是否存在 + DXF 魔数 + $ACADVER" 三者共同判定单文件成败，
#     不把 ODA 的 0 当作成功证据。
#   * 先整目录批量转换（一次 ODA 调用，避免 N 次 Qt 启动开销），再对缺失项
#     逐文件重试并记录重试原因；仍失败者记 FAILED，不静默跳过。
#   * 中文文件名一律先以 ASCII 暂存名（sid）落盘再转换，规避转换器编码风险；
#     原名与 sid 的映射写入 <out>/conversion_map.json，供后续步骤回溯。
#   * 不写入任何图纸内容到日志：日志只记路径、字节数、状态、DXF 版本头。
#
# 退出码：0 全部成功 / 1 存在 FAILED / 2 配置或前置条件错误。
set -euo pipefail

IN_DIR=""
MAP_FILE=""
OUT_DIR="/tmp/dxf_converted"
STAGING=""
VERSION="ACAD2018"
FTYPE="DXF"
AUDIT="1"
ODA=""
LOG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --in)       IN_DIR="$2"; shift 2 ;;
    --map)      MAP_FILE="$2"; shift 2 ;;
    --out)      OUT_DIR="$2"; shift 2 ;;
    --staging)  STAGING="$2"; shift 2 ;;
    --version)  VERSION="$2"; shift 2 ;;
    --type)     FTYPE="$2"; shift 2 ;;
    --audit)    AUDIT="$2"; shift 2 ;;
    --oda)      ODA="$2"; shift 2 ;;
    --log)      LOG="$2"; shift 2 ;;
    -h|--help)  sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[ -n "$STAGING" ] || STAGING="$OUT_DIR/.staging"
[ -n "$LOG" ] || LOG="$OUT_DIR/conversion_log.tsv"

# ---- 定位 ODAFileConverter -------------------------------------------------
if [ -z "$ODA" ]; then
  for cand in \
    "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter" \
    "/usr/bin/ODAFileConverter" \
    "/usr/local/bin/ODAFileConverter"; do
    [ -x "$cand" ] && { ODA="$cand"; break; }
  done
fi
if [ -z "$ODA" ]; then
  ODA="$(command -v ODAFileConverter 2>/dev/null || true)"
fi
if [ -z "$ODA" ] || [ ! -x "$ODA" ]; then
  echo "前置条件缺失：未找到 ODAFileConverter 可执行文件。请用 --oda 指定路径。" >&2
  echo "macOS 默认位置：/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter" >&2
  exit 2
fi
if [ -z "$IN_DIR" ] && [ -z "$MAP_FILE" ]; then
  echo "参数错误：必须提供 --in DIR 或 --map FILE 之一。" >&2
  exit 2
fi
if [ -n "$IN_DIR" ] && [ ! -d "$IN_DIR" ]; then
  echo "参数错误：--in 目录不存在：$IN_DIR" >&2; exit 2
fi
if [ -n "$MAP_FILE" ] && [ ! -f "$MAP_FILE" ]; then
  echo "参数错误：--map 文件不存在：$MAP_FILE" >&2; exit 2
fi

echo "ODA      : $ODA"
echo "版本/类型: $VERSION / $FTYPE  (audit=$AUDIT)"
echo "输出目录 : $OUT_DIR"
echo "暂存目录 : $STAGING"

# ---- 组装工作清单（ASCII 暂存）--------------------------------------------
mkdir -p "$OUT_DIR" "$STAGING"
rm -rf "${STAGING:?}"/* 2>/dev/null || true

PY_BIN="${PY_BIN:-_venv/bin/python}"
if [ ! -x "$PY_BIN" ]; then PY_BIN="$(command -v python3)"; fi

WORKLIST="$OUT_DIR/.worklist.json"
MAP_OUT="$OUT_DIR/conversion_map.json"

"$PY_BIN" - "$IN_DIR" "$MAP_FILE" "$STAGING" "$WORKLIST" "$MAP_OUT" <<'PYEOF'
import json, os, shutil, sys, hashlib
from pathlib import Path

in_dir, map_file, staging, worklist, map_out = sys.argv[1:6]
staging = Path(staging)

def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

items = []
if map_file:
    for i, r in enumerate(json.load(open(map_file)), 1):
        src = Path(r["path"])
        if not src.is_file():
            print(f"WARN 清单项不存在，跳过: {src}", file=sys.stderr)
            continue
        sid = r.get("sid") or f"f{i:03d}"
        items.append({"sid": sid, "src": str(src), "orig_name": src.name,
                      "cls": r.get("cls", ""), "proj": r.get("proj", ""),
                      "src_bytes": src.stat().st_size})
else:
    files = sorted(p for p in Path(in_dir).rglob("*.dwg") if p.is_file())
    for i, p in enumerate(files, 1):
        items.append({"sid": f"f{i:03d}", "src": str(p), "orig_name": p.name,
                      "cls": "", "proj": p.parent.name,
                      "src_bytes": p.stat().st_size})

seen = {}
for it in items:
    sid = it["sid"]
    if sid in seen:                     # sid 冲突加后缀，避免互相覆盖
        n = 2
        while f"{sid}_{n}" in seen:
            n += 1
        sid = f"{sid}_{n}"
        it["sid"] = sid
    seen[sid] = True
    dst = staging / f"{sid}.dwg"
    shutil.copy2(it["src"], dst)
    it["staged"] = str(dst)
    it["src_md5"] = md5(dst)

json.dump(items, open(worklist, "w"), ensure_ascii=False, indent=1)
json.dump(items, open(map_out, "w"), ensure_ascii=False, indent=1)
print(f"工作清单: {len(items)} 份 DWG 已 ASCII 暂存")
PYEOF

# ---- 批量转换 --------------------------------------------------------------
ODA_LOG="$OUT_DIR/.oda_stdout.log"
echo "--- 批量转换（单次 ODA 调用）---"
set +e
"$ODA" "$STAGING" "$OUT_DIR" "$VERSION" "$FTYPE" 0 "$AUDIT" "*.dwg" >"$ODA_LOG" 2>&1
ODA_RC=$?
set -e
echo "ODA exit=$ODA_RC（注：ODA 对部分失败仍返回 0，退出码不作为成功证据）"
[ -s "$ODA_LOG" ] && { echo "--- ODA 输出（尾部 20 行）---"; tail -20 "$ODA_LOG"; }

# ---- 逐项校验 + 缺失项重试 -------------------------------------------------
"$PY_BIN" - "$WORKLIST" "$OUT_DIR" "$STAGING" "$ODA" "$VERSION" "$FTYPE" "$AUDIT" "$LOG" <<'PYEOF'
import json, os, re, subprocess, sys, datetime
from pathlib import Path

worklist, out_dir, staging, oda, version, ftype, audit, log_path = sys.argv[1:9]
out_dir, staging = Path(out_dir), Path(staging)
items = json.load(open(worklist))

DXF_VER_NAME = {"AC1009": "R12", "AC1012": "R13", "AC1014": "R14", "AC1015": "2000",
                "AC1018": "2004", "AC1021": "2007", "AC1024": "2010", "AC1027": "2013",
                "AC1032": "2018"}
EXPECT_ACADVER = {"ACAD2018": "AC1032", "ACAD2013": "AC1027", "ACAD2010": "AC1024",
                  "ACAD2007": "AC1021", "ACAD2004": "AC1018", "ACAD2000": "AC1015",
                  "ACAD14": "AC1014", "ACAD12": "AC1009"}.get(version, "")

def probe(p):
    """返回 (ok, acadver, insunits, codepage, reason)。读前 64KB 足够拿到头段。"""
    try:
        raw = p.read_bytes()[:65536]
    except Exception as e:
        return False, "", "", "", f"READ_ERROR:{e}"
    if not raw.lstrip()[:1].startswith(b"0"):
        return False, "", "", "", "NOT_DXF_MAGIC"
    txt = raw.decode("ascii", "replace")
    def g(code, name):
        m = re.search(r"\$" + name + r"\s*\r?\n\s*" + code + r"\s*\r?\n\s*([^\r\n]+)", txt)
        return m.group(1).strip() if m else ""
    av = g("1", "ACADVER")
    iu = g("70", "INSUNITS")
    cp = g("3", "DWGCODEPAGE")
    if not av:
        return False, "", iu, cp, "NO_ACADVER"
    if EXPECT_ACADVER and av != EXPECT_ACADVER:
        return False, av, iu, cp, f"VERSION_MISMATCH(expect {EXPECT_ACADVER})"
    return True, av, iu, cp, ""

rows, n_ok, n_retry_ok, n_fail = [], 0, 0, 0
for it in items:
    src = Path(it["staged"])
    dst = out_dir / f"{src.stem}.{ftype.lower()}"
    status, reason = "", ""
    if not dst.is_file():
        # 缺失 → 单文件重试（该文件单独建目录，避免影响其他）
        solo_in = staging / "_solo_in"; solo_out = staging / "_solo_out"
        for d in (solo_in, solo_out):
            if d.exists():
                for f in d.glob("*"):
                    f.unlink()
            d.mkdir(parents=True, exist_ok=True)
        import shutil as _sh
        _sh.copy2(src, solo_in / src.name)
        try:
            subprocess.run([oda, str(solo_in), str(solo_out), version, ftype, "0", audit, "*.dwg"],
                           capture_output=True, timeout=300, check=False)
        except Exception as e:
            reason = f"RETRY_EXC:{type(e).__name__}"
        cand = solo_out / f"{src.stem}.{ftype.lower()}"
        if cand.is_file():
            _sh.copy2(cand, dst)
            status = "RETRY_OK"
        else:
            status = "FAILED"
            reason = reason or "NO_OUTPUT"
    else:
        status = "OK"

    ok, av, iu, cp, why = (False, "", "", "", "") if status == "FAILED" else probe(dst)
    if status != "FAILED" and not ok:
        status, reason = "FAILED", why
    if status == "OK":
        n_ok += 1
    elif status == "RETRY_OK":
        n_retry_ok += 1
    else:
        n_fail += 1

    rows.append({
        "sid": it["sid"], "orig_name": it["orig_name"], "cls": it.get("cls", ""),
        "proj": it.get("proj", ""), "status": status,
        "src_bytes": it["src_bytes"], "dxf_bytes": dst.stat().st_size if dst.is_file() else 0,
        "acadver": av, "dxf_release": DXF_VER_NAME.get(av, ""),
        "insunits": iu, "codepage": cp, "src_md5": it["src_md5"], "reason": reason,
    })

# TSV 日志（供人读）+ JSON（供后续步骤消费）
hdr = ["sid", "status", "acadver", "dxf_release", "src_bytes", "dxf_bytes",
       "insunits", "codepage", "cls", "orig_name", "reason"]
with open(log_path, "w") as f:
    f.write(f"# DWG->DXF conversion log  {datetime.datetime.now().isoformat(timespec='seconds')}\n")
    f.write(f"# version={version} type={ftype} audit={audit} oda={oda}\n")
    f.write("\t".join(hdr) + "\n")
    for r in rows:
        f.write("\t".join(str(r.get(k, "")) for k in hdr) + "\n")

json.dump(rows, open(out_dir / "conversion_log.json", "w"), ensure_ascii=False, indent=1)

print(f"\n{'sid':<9}{'状态':<10}{'DXF版本':<10}{'源KB':>8}{'DXF KB':>9}  原始文件名")
print("-" * 96)
for r in rows:
    print(f"{r['sid']:<9}{r['status']:<10}{(r['acadver'] or '-'):<10}"
          f"{r['src_bytes']/1024:>8.1f}{r['dxf_bytes']/1024:>9.1f}  {r['orig_name'][:44]}"
          + (f"   <- {r['reason']}" if r["reason"] else ""))
print(f"\n成功 {n_ok} | 重试后成功 {n_retry_ok} | 失败 {n_fail} | 合计 {len(rows)}")
print(f"日志: {log_path}\n      {out_dir/'conversion_log.json'}")
sys.exit(1 if n_fail else 0)
PYEOF
RC=$?
echo "== 转换阶段退出码: $RC =="
exit $RC
