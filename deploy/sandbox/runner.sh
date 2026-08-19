#!/bin/sh
set -eu

# This script is part of the trusted runner image. Request payloads cannot
# replace it or append commands to it.
readonly action="${PRISM_ACTION:-}"
readonly language="${PRISM_LANGUAGE:-}"
readonly test_mode="${PRISM_TEST_MODE:-whitebox}"
readonly preview_port="${PRISM_PREVIEW_PORT:-8080}"

proxy_request() {
  [ "$#" -eq 7 ] || { printf '%s\n' 'invalid preview proxy arguments' >&2; exit 64; }
  method="$1"
  body_length="$2"
  max_bytes="$3"
  accept="$4"
  accept_language="$5"
  content_type="$6"
  target="$7"

  case "$method" in GET|HEAD|POST) ;; *) exit 64 ;; esac
  case "$body_length:$max_bytes:$preview_port" in
    *[!0-9:]*|:*|*::*|*:) exit 64 ;;
  esac
  fixed_origin="http://127.0.0.1:${preview_port}"
  case "$target" in
    "$fixed_origin"/*) request_target="${target#"$fixed_origin"}" ;;
    *) printf '%s\n' 'preview target must use the fixed loopback origin' >&2; exit 64 ;;
  esac
  carriage_return="$(printf '\r')"
  case "$request_target$accept$accept_language$content_type" in
    *"$carriage_return"*) exit 64 ;;
  esac
  case "$request_target$accept$accept_language$content_type" in
    *"
"*|*"
"*) exit 64 ;;
  esac

  exec bash -c '
    set -eu
    port="$1"
    method="$2"
    request_target="$3"
    body_length="$4"
    max_bytes="$5"
    accept="$6"
    accept_language="$7"
    content_type="$8"
    exec 3<>"/dev/tcp/127.0.0.1/$port"
    printf "%s %s HTTP/1.1\r\n" "$method" "$request_target" >&3
    printf "Host: 127.0.0.1:%s\r\nConnection: close\r\n" "$port" >&3
    [ -z "$accept" ] || printf "Accept: %s\r\n" "$accept" >&3
    [ -z "$accept_language" ] || printf "Accept-Language: %s\r\n" "$accept_language" >&3
    [ -z "$content_type" ] || printf "Content-Type: %s\r\n" "$content_type" >&3
    if [ "$method" = POST ]; then
      printf "Content-Length: %s\r\n\r\n" "$body_length" >&3
      cat >&3
    else
      printf "\r\n" >&3
    fi
    head -c "$max_bytes" <&3
  ' prism-preview "$preview_port" "$method" "$request_target" "$body_length" "$max_bytes" \
    "$accept" "$accept_language" "$content_type"
}

if [ "${1:-}" = "proxy" ]; then
  shift
  proxy_request "$@"
fi

case "$action" in
  test|deploy) ;;
  *) printf '%s\n' 'unsupported sandbox action' >&2; exit 64 ;;
esac
case "$language" in
  python|node|java|go|php) ;;
  *) printf '%s\n' 'unsupported sandbox language' >&2; exit 64 ;;
esac
case "$test_mode" in
  whitebox|blackbox|combined) ;;
  *) printf '%s\n' 'unsupported sandbox test mode' >&2; exit 64 ;;
esac

cp -R /source/. /workspace/
chmod -R u+rwX /workspace
cd /workspace

mkdir -p .prism-tmp .prism-home .prism-cache/npm .prism-cache/m2 .prism-cache/gradle .prism-cache/go

export PIP_NO_INDEX=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export NPM_CONFIG_OFFLINE=true
export NPM_CONFIG_AUDIT=false
export NPM_CONFIG_FUND=false
export NPM_CONFIG_CACHE=/workspace/.prism-cache/npm
export MAVEN_OPTS="-Dmaven.repo.local=/workspace/.prism-cache/m2 -Djava.io.tmpdir=/workspace/.prism-tmp -Djava.net.preferIPv4Stack=true"
export GRADLE_USER_HOME=/workspace/.prism-cache/gradle
export GOPROXY=off
export GOSUMDB=off
export GOCACHE=/workspace/.prism-cache/go/build
export GOMODCACHE=/workspace/.prism-cache/go/modules
export COMPOSER_DISABLE_NETWORK=1
export HOME=/workspace/.prism-home
export TMPDIR=/workspace/.prism-tmp
export JAVA_TOOL_OPTIONS="-Duser.home=$HOME -Djava.io.tmpdir=$TMPDIR"

preflight_agent_test_file() {
  # 先用语言原生工具完成解析/编译，避免后端根据自由文本猜测失败阶段。
  case "$1" in
    *.py) PYTHONPYCACHEPREFIX=/workspace/.prism-tmp/pycache python -m py_compile "$1" ;;
    *.js|*.mjs) node --check "$1" ;;
    *.php) php -l "$1" ;;
    *.go) go build -o /workspace/.prism-tmp/agent-test-bin "$1" ;;
    *.java)
      cls_dir=".prism-ai-classes"
      mkdir -p "$cls_dir"
      javac -d "$cls_dir" "$1" ;;
    *.sh) sh -n "$1" ;;
    *) return 64 ;;
  esac
}

run_agent_test_file() {
  # agent 动态生成的测试文件必须自包含可执行；预检成功后这里只执行测试。
  case "$1" in
    *.py) PYTHONPATH=/workspace${PYTHONPATH:+:$PYTHONPATH} python "$1" ;;
    *.js|*.mjs) node "$1" ;;
    *.php) php "$1" ;;
    *.go) /workspace/.prism-tmp/agent-test-bin ;;
    *.java)
      cls_dir=".prism-ai-classes"
      cls_name="$(basename "$1" .java)"
      java -cp "$cls_dir" "$cls_name" ;;
    *.sh) sh "$1" ;;
    *) return 64 ;;
  esac
}

agent_test_file_json() {
  # 文件名在注入前已限制为 ASCII 安全字符；输出以 base64 放入 JSON，避免日志破坏协议。
  agent_output=""
  if [ -f "$6" ]; then
    agent_output="$(tail -c 2000 "$6" | base64 | tr -d '\n')"
  fi
  printf '"%s":{"status":"%s","phase":"%s","failure_kind":"%s","exit_code":%s,"output_encoding":"base64","output_base64":"%s"}' \
    "$(basename "$1")" "$2" "$3" "$4" "$5" "$agent_output"
}

execute_agent_test() {
  # 输出一条文件级 JSON；调用方通过返回码决定整体测试是否通过。
  agent_file="$1"
  agent_output_file="$2"
  : >"$agent_output_file"
  if preflight_agent_test_file "$agent_file" >"$agent_output_file" 2>&1; then
    agent_preflight_rc=0
  else
    agent_preflight_rc=$?
  fi
  if [ "$agent_preflight_rc" -eq 0 ]; then
    if run_agent_test_file "$agent_file" >"$agent_output_file" 2>&1; then
      agent_test_file_json "$agent_file" pass execute "" 0 "$agent_output_file"
      return 0
    else
      agent_rc=$?
    fi
    agent_kind="execution_failure"
    case "$agent_rc" in 126|127) agent_kind="infrastructure_error" ;; esac
    agent_test_file_json "$agent_file" fail execute "$agent_kind" "$agent_rc" "$agent_output_file"
    return "$agent_rc"
  fi
  agent_rc="$agent_preflight_rc"
  agent_kind="compile_error"
  case "$agent_rc" in 126|127) agent_kind="infrastructure_error" ;; esac
  agent_test_file_json "$agent_file" fail compile "$agent_kind" "$agent_rc" "$agent_output_file"
  return "$agent_rc"
}

run_agent_tests() {
  # 白盒:常规测试后执行 agent 动态生成的断言文件,输出结构化结果标记。
  if [ ! -d ./_agent_tests ]; then
    printf '%s\n' 'PRISM_AGENT_TESTS_BEGIN {"protocol_version":2,"generated":0,"passed":0,"failed":0,"passed_count":0,"files":{},"file_results":{}} PRISM_AGENT_TESTS_END'
    return 0
  fi
  total=0; ok=0; failed=0; results=""; file_results=""
  for f in ./_agent_tests/*; do
    [ -f "$f" ] || continue
    case "$f" in *.py|*.js|*.mjs|*.php|*.go|*.java) ;; *) continue ;; esac
    # 黑盒脚本只在应用就绪后由 run_agent_blackbox 执行,白盒阶段跳过,避免时序失败
    case "$(basename "$f")" in blackbox.*) continue ;; esac
    total=$((total + 1))
    if agent_file_result="$(execute_agent_test "$f" /tmp/agent-test-out)"; then
      agent_rc=0
    else
      agent_rc=$?
    fi
    if [ "$agent_rc" -eq 0 ]; then
      ok=$((ok + 1)); status="pass"
    else
      failed=$((failed + 1)); status="fail"
      printf '%s\n' "agent test failed: $f" >&2
      tail -c 2000 /tmp/agent-test-out >&2 || true
    fi
    results="${results}\"$(basename "$f")\":\"$status\","
    file_results="${file_results}${agent_file_result},"
  done
  results="${results%,}"
  file_results="${file_results%,}"
  printf 'PRISM_AGENT_TESTS_BEGIN {"protocol_version":2,"generated":%s,"passed":%s,"failed":%s,"passed_count":%s,"files":{%s},"file_results":{%s}} PRISM_AGENT_TESTS_END\n' "$total" "$ok" "$failed" "$ok" "$results" "$file_results"
  [ "$failed" -eq 0 ]
}

run_agent_blackbox() {
  # 黑盒:应用就绪后执行 agent 生成的 blackbox 脚本(仅本机回环),失败不阻断就绪结论。
  # 探测脚本语言与项目语言一致:python→blackbox.py,node→blackbox.js,php→blackbox.php,go→blackbox.go,java→blackbox.java。
  for f in ./_agent_tests/blackbox.py ./_agent_tests/blackbox.js ./_agent_tests/blackbox.php ./_agent_tests/blackbox.go ./_agent_tests/blackbox.java ./_agent_tests/blackbox.sh; do
    if [ -f "$f" ]; then
      printf '%s\n' "executing agent blackbox: $f"
      if agent_file_result="$(execute_agent_test "$f" /tmp/agent-bb-out)"; then
        agent_rc=0
      else
        agent_rc=$?
      fi
      if [ "$agent_rc" -eq 0 ]; then
        printf 'PRISM_AGENT_TESTS_BEGIN {"protocol_version":2,"generated":1,"passed":1,"failed":0,"passed_count":1,"files":{"%s":"pass"},"file_results":{%s}} PRISM_AGENT_TESTS_END\n' "$(basename "$f")" "$agent_file_result"
      else
        printf '%s\n' "agent test failed: $f" >&2
        tail -c 2000 /tmp/agent-bb-out >&2 || true
        printf 'PRISM_AGENT_TESTS_BEGIN {"protocol_version":2,"generated":1,"passed":0,"failed":1,"passed_count":0,"files":{"%s":"fail"},"file_results":{%s}} PRISM_AGENT_TESTS_END\n' "$(basename "$f")" "$agent_file_result"
        return 1
      fi
      return 0
    fi
  done
  printf '%s\n' 'PRISM_AGENT_TESTS_BEGIN {"protocol_version":2,"generated":0,"passed":0,"failed":0,"passed_count":0,"files":{},"file_results":{}} PRISM_AGENT_TESTS_END'
  return 0
}

run_decompilation() {
  # 仅处理归档中已经安全解包出的 Android 制品；固定工具和固定参数，
  # 不接受用户或 Agent 提供的命令、镜像、网络、挂载和宿主路径。
  readonly jadx_version="${PRISM_JADX_VERSION:-1.5.6}"
  readonly max_decomp_files=20000
  readonly max_decomp_bytes=$((512 * 1024 * 1024))
  find . -type f \( -name '*.apk' -o -name '*.aab' -o -name '*.dex' \) \
    -not -path './_agent_tests/*' -not -path './.prism-*/*' -print \
    | sort > /tmp/prism-decomp-candidates
  if [ ! -s /tmp/prism-decomp-candidates ]; then
    printf '%s\n' 'PRISM_DECOMPILATION_JSON {"status":"skipped","tool":"none","candidate_count":0,"exit_code":0}'
    return 0
  fi
  : > /tmp/prism-decomp-input-manifest
  while IFS= read -r artifact; do
    [ -n "$artifact" ] || continue
    sha256sum "$artifact" >> /tmp/prism-decomp-input-manifest
  done < /tmp/prism-decomp-candidates
  input_sha="$(sha256sum /tmp/prism-decomp-input-manifest | cut -d' ' -f1)"
  input_artifact_sha256s="$(awk 'BEGIN { printf "[" } { if (NR > 1) printf ","; printf "\"%s\"", $1 } END { printf "]" }' /tmp/prism-decomp-input-manifest)"
  candidate_count="$(wc -l < /tmp/prism-decomp-input-manifest | tr -d ' ')"
  if [ ! -x /opt/jadx/bin/jadx ]; then
    printf 'PRISM_DECOMPILATION_JSON {"status":"failed","tool":"jadx","tool_version":"unknown","candidate_count":%s,"input_sha256":"%s","input_artifact_sha256s":%s,"exit_code":127,"reason":"tool_unavailable","log_ref":"worker.log","artifact_refs":["decompilation-manifest"]}\n' "$candidate_count" "$input_sha" "$input_artifact_sha256s"
    return 1
  fi
  decomp_root="./.prism-decompiled"
  rm -rf "$decomp_root"
  mkdir -p "$decomp_root"
  processed_count=0
  while IFS= read -r artifact; do
    [ -n "$artifact" ] || continue
    processed_count=$((processed_count + 1))
    output_dir="$decomp_root/$processed_count"
    mkdir -p "$output_dir"
    if timeout 180 /opt/jadx/bin/jadx \
      --output-dir "$output_dir" \
      --no-debug-info \
      --no-inline-anonymous \
      --show-bad-code \
      "$artifact" >"/tmp/prism-jadx-$processed_count.out" 2>&1; then
      :
    else
      rc=$?
      printf 'PRISM_DECOMPILATION_JSON {"status":"failed","tool":"jadx","tool_version":"%s","candidate_count":%s,"input_sha256":"%s","input_artifact_sha256s":%s,"exit_code":%s,"reason":"exit_nonzero","log_ref":"worker.log","artifact_refs":["decompilation-manifest"]}\n' "$jadx_version" "$candidate_count" "$input_sha" "$input_artifact_sha256s" "$rc"
      cat "/tmp/prism-jadx-$processed_count.out" >&2 || true
      return 1
    fi
  done < /tmp/prism-decomp-candidates
  source_count="$(find "$decomp_root" -type f \( -name '*.java' -o -name '*.kt' \) -print | wc -l | tr -d ' ')"
  output_bytes="$(find "$decomp_root" -type f \( -name '*.java' -o -name '*.kt' \) -printf '%s\n' | awk '{sum += $1} END {print sum + 0}')"
  if [ "$source_count" -gt "$max_decomp_files" ] || [ "$output_bytes" -gt "$max_decomp_bytes" ]; then
    printf 'PRISM_DECOMPILATION_JSON {"status":"failed","tool":"jadx","tool_version":"%s","candidate_count":%s,"input_sha256":"%s","input_artifact_sha256s":%s,"output_file_count":%s,"output_size_bytes":%s,"exit_code":65,"reason":"output_limit","log_ref":"worker.log","artifact_refs":["decompilation-manifest"]}\n' "$jadx_version" "$candidate_count" "$input_sha" "$input_artifact_sha256s" "$source_count" "$output_bytes"
    return 1
  fi
  if [ "$source_count" -le 0 ]; then
    printf 'PRISM_DECOMPILATION_JSON {"status":"failed","tool":"jadx","tool_version":"%s","candidate_count":%s,"input_sha256":"%s","input_artifact_sha256s":%s,"output_file_count":0,"output_size_bytes":0,"exit_code":65,"reason":"empty_output","log_ref":"worker.log","artifact_refs":["decompilation-manifest"]}\n' "$jadx_version" "$candidate_count" "$input_sha" "$input_artifact_sha256s"
    return 1
  fi
  find "$decomp_root" -type f \( -name '*.java' -o -name '*.kt' \) -print \
    | sort | while IFS= read -r file; do sha256sum "$file"; done \
    > /tmp/prism-decomp-manifest
  output_sha="$(sha256sum /tmp/prism-decomp-manifest | cut -d' ' -f1)"
  cp /tmp/prism-decomp-manifest ./decompilation-manifest
  printf 'PRISM_DECOMPILATION_JSON {"status":"succeeded","tool":"jadx","tool_version":"%s","candidate_count":%s,"input_sha256":"%s","input_artifact_sha256s":%s,"output_file_count":%s,"output_size_bytes":%s,"output_sha256":"%s","exit_code":0,"log_ref":"worker.log","artifact_refs":["decompilation-manifest"]}\n' "$jadx_version" "$candidate_count" "$input_sha" "$input_artifact_sha256s" "$source_count" "$output_bytes" "$output_sha"
  return 0
}

run_test() {
  if ! run_decompilation; then
    printf '%s\n' 'whitebox: decompilation failed (see logs)' >&2
    printf '%s\n' 'PRISM_WHITEBOX_DONE {"executed":true,"passed":false,"reason":"decompilation_failed"}'
    return 66
  fi
  # deploy 后自动测试链注入 _prism_verify.sh 时优先执行它(固定后端脚本,非任意命令)。
  # 反编译前置必须先完成,避免注入脚本绕过 Android 证据门禁。
  if [ -f ./_prism_verify.sh ]; then
    sh ./_prism_verify.sh whitebox
    return $?
  fi
  # 基础测试继续收集所有失败证据，但最终必须以非零退出码反映任何真实失败。
  source_present=0
  if find . -type f \( -name '*.py' -o -name '*.js' -o -name '*.ts' -o -name '*.java' -o -name '*.go' -o -name '*.php' \) \
      -not -path './_agent_tests/*' -not -path './.prism-*/*' -print -quit | grep -q .; then
    source_present=1
  fi
  if [ -d ./.prism-decompiled ] && find ./.prism-decompiled -type f \( -name '*.java' -o -name '*.kt' \) -print -quit | grep -q .; then
    source_present=1
  fi
  if [ "$source_present" -eq 0 ]; then
    printf '%s\n' 'PRISM_WHITEBOX_DONE {"executed":false,"reason":"no_source_files"}'
    return 66
  fi
  test_failed=0
  if [ -d ./.prism-decompiled ]; then
    if find ./.prism-decompiled -type f \( -name '*.java' -o -name '*.kt' \) -size 0 -print -quit | grep -q .; then
      printf '%s\n' 'whitebox: decompiled output contains empty source files' >&2
      test_failed=1
    elif grep -R -n 'JADX ERROR' ./.prism-decompiled >/tmp/prism-jadx-errors 2>/dev/null; then
      cat /tmp/prism-jadx-errors >&2
      printf '%s\n' 'whitebox: JADX emitted recovery errors' >&2
      test_failed=1
    else
      printf '%s\n' 'whitebox: JADX output static integrity check passed; project runtime not compiled' >&2
    fi
  fi
  case "$language" in
    python)
      if ! python -m compileall -q -x '(^|/)_agent_tests(/|$)' . 2>/dev/null; then
        printf '%s\n' 'whitebox: compileall reported errors (see logs)' >&2
        test_failed=1
      fi
      if find . -type f \( -name 'test_*.py' -o -name '*_test.py' \) \
          -not -path './_agent_tests/*' -print -quit | grep -q .; then
        if python -c 'import pytest' >/dev/null 2>&1; then
          if ! python -m pytest -q --disable-warnings --maxfail=50 --ignore=_agent_tests; then
            printf '%s\n' 'whitebox: pytest reported failures (see logs)' >&2
            test_failed=1
          fi
        else
          if ! python -m unittest discover -v; then
            printf '%s\n' 'whitebox: unittest reported failures (see logs)' >&2
            test_failed=1
          fi
        fi
      fi
      ;;
    node)
      if ! find . -type f -name '*.js' -not -path './node_modules/*' \
          -not -path './_agent_tests/*' -exec node --check '{}' ';' 2>/dev/null; then
        printf '%s\n' 'whitebox: node --check reported errors (see logs)' >&2
        test_failed=1
      fi
      if [ -f package.json ]; then
        if ! npm test --if-present; then
          printf '%s\n' 'whitebox: npm test reported failures (see logs)' >&2
          test_failed=1
        fi
      fi
      ;;
    java)
      if [ -f mvnw ]; then
        if ! sh ./mvnw -o -B test; then
          printf '%s\n' 'whitebox: mvn test reported failures (see logs)' >&2
          test_failed=1
        fi
      elif [ -f gradlew ]; then
        if ! sh ./gradlew --offline --no-daemon test; then
          printf '%s\n' 'whitebox: gradle test reported failures (see logs)' >&2
          test_failed=1
        fi
    else
        find . -type f -name '*.java' -not -path './_agent_tests/*' -print > /tmp/prism-java-sources
        if [ -s /tmp/prism-java-sources ]; then
          if [ ! -d ./.prism-decompiled ]; then
            mkdir -p /workspace/.prism-classes
            if ! javac -d /workspace/.prism-classes @/tmp/prism-java-sources 2>/dev/null; then
              printf '%s\n' 'whitebox: javac reported errors (see logs)' >&2
              test_failed=1
            fi
          fi
        fi
      fi
      ;;
    go)
      if go_packages="$(go list ./... 2>/tmp/prism-go-list.err)"; then
        go_packages="$(printf '%s\n' "$go_packages" | grep -v '/_agent_tests$' || true)"
        if [ -n "$go_packages" ]; then
          if [ -d vendor ]; then
            printf '%s\n' "$go_packages" | xargs go test -mod=vendor || test_failed=1
          else
            printf '%s\n' "$go_packages" | xargs go test || test_failed=1
          fi
        fi
      else
        cat /tmp/prism-go-list.err >&2 || true
        test_failed=1
      fi
      if [ "$test_failed" -ne 0 ]; then
        printf '%s\n' 'whitebox: go test reported failures (see logs)' >&2
      fi
      ;;
    php)
      # 分批语法检查:PHP8 对老库的 Fatal/Parse error 是真实语法问题,Deprecated/Warning 是提示级。
      # 所有输出仍进入日志供多Agent审查引用,但只把 Fatal/Parse/Errors parsing 判为失败。
      find . -type f -name '*.php' -not -path './_agent_tests/*' -print0 | xargs -0 -n 50 -r php -l 2>&1 \
        | grep -v 'No syntax errors detected' > /tmp/.lint_all || true
      if [ -s /tmp/.lint_all ]; then
        cat /tmp/.lint_all
        if grep -E 'Fatal error|Parse error|Errors parsing' /tmp/.lint_all > /tmp/.lint_fatal; then
          cat /tmp/.lint_fatal >&2
          printf '%s\n' 'whitebox: php lint reported fatal/parse errors (see logs)' >&2
          test_failed=1
        else
          printf '%s\n' 'whitebox: php lint reported warnings (see logs)' >&2
        fi
      fi
      if [ -f vendor/bin/phpunit ]; then
        if ! php vendor/bin/phpunit --colors=never; then
          printf '%s\n' 'whitebox: phpunit reported failures (see logs)' >&2
          test_failed=1
        fi
      fi
      ;;
  esac
  if ! run_agent_tests; then
    test_failed=1
  fi
  collect_facts
  if [ "$test_failed" -eq 0 ]; then
    printf '%s\n' 'PRISM_WHITEBOX_DONE {"executed":true,"passed":true}'
  else
    printf '%s\n' 'PRISM_WHITEBOX_DONE {"executed":true,"passed":false}'
  fi
  return "$test_failed"
}

prepare_deps() {
  # 离线补全项目依赖(尽力而为):仅用镜像内置缓存或项目 vendor,不联网。
  # 补全结果不影响主流程;缺失依赖由后端部署核验 agent 记录并在报告中提示。
  case "$language" in
    python)
      if [ -f requirements.txt ]; then
        python -m pip install -q --no-index -r requirements.txt 2>/dev/null || \
          printf '%s\n' 'PRISM_DEPS python requirements partial (offline)' >&2
      fi
      ;;
    node)
      if [ -d node_modules ]; then :;
      elif [ -f package.json ]; then
        npm ci --offline --ignore-scripts 2>/dev/null || \
          printf '%s\n' 'PRISM_DEPS node offline install partial' >&2
      fi
      ;;
    go|java|php) : ;;
  esac
  return 0
}

run_deploy() {
  # 后端部署核验 agent 生成的补全启动脚本优先(受控注入,固定由本 runner 调用)
  if [ -f ./_prism_launch.sh ]; then
    exec sh ./_prism_launch.sh
  fi
  export HOST=127.0.0.1
  export PORT="$preview_port"
  export SERVER_PORT="$preview_port"
  case "$language" in
    python)
      if [ -f app.py ] && python -c 'import flask' >/dev/null 2>&1; then
        exec python -m flask --app app run --host 127.0.0.1 --port "$preview_port"
      elif [ -f main.py ]; then
        exec python main.py
      elif [ -f app.py ]; then
        exec python app.py
      fi
      printf '%s\n' 'no supported Python deployment entry was detected' >&2
      exit 66
      ;;
    node)
      [ -f package.json ] || { printf '%s\n' 'package.json is required' >&2; exit 66; }
      exec npm start --if-present
      ;;
    java)
      jar_path="$(find . -type f -name '*.jar' -not -name '*-sources.jar' -not -name '*-javadoc.jar' -print -quit)"
      if [ -z "$jar_path" ] && [ -f mvnw ]; then
        sh ./mvnw -o -B -DskipTests package
        jar_path="$(find target -type f -name '*.jar' -not -name '*-sources.jar' -print -quit)"
      elif [ -z "$jar_path" ] && [ -f gradlew ]; then
        sh ./gradlew --offline --no-daemon build -x test
        jar_path="$(find build/libs -type f -name '*.jar' -print -quit)"
      fi
      [ -n "$jar_path" ] || { printf '%s\n' 'no runnable Java archive was detected' >&2; exit 66; }
      exec java -Dserver.address=127.0.0.1 -Dserver.port="$preview_port" -jar "$jar_path"
      ;;
    go)
      exec go run .
      ;;
    php)
      # 与 sandbox_service 内嵌 runner 一致:顶层入口优先,空 public 不抢占,
      # 嵌套包(zip 多套一层目录)递归下探唯一候选。
      document_root=.
      if [ -f ./index.php ] || [ -f ./index.html ]; then
        document_root=.
      elif [ -d public ] && { [ -f public/index.php ] || [ -f public/index.html ]; }; then
        document_root=public
      else
        nested_root=""
        nested_count=0
        for directory in */; do
          [ -d "$directory" ] || continue
          case "$directory" in .*|prism-tmp/*|prism-home/*|prism-cache/*) continue ;; esac
          candidate=""
          if [ -f "${directory}index.php" ] || [ -f "${directory}index.html" ]; then
            candidate="${directory%/}"
          elif [ -f "${directory}public/index.php" ] || [ -f "${directory}public/index.html" ]; then
            candidate="${directory%/}/public"
          fi
          [ -n "$candidate" ] || continue
          nested_root="$candidate"
          nested_count=$((nested_count + 1))
        done
        if [ "$nested_count" -eq 1 ]; then
          document_root="$nested_root"
        fi
      fi
      exec php -S "127.0.0.1:$preview_port" -t "$document_root"
      ;;
  esac
}

run_blackbox() {
  # deploy 后自动测试链注入 _prism_verify.sh 时优先执行它(固定后端脚本,非任意命令)。
  if [ -f ./_prism_verify.sh ]; then
    sh ./_prism_verify.sh blackbox
    return $?
  fi
  run_deploy &
  app_pid="$!"
  trap 'kill "$app_pid" >/dev/null 2>&1 || true' EXIT INT TERM
  attempts=0
  stable=0
  http_ready=""
  http_status=""
  # 完整部署核验:先离线补全依赖,再启动应用;服务"稳定运行"= 连续 3 次探活
  # 返回任何合法 HTTP 状态行(含 5xx)。5xx 说明服务已起来,只是应用自身(如缺DB/
  # 配置)报错——这是运行态证据,不该判黑盒失败;真正的失败是"一直没监听/
  # 进程死了/完全无响应"。
  prepare_deps
  while [ "$attempts" -lt 45 ]; do
    if status="$(bash -c '
      port="$1"
      exec 3<>"/dev/tcp/127.0.0.1/$port"
      printf "GET / HTTP/1.0\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n" >&3
      IFS= read -r line <&3
      case "$line" in
        HTTP/*\ [1-5][0-9][0-9]\ *)
          code="${line#HTTP/* }"
          printf "%s" "${code%% *}"
          ;;
        *) exit 1 ;;
      esac
    ' prism-health "$preview_port" 2>/dev/null)"; then
      http_status="$status"
      stable=$((stable + 1))
      if [ "$stable" -ge 3 ]; then
        http_ready=1
        break
      fi
    else
      stable=0
    fi
    if ! kill -0 "$app_pid" >/dev/null 2>&1; then
      wait "$app_pid"
      return "$?"
    fi
    attempts=$((attempts + 1))
    sleep 1
  done
  if [ -z "$http_ready" ]; then
    printf '%s\n' 'application did not become ready on the fixed loopback port' >&2
    return 1
  fi
  printf 'blackbox loopback status=%s\n' "$http_status"
  collect_facts
  # ── v3.4 真实 PoC 验证 ──
  # 服务已在固定 loopback 端口就绪。若源码携带审计平台生成的 _prism_poc.sh,
  # 在隔离环境内执行它(发起 PoC 请求拿真实响应),输出 PRISM_POC_RESULT 供后端
  # 经 docker logs 回收判定。PoC 由后端按 CRUD 数据隔离红线生成,只读写自身
  # 创建的数据;脚本缺失或失败不阻断黑盒就绪结论。
  if [ -f ./_prism_poc.sh ]; then
    printf '%s\n' 'prism poc script detected, executing'
    PRISM_POC_PORT="$preview_port" sh ./_prism_poc.sh || printf '%s\n' 'prism poc script exited non-zero'
  else
    printf '%s\n' 'no _prism_poc.sh present, skip poc execution'
  fi
  # agent 动态黑盒:应用仍在运行,执行 agent 生成的回环测试脚本(必须放在 kill 之前)
  blackbox_failed=0
  case "$http_status" in
    5*) printf '%s\n' 'blackbox: application returned a 5xx response' >&2; blackbox_failed=1 ;;
  esac
  if ! run_agent_blackbox; then
    blackbox_failed=1
  fi
  kill "$app_pid" >/dev/null 2>&1 || true
  wait "$app_pid" 2>/dev/null || true
  trap - EXIT INT TERM
  return "$blackbox_failed"
}

run_combined() {
  combined_failed=0
  if ! run_test; then
    combined_failed=1
  fi
  if ! run_blackbox; then
    combined_failed=1
  fi
  return "$combined_failed"
}

collect_facts() {
  # 输出 PRISM_FACTS_BEGIN/END 供后端回收 recon_facts(入口/端点/密钥/参数提示)
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PYF' 2>/dev/null || true
import json, os, re
facts = {"entrypoints": [], "test_files": {"found": 0, "framework": ""},
         "endpoints": [], "param_hints": [], "hardcoded_secrets": []}
entry_names = {"main.py", "app.py", "manage.py", "wsgi.py", "asgi.py", "index.js",
               "server.js", "app.js", "main.go", "go.mod", "pom.xml", "index.php", "index.html"}
test_re = re.compile(r"(^test_.*\.py$|.*_test\.py$|.*\.test\.js$|.*_test\.go$|Test\.java$)")
route_re = re.compile(
    r"(?:route|get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]|"
    r"@(?:app|bp|router)\.(?:route|get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]|"
    r"path\s*\(\s*['\"]([^'\"]+)['\"]", re.I)
secret_re = re.compile(r"(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['\"]([^'\"]{6,})['\"]", re.I)
param_names = {"file", "path", "filename", "download", "url", "callback", "id", "userid",
               "orderid", "template", "export", "redirect", "next", "upload"}
endpoints, secrets, params, tests = [], [], set(), 0
framework = ""
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in {".git", "_agent_tests", "node_modules", "__pycache__", "venv", ".venv", "vendor"}]
    for fn in files:
        p = os.path.join(root, fn)
        if fn in entry_names:
            facts["entrypoints"].append(p.lstrip("./"))
        if test_re.search(fn):
            tests += 1
            if fn.endswith(".py"): framework = framework or "pytest"
            if fn.endswith(".js"): framework = framework or "jest"
        if not fn.endswith((".py", ".js", ".ts", ".go", ".java", ".php")):
            continue
        try:
            with open(p, "r", errors="ignore") as fh:
                src = fh.read(200000)
        except OSError:
            continue
        for m in route_re.finditer(src):
            ep = m.group(1) or m.group(2) or m.group(3)
            if ep and ep.startswith("/") and len(endpoints) < 60:
                endpoints.append({"path": ep, "file": p.lstrip("./")})
        for m in secret_re.finditer(src):
            if len(secrets) < 20:
                secrets.append({"file": p.lstrip("./"), "kind": m.group(1)})
        for name in param_names:
            if re.search(r"[?&\"'\s]" + name + r"['\"=:\\s]", src, re.I):
                params.add(name)
facts["test_files"] = {"found": tests, "framework": framework}
facts["endpoints"] = endpoints
facts["hardcoded_secrets"] = secrets
facts["param_hints"] = sorted(params)
print("PRISM_FACTS_BEGIN")
print(json.dumps(facts, ensure_ascii=False))
print("PRISM_FACTS_END")
PYF
  elif command -v php >/dev/null 2>&1; then
    cat > /tmp/_facts_runner.php <<'PHPF'
<?php
try {
$facts = array("entrypoints"=>array(), "test_files"=>array("found"=>0,"framework"=>""), "endpoints"=>array(), "param_hints"=>array(), "hardcoded_secrets"=>array());
$entry_names = array("main.py","app.py","manage.py","wsgi.py","asgi.py","index.js","server.js","app.js","main.go","go.mod","pom.xml","index.php","index.html");
$route_re = "/(?:route|get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]|@(?:app|bp|router)\.(?:route|get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]|path\s*\(\s*['\"]([^'\"]+)['\"]/i";
$secret_re = "/(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['\"]([^'\"]{6,})['\"]/i";
$param_names = array("file","path","filename","download","url","callback","id","userid","orderid","template","export","redirect","next","upload");
$tests = 0; $framework = ""; $endpoints = array(); $secrets = array(); $params = array();
$scanned = 0;
$it = new RecursiveIteratorIterator(new RecursiveDirectoryIterator("."));
foreach ($it as $f) {
  if ($f->isDir()) continue;
  if ($scanned++ > 3000) break;
	  $name = $f->getFilename(); $rel = substr($f->getPathname(), 2);
	  if (strpos($rel, "_agent_tests/") === 0) continue;
  if (in_array($name, $entry_names)) $facts["entrypoints"][] = $rel;
  if (preg_match("/^(test_.*\.py$|.*_test\.py$|.*\.test\.js$|.*_test\.go$|Test\.java$)/", $name)) { $tests++; if (substr($name,-3)===".py") $framework = $framework ?: "pytest"; if (substr($name,-3)===".js") $framework = $framework ?: "jest"; }
  $ext = strtolower(pathinfo($name, PATHINFO_EXTENSION));
  if (!in_array($ext, array("py","js","ts","go","java","php"))) continue;
  $src = @file_get_contents($f->getPathname());
  if ($src === false) continue;
  $src = substr($src, 0, 200000);
  if (preg_match_all($route_re, $src, $mm)) {
    foreach (array_merge($mm[1], $mm[2], $mm[3]) as $ep) {
      if ($ep && $ep[0] === "/" && count($endpoints) < 60) $endpoints[] = array("path"=>$ep, "file"=>$rel);
    }
  }
  if (preg_match_all($secret_re, $src, $sm)) {
    foreach ($sm[1] as $k) { if (count($secrets) < 20) $secrets[] = array("file"=>$rel, "kind"=>$k); }
  }
  foreach ($param_names as $pn) {
    if (preg_match("/[?&'\"\\s]" . preg_quote($pn, "/") . "['\"=:\\s]/i", $src)) $params[$pn] = 1;
  }
}
$facts["test_files"] = array("found"=>$tests, "framework"=>$framework);
$facts["endpoints"] = $endpoints;
$facts["hardcoded_secrets"] = $secrets;
$facts["param_hints"] = array_keys($params);
echo "PRISM_FACTS_BEGIN\n" . json_encode($facts, JSON_INVALID_UTF8_SUBSTITUTE | JSON_PARTIAL_OUTPUT_ON_ERROR) . "\nPRISM_FACTS_END\n";
} catch (Throwable $e) { echo "PRISM_FACTS_BEGIN\n{\"error\":\"" . addslashes($e->getMessage()) . "\"}\nPRISM_FACTS_END\n"; }
PHPF
    php /tmp/_facts_runner.php 2>/dev/null || echo "PRISM_FACTS_BEGIN
{"error":"php facts runner failed"}
PRISM_FACTS_END"
  fi
}

if [ "$action" = test ]; then
  case "$test_mode" in
    whitebox) run_test ;;
    blackbox) run_blackbox ;;
    combined) run_combined ;;
  esac
else
  run_deploy
fi
