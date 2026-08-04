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
"*|*""*) exit 64 ;;
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

run_test() {
  # deploy 后自动测试链注入 _prism_verify.sh 时优先执行它(固定后端脚本,非任意命令)。
  if [ -f ./_prism_verify.sh ]; then
    sh ./_prism_verify.sh whitebox
    return $?
  fi
  case "$language" in
    python)
      python -m compileall -q .
      if find . -type f \( -name 'test_*.py' -o -name '*_test.py' \) -print -quit | grep -q .; then
        if python -c 'import pytest' >/dev/null 2>&1; then
          python -m pytest -q --disable-warnings --maxfail=50
        else
          python -m unittest discover -v
        fi
      fi
      ;;
    node)
      find . -type f -name '*.js' -not -path './node_modules/*' -exec node --check '{}' ';'
      if [ -f package.json ]; then
        npm test --if-present
      fi
      ;;
    java)
      if [ -f mvnw ]; then
        sh ./mvnw -o -B test
      elif [ -f gradlew ]; then
        sh ./gradlew --offline --no-daemon test
      else
        find . -type f -name '*.java' -print > /tmp/prism-java-sources
        if [ -s /tmp/prism-java-sources ]; then
          mkdir -p /workspace/.prism-classes
          javac -d /workspace/.prism-classes @/tmp/prism-java-sources
        fi
      fi
      ;;
    go)
      if [ -d vendor ]; then
        go test -mod=vendor ./...
      else
        go test ./...
      fi
      ;;
    php)
      find . -type f -name '*.php' -exec php -l '{}' ';'
      if [ -f vendor/bin/phpunit ]; then
        php vendor/bin/phpunit --colors=never
      fi
      ;;
  esac
}

run_deploy() {
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
      document_root=.
      [ ! -d public ] || document_root=public
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
  while [ "$attempts" -lt 45 ]; do
    if status="$(bash -c '
      port="$1"
      exec 3<>"/dev/tcp/127.0.0.1/$port"
      printf "GET / HTTP/1.0\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n" >&3
      IFS= read -r line <&3
      case "$line" in
        HTTP/*\ [23][0-9][0-9]\ *)
          code="${line#HTTP/* }"
          printf "%s" "${code%% *}"
          ;;
        *) exit 1 ;;
      esac
    ' prism-health "$preview_port" 2>/dev/null)"; then
      case "$status" in
        [23][0-9][0-9])
          printf 'blackbox loopback status=%s\n' "$status"
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
          kill "$app_pid" >/dev/null 2>&1 || true
          wait "$app_pid" 2>/dev/null || true
          trap - EXIT INT TERM
          return 0
          ;;
      esac
    fi
    if ! kill -0 "$app_pid" >/dev/null 2>&1; then
      wait "$app_pid"
      return "$?"
    fi
    attempts=$((attempts + 1))
    sleep 1
  done
  printf '%s\n' 'application did not become ready on the fixed loopback port' >&2
  return 1
}

if [ "$action" = test ]; then
  case "$test_mode" in
    whitebox) run_test ;;
    blackbox) run_blackbox ;;
    combined) run_test; run_blackbox ;;
  esac
else
  run_deploy
fi
