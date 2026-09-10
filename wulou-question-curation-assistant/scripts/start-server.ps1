$projectRoot = Resolve-Path "$PSScriptRoot\.."

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "未找到 uv。请先安装 uv，再在项目目录执行 uv sync。"
    exit 1
}

& uv run --project $projectRoot python (Join-Path $projectRoot "server\main.py")
