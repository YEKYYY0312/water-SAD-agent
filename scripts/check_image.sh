#!/usr/bin/env bash
# P5 交付物：对**已构建**镜像做安全与构建上下文卫生检查。
#
# 检查面（任一违规 → exit 1，且逐条打印清晰行）：
#   a. 非 root：`docker inspect --format '{{.Config.User}}'` 非空且不是 root/0；
#   b. 无密钥材料：镜像文件系统里不存在 .env / .env.*、/app/secrets、/app 下的
#      *.pem|*.key，且 /app（排除 /app/.venv）里 grep 不到凭据形态字面量；
#   c. 无构建上下文泄漏：/app/.git、/app/tests、/app/.env、/app/secrets、
#      /app/data/eval/reports、/app/backups 均不存在（/app/.venv 是应用虚拟环境，
#      必须存在，**不算**泄漏）；
#   d. 期望运行用户是 wateragent；其他非 root 用户只告警，不影响退出码。
#
# 用法：
#   bash scripts/check_image.sh [image-ref] [--require-scanners]
#     image-ref           默认 water-agent-app:local
#     --require-scanners  把"找不到外部漏洞扫描器"从结构化 SKIP 升级为 FAILURE；
#                         这是**发布门禁**选项，CI 默认不加（见 p5-compose.yml）。
#
# 外部扫描器（trivy/grype/docker scout）是 best-effort：只在存在且可用时跑，
# 缺失或（未登录/无网络导致）跑不起来 → 结构化 SKIP；扫出 HIGH/CRITICAL → FAIL。
#
# 退出码：0 全部必需检查 PASS（--require-scanners 时还要求至少跑过一个扫描器）
#         1 有检查 FAIL（含探针跑不起来：无法证明无密钥 → fail closed）
#         2 参数错误
#         0 结构化 SKIP：无 docker CLI / 守护进程不可达 / 镜像未构建
#           （任务书允许这三类 SKIP，SKIP 既不算通过也不算失败）
#
# 安全纪律：
#   * 只报告命中的**文件路径 + 规则名**，绝不回显命中的文本（grep -l），日志里不出现密钥值；
#   * 不 docker push、不挂载宿主目录、不修改宿主；探针容器 `--rm --network none`；
#   * 只对已构建镜像做只读检查，不触发任何构建/拉取。
set -euo pipefail

IMAGE="water-agent-app:local"
REQUIRE_SCANNERS=0

usage() {
    cat <<'USAGE'
usage: bash scripts/check_image.sh [image-ref] [--require-scanners]

  image-ref           image reference to inspect (default: water-agent-app:local)
  --require-scanners  treat a missing vulnerability scanner as FAILURE
                      (release gate only; CI runs without it)
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --require-scanners) REQUIRE_SCANNERS=1 ;;
        -h|--help) usage; exit 0 ;;
        --) shift; break ;;
        -*) echo "check_image: unknown option: $1" >&2; usage >&2; exit 2 ;;
        *) IMAGE="$1" ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# JSON helpers（不依赖 jq；只做最小转义，细节串我们自己生成）
# ---------------------------------------------------------------------------
json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | LC_ALL=C tr -d '\000-\037'
}

# 单行结构化 SKIP：既不算通过也不算失败（任务书允许的缺前置条件语义）。
skip_exit() {
    printf '{"status": "SKIP", "reason": "%s", "image": "%s"}\n' \
        "$(json_escape "$1")" "$(json_escape "$IMAGE")"
    exit 0
}

CHECKS=()
SCANNERS=()
SKIPPED=()
FAILED=0

add_check() { # name status detail
    CHECKS+=("$1|$2|$3")
    if [ "$2" = "FAIL" ]; then
        FAILED=1
    fi
    printf '[%s] %s: %s\n' "$2" "$1" "$3"
}

join_lines() { tr '\n' ' ' | sed -e 's/  */ /g' -e 's/^ *//' -e 's/ *$//'; }

# ---------------------------------------------------------------------------
# Docker CLI / 守护进程 / 镜像是否本地已构建
# ---------------------------------------------------------------------------
# Docker Desktop on macOS often keeps docker off PATH; try the app bundle too.
DOCKER="${DOCKER:-$(command -v docker || true)}"
if [ -z "$DOCKER" ] && [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
    DOCKER=/Applications/Docker.app/Contents/Resources/bin/docker
fi

if [ -z "$DOCKER" ]; then
    # 本机没有 docker CLI：按任务书允许 SKIP（确定性检查在 CI/有 Docker 的机器上执行）。
    skip_exit "docker CLI not found"
fi

if ! "$DOCKER" info >/dev/null 2>&1; then
    skip_exit "docker daemon unreachable"
fi

if ! "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1; then
    skip_exit "image $IMAGE is not built locally"
fi

echo "==> check_image: inspecting $IMAGE"

TMP_RUN="$(mktemp -d "${TMPDIR:-/tmp}/check_image.XXXXXX")"
trap 'rm -rf "$TMP_RUN"' EXIT

# ---------------------------------------------------------------------------
# a/d. 运行用户（镜像运行时身份）
# ---------------------------------------------------------------------------
RUNTIME_USER="$("$DOCKER" inspect --format '{{.Config.User}}' "$IMAGE" 2>/dev/null || true)"
USER_PART="${RUNTIME_USER%%:*}"

if [ -z "$RUNTIME_USER" ]; then
    add_check runtime_user_non_root FAIL "Config.User is empty; the image would run as root"
elif [ "$USER_PART" = "root" ] || [ "$USER_PART" = "0" ]; then
    add_check runtime_user_non_root FAIL "Config.User='$RUNTIME_USER'; root is not allowed"
else
    add_check runtime_user_non_root PASS "Config.User='$RUNTIME_USER'"
fi

if [ "$USER_PART" = "wateragent" ]; then
    add_check runtime_user_is_wateragent PASS "runtime user is wateragent as expected"
elif [ "$USER_PART" = "root" ] || [ "$USER_PART" = "0" ] || [ -z "$RUNTIME_USER" ]; then
    add_check runtime_user_is_wateragent FAIL "runtime user is '$RUNTIME_USER'; expected wateragent"
else
    # 非 root 但不是本项目期望用户：只告警，不影响退出码。
    add_check runtime_user_is_wateragent PASS \
        "WARNING: runtime user is '$RUNTIME_USER'; expected wateragent (non-root, exit code unaffected)"
fi

# ---------------------------------------------------------------------------
# b/c. 镜像文件系统探针（单次 docker run，只输出机器可读标记行）
# ---------------------------------------------------------------------------
# 探针先落成临时文件再读进变量：macOS 自带的 bash 3.2 会把 `$( )` 内 heredoc 正文里的
# 裸 `)` 当成命令替换的结束符，把探针正文当宿主命令执行（实测 line 180 处炸）。
PROBE_FILE="$TMP_RUN/probe.sh"
cat >"$PROBE_FILE" <<'PROBE_SCRIPT'
set -u

check_path() {
  if [ -e "$2" ]; then
    printf 'PATH_FOUND %s %s\n' "$1" "$2"
  else
    printf 'PATH_MISSING %s %s\n' "$1" "$2"
  fi
}

# 只输出命中**文件路径**（grep -l），绝不输出命中文本，避免密钥进日志。
scan() {
  grep -rlE --exclude-dir=.venv -- "$2" /app 2>/dev/null \
    | while IFS= read -r hit; do printf 'GREP_HIT %s %s\n' "$1" "$hit"; done
}

# 同上，但按**精确字面量**豁免一个已公开的合成 red-team 夹具值。
# 豁免是枚举式的：只有文件里所有命中值都恰好等于该夹具串才判干净；任何其他
# 形状相同的字面量（例如真的泄漏了 Key）仍会命中 → FAIL。之所以不改写夹具：它
# 就是 governance 红队语料里"看起来像真实凭据"的输入，改掉会削弱那条测试的意图
# （见 docs/design/p5-step-45-decision.md）。
scan_except_literal() { # rule pattern allowed-literal
  grep -rlE --exclude-dir=.venv -- "$2" /app 2>/dev/null \
    | while IFS= read -r hit; do
        unexpected="$(grep -oE -- "$2" "$hit" 2>/dev/null | grep -vxF -- "$3" | head -1)"
        if [ -n "$unexpected" ]; then
          printf 'GREP_HIT %s %s\n' "$1" "$hit"
        else
          printf 'INFO fixture_literal_exempt %s %s\n' "$1" "$hit"
        fi
      done
}

# (b) 文件名形态的密钥材料
find /app -xdev \( -name '.env' -o -name '.env.*' \) -print 2>/dev/null \
  | while IFS= read -r envfile; do printf 'PATH_FOUND env_file %s\n' "$envfile"; done
check_path secrets_dir /app/secrets

# (c) 构建上下文泄漏（/app/.venv 是应用虚拟环境，必须存在，不在此列）
check_path build_leak_git /app/.git
check_path build_leak_tests /app/tests
check_path build_leak_env /app/.env
check_path build_leak_reports /app/data/eval/reports
check_path build_leak_backups /app/backups
if [ -d /app/.venv ]; then
  printf 'INFO venv_present /app/.venv\n'
else
  printf 'INFO venv_absent /app/.venv\n'
fi

# (b) *.pem / *.key：/app/.venv 下是第三方依赖自带的公共 CA 证书包（如 certifi
# 的 cacert.pem），属于依赖内容而非本项目密钥材料，单列 INFO；其余位置一律违规。
find /app -xdev \( -name '*.pem' -o -name '*.key' \) -print 2>/dev/null \
  | while IFS= read -r keyfile; do
      case "$keyfile" in
        /app/.venv/*) printf 'INFO venv_certificate %s\n' "$keyfile" ;;
        *) printf 'PATH_FOUND pem_key_file %s\n' "$keyfile" ;;
      esac
    done

# (b) 凭据形态字面量：只匹配"赋值/凭据形状"，不匹配裸词 sk-
scan postgres_secret_reference 'postgres_password'
scan deepseek_key_assignment 'DEEPSEEK_API_KEY='
scan dashscope_key_assignment 'DASHSCOPE_API_KEY='
# 唯一豁免：governance 红队语料里那条**合成**夹具（它必须长得像真实 Key）。
# 值来自 src/water_agent/runtime/governance/scenarios.py，已随仓库公开，非密钥。
scan_except_literal api_key_shape 'sk-[A-Za-z0-9_-]{20,}' \
  'sk-abcdef0123456789abcdef0123456789'
scan pem_private_key_header '-----BEGIN '

printf 'PROBE_DONE\n'
PROBE_SCRIPT

PROBE="$(cat "$PROBE_FILE")"

probe_paths() { # rule -> matched paths (one per line); never matched text
    printf '%s\n' "$PROBE_OUT" | grep -E "^PATH_FOUND $1 " \
        | sed -e "s/^PATH_FOUND $1 //" || true
}

probe_scan_hits() { # rule
    printf '%s\n' "$PROBE_OUT" | grep -E "^GREP_HIT $1 " \
        | sed -e "s/^GREP_HIT $1 //" || true
}

probe_info() { # marker
    printf '%s\n' "$PROBE_OUT" | grep -E "^INFO $1 " \
        | sed -e "s/^INFO $1 //" || true
}

PROBE_OUT=""
PROBE_OK=0
PROBE_ENTRYPOINT="sh"

if PROBE_OUT="$("$DOCKER" run --rm --network none --entrypoint sh "$IMAGE" -c "$PROBE" 2>"$TMP_RUN/probe.err")" \
    && printf '%s\n' "$PROBE_OUT" | grep -q '^PROBE_DONE$'; then
    PROBE_OK=1
else
    # 少数镜像没有 PATH 上的 sh：退回 /bin/sh -c。
    PROBE_ENTRYPOINT="/bin/sh"
    if PROBE_OUT="$("$DOCKER" run --rm --network none --entrypoint /bin/sh "$IMAGE" -c "$PROBE" 2>"$TMP_RUN/probe.err")" \
        && printf '%s\n' "$PROBE_OUT" | grep -q '^PROBE_DONE$'; then
        PROBE_OK=1
        SKIPPED+=("probe_entrypoint:sh-not-on-path (used /bin/sh -c)")
    fi
fi

if [ "$PROBE_OK" -ne 1 ]; then
    # 探针跑不起来 = 无法证明镜像里没有密钥 → fail closed，不给 SKIP。
    detail="container probe failed via --entrypoint sh and /bin/sh (see the error below)"
    probe_error="$(tail -n 3 "$TMP_RUN/probe.err" 2>/dev/null | join_lines || true)"
    add_check secret_material_absent FAIL "$detail: ${probe_error:-no stderr captured}"
    add_check secret_pattern_scan FAIL "$detail"
    add_check no_build_context_leak FAIL "$detail"
else
    echo "==> container probe ran with --entrypoint $PROBE_ENTRYPOINT"

    # -- b1. .env / .env.* / /app/secrets / *.pem|*.key
    env_hits="$(probe_paths env_file | join_lines)"
    secrets_hits="$(probe_paths secrets_dir | join_lines)"
    pem_hits="$(probe_paths pem_key_file | join_lines)"
    venv_certs="$(probe_info venv_certificate | join_lines)"
    venv_note=""
    if [ -n "$venv_certs" ]; then
        # 只报告路径；第三方依赖自带的公共证书包不算密钥材料。
        venv_note="; third-party certificate bundle(s) under /app/.venv excluded (not project secret material): $venv_certs"
    fi
    if [ -n "$env_hits" ] || [ -n "$secrets_hits" ] || [ -n "$pem_hits" ]; then
        secret_hits=""
        if [ -n "$env_hits" ]; then secret_hits="${secret_hits} env_file=[${env_hits}]"; fi
        if [ -n "$secrets_hits" ]; then secret_hits="${secret_hits} secrets_dir=[${secrets_hits}]"; fi
        if [ -n "$pem_hits" ]; then secret_hits="${secret_hits} pem_key_file=[${pem_hits}]"; fi
        add_check secret_material_absent FAIL "secret material found (paths only):${secret_hits}${venv_note}"
    else
        add_check secret_material_absent PASS \
            "no .env/.env.*, no /app/secrets, no *.pem/*.key outside /app/.venv${venv_note}"
    fi

    # -- b2. 凭据形态字面量（只报路径 + 规则名）
    pattern_hits=""
    for rule in postgres_secret_reference deepseek_key_assignment dashscope_key_assignment api_key_shape pem_private_key_header; do
        rule_hits="$(probe_scan_hits "$rule" | join_lines)"
        if [ -n "$rule_hits" ]; then
            pattern_hits="${pattern_hits} ${rule}=[${rule_hits}]"
        fi
    done
    fixture_note=""
    fixture_hits="$(probe_info fixture_literal_exempt | join_lines)"
    if [ -n "$fixture_hits" ]; then
        fixture_note="; one published synthetic red-team fixture literal exempted in: ${fixture_hits}"
    fi
    if [ -n "$pattern_hits" ]; then
        add_check secret_pattern_scan FAIL "credential-shaped literal found (paths only):${pattern_hits}${fixture_note}"
    else
        add_check secret_pattern_scan PASS \
            "no credential-shaped literal under /app (excluding /app/.venv)${fixture_note}"
    fi

    # -- c. 构建上下文泄漏
    leak_hits=""
    for rule in build_leak_git build_leak_tests build_leak_env build_leak_reports build_leak_backups; do
        rule_hits="$(probe_paths "$rule" | join_lines)"
        if [ -n "$rule_hits" ]; then
            leak_hits="${leak_hits} ${rule}=[${rule_hits}]"
        fi
    done
    venv_present="$(probe_info venv_present | join_lines)"
    if [ -n "$leak_hits" ]; then
        add_check no_build_context_leak FAIL "build context leaked into the image:${leak_hits}"
    elif [ -z "$venv_present" ]; then
        add_check no_build_context_leak FAIL \
            "/app/.venv is missing; the image does not carry the locked application venv"
    else
        add_check no_build_context_leak PASS \
            "no /app/.git, /app/tests, /app/.env, /app/secrets, /app/data/eval/reports, /app/backups; /app/.venv present"
    fi
fi

# ---------------------------------------------------------------------------
# 外部漏洞扫描器（best-effort，只在 PATH 上存在时跑）
#
# "跑不起来"（未登录 / 无网络 / 镜像不在 registry）与"扫出 HIGH/CRITICAL"必须区分：
#   前者是任务书定义的**前置条件缺失 → 结构化 SKIP**（SKIP 既不算通过也不算失败），
#   后者才是 FAIL。否则一台装了 docker scout 插件但没登录的机器（GitHub 托管 runner
#   就是这种）会被记成"发现漏洞"，门禁红得毫无信息量。
# 注意：`docker scout cves` 按规格原样调用，scout 默认不因发现漏洞而失败（需要额外的
# --exit-code）；trivy/grype 的 --exit-code 1 / --fail-on high 才是显式的失败语义。
# ---------------------------------------------------------------------------
SCANNER_RAN=0
SCANNER_FAILED=0
SCANNER_ABSENT=""
SCANNER_UNUSABLE=""

# 扫描器"不可用"的签名（大小写不敏感）。只匹配执行前提缺失，不匹配漏洞条目文本。
SCANNER_UNUSABLE_RE='not logged in|docker login|log in to docker|unauthorized|access denied|403 forbidden|no basic auth|no such host|connection refused|connection reset|i/o timeout|network is unreachable|dial tcp|tls handshake|unable to find image|no such image|manifest unknown|failed to download|failed to fetch|cannot connect to the docker daemon|error during connect'

run_scanner() { # name, command...
    local name="$1"
    shift
    local log="$TMP_RUN/scanner_${name}.log"
    local status detail
    if "$@" >"$log" 2>&1; then
        status="PASS"
        detail="exit 0"
        SCANNER_RAN=$((SCANNER_RAN + 1))
    elif grep -qiE "$SCANNER_UNUSABLE_RE" "$log" 2>/dev/null; then
        status="SKIP"
        detail="scanner could not run (precondition missing); last line: $(tail -n 1 "$log" 2>/dev/null | join_lines || true)"
        SCANNER_UNUSABLE="${SCANNER_UNUSABLE} ${name}"
        SKIPPED+=("scanner:${name}:not-usable")
    else
        status="FAIL"
        SCANNER_FAILED=1
        SCANNER_RAN=$((SCANNER_RAN + 1))
        detail="non-zero exit (findings or error); tail: $(tail -n 3 "$log" 2>/dev/null | join_lines || true)"
    fi
    SCANNERS+=("$name|$status|$detail")
    printf '[%s] scanner %s: %s\n' "$status" "$name" "$detail"
}

if command -v trivy >/dev/null 2>&1; then
    run_scanner trivy trivy image --exit-code 1 --severity HIGH,CRITICAL "$IMAGE"
else
    SCANNER_ABSENT="${SCANNER_ABSENT} trivy"
fi

if command -v grype >/dev/null 2>&1; then
    run_scanner grype grype "$IMAGE" --fail-on high
else
    SCANNER_ABSENT="${SCANNER_ABSENT} grype"
fi

# docker scout 是 docker CLI 插件：插件没装时不要把"插件缺失"记成扫描失败。
if "$DOCKER" scout version >/dev/null 2>&1; then
    run_scanner docker-scout "$DOCKER" scout cves "$IMAGE"
else
    SCANNER_ABSENT="${SCANNER_ABSENT} docker-scout"
fi

if [ "$SCANNER_FAILED" -ne 0 ]; then
    add_check vulnerability_scanners FAIL \
        "$SCANNER_RAN scanner(s) ran; at least one reported high/critical findings or failed; see scanners[]"
elif [ "$SCANNER_RAN" -eq 0 ]; then
    if [ -n "$SCANNER_UNUSABLE" ]; then
        reason="vulnerability scanner(s) present but not usable:${SCANNER_UNUSABLE} (missing precondition, e.g. registry login or network)"
    else
        reason="no vulnerability scanner on PATH (trivy/grype/docker scout)"
    fi
    printf '{"status": "SKIP", "reason": "%s", "image": "%s"}\n' \
        "$(json_escape "$reason")" "$(json_escape "$IMAGE")"
    for scanner_name in trivy grype docker-scout; do
        case " ${SCANNER_UNUSABLE} " in
            *" ${scanner_name} "*) ;;
            *) SKIPPED+=("scanner:${scanner_name}") ;;
        esac
    done
    if [ "$REQUIRE_SCANNERS" -eq 1 ]; then
        add_check vulnerability_scanners FAIL \
            "--require-scanners was passed but no scanner actually ran (absent:${SCANNER_ABSENT:-none}; unusable:${SCANNER_UNUSABLE:-none})"
    else
        add_check vulnerability_scanners SKIP \
            "${reason}; best-effort scan skipped (release gate passes --require-scanners)"
    fi
else
    add_check vulnerability_scanners PASS "$SCANNER_RAN scanner(s) ran and passed"
fi

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
if [ "$FAILED" -ne 0 ]; then
    OK="false"
else
    OK="true"
fi

first=1
printf '{"ok": %s, "image": "%s", "checks": [' "$OK" "$(json_escape "$IMAGE")"
for entry in ${CHECKS[@]+"${CHECKS[@]}"}; do
    name="${entry%%|*}"
    rest="${entry#*|}"
    status="${rest%%|*}"
    detail="${rest#*|}"
    if [ "$first" -eq 0 ]; then
        printf ', '
    fi
    first=0
    printf '{"name": "%s", "status": "%s", "detail": "%s"}' \
        "$(json_escape "$name")" "$status" "$(json_escape "$detail")"
done
printf '], "scanners": ['
first=1
for entry in ${SCANNERS[@]+"${SCANNERS[@]}"}; do
    name="${entry%%|*}"
    rest="${entry#*|}"
    status="${rest%%|*}"
    detail="${rest#*|}"
    if [ "$first" -eq 0 ]; then
        printf ', '
    fi
    first=0
    printf '{"name": "%s", "status": "%s", "detail": "%s"}' \
        "$(json_escape "$name")" "$status" "$(json_escape "$detail")"
done
printf '], "skipped": ['
first=1
for entry in ${SKIPPED[@]+"${SKIPPED[@]}"}; do
    if [ "$first" -eq 0 ]; then
        printf ', '
    fi
    first=0
    printf '"%s"' "$(json_escape "$entry")"
done
printf ']}\n'

if [ "$FAILED" -ne 0 ]; then
    echo "check_image: FAIL ($IMAGE)" >&2
    exit 1
fi

echo "check_image: PASS ($IMAGE)"
exit 0
