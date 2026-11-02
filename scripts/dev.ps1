# DocuMind local commands (Windows PowerShell). Usage: .\scripts\dev.ps1 <command>
#   up       build + start db, migrate+seed, api, web
#   test     unit + security tests (needs `up`)
#   down     stop containers (keeps data)
#   reset    stop and DELETE local db volume
#   observe  like `up`, plus Jaeger tracing UI at http://localhost:16686
#   eval     golden-set quality run; writes docs/benchmarks/golden-eval-latest.md
#   ai       real local models (Ollama): nomic-embed-text + qwen2.5:1.5b golden-set report
#   logs     follow api logs
param([Parameter(Position = 0)][string]$Cmd = "help")
# Not "Stop": PowerShell 5.1 treats native stderr progress output (docker) as errors.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Compose = @("compose", "--env-file", "$Root\.env", "-f", "$Root\infra\compose\docker-compose.yml")
if (-not (Test-Path "$Root\.env")) { Copy-Item "$Root\.env.example" "$Root\.env" }

function Venv {
    $py = "$Root\services\core-api\.venv\Scripts\python.exe"
    if (-not (Test-Path $py)) {
        python -m venv "$Root\services\core-api\.venv"
        & $py -m pip install -q -e "$Root\services\core-api[dev]"
    }
    $py
}

switch ($Cmd) {
    "up"    { docker @Compose up -d --build --wait db azurite oidc api worker web; docker @Compose logs migrate
              "Web: http://localhost:5173   API docs: http://localhost:8000/docs" }
    # The worker container is paused so integration tests drive the queue deterministically.
    "test"  { $py = Venv; docker @Compose stop worker; Push-Location "$Root\services\core-api"
              try { & $py -m pytest -q } finally { Pop-Location; docker @Compose start worker } }
    "observe" { $env:OTEL_ENDPOINT = "http://jaeger:4318"
              docker @Compose --profile observability up -d --build --wait db azurite oidc jaeger api worker web
              "Traces: http://localhost:16686   Web: http://localhost:5173" }
    "eval"  { $py = Venv; docker @Compose stop worker; Push-Location "$Root\services\core-api"
              $env:EVAL_REPORT = "$Root\docs\benchmarks\golden-eval-latest.md"
              try { & $py -m pytest tests/integration/test_eval_golden.py -q -s }
              finally { Remove-Item Env:EVAL_REPORT; Pop-Location; docker @Compose start worker } }
    # Real local models: starts Ollama, pulls models, runs the golden set (report-only).
    "ai"    { docker @Compose --profile ai up -d ollama
              docker @Compose exec -T ollama ollama pull nomic-embed-text
              docker @Compose exec -T ollama ollama pull qwen2.5:1.5b
              $py = Venv; docker @Compose stop worker; Push-Location "$Root\services\core-api"
              $env:OLLAMA_URL = "http://localhost:11434"; $env:EMBEDDING_PROVIDER = "ollama"
              $env:LLM_PROVIDER = "ollama"; $env:OLLAMA_MODEL = "qwen2.5:1.5b"
              $env:EVAL_REPORT = "$Root\docs\benchmarks\golden-eval-nomic-qwen.md"
              try { & $py -m pytest tests/integration/test_eval_golden.py -q -s }
              finally { Remove-Item Env:OLLAMA_URL, Env:EMBEDDING_PROVIDER, Env:LLM_PROVIDER,
                                    Env:OLLAMA_MODEL, Env:EVAL_REPORT
                        Pop-Location; docker @Compose start worker } }
    "down"  { docker @Compose down }
    "reset" { docker @Compose --profile observability down -v }
    "logs"  { docker @Compose logs -f api }
    default { Get-Content $PSCommandPath -TotalCount 10 }
}
