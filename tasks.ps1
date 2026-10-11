<#
.SYNOPSIS
  Windows equivalent of the Makefile (make isn't installed by default on Windows).
.EXAMPLE
  ./tasks.ps1 test
  ./tasks.ps1 lint
#>
param([Parameter(Mandatory = $true)][ValidateSet('dev','worker','scheduler','test','lint','eval','eval-demo','migrate','build')][string]$Task)

$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

switch ($Task) {
  'dev'     { & $py -m uvicorn app.main:create_app --factory --reload --no-access-log --port 8080 }
  'worker'  { & $py -m app worker }
  'scheduler' { & $py -m app scheduler }
  'test'    { & $py -m pytest }
  'lint'    {
    & $py -m ruff check app tests evals scripts alembic; if (-not $?) { exit 1 }
    & $py -m ruff format --check app tests evals scripts alembic; if (-not $?) { exit 1 }
    & $py -m mypy app tests evals scripts
  }
  'eval'      { & $py -m evals.runner }
  'eval-demo' { & $py -m evals.runner --cases examples/demo-clinic/eval-cases.yaml }
  'migrate' { & $py -m alembic upgrade head }
  'build'   { docker build -f deploy/docker/Dockerfile -t whatsapp-ai-agent:dev . }
}
exit $LASTEXITCODE
