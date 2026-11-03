# Windows wrapper. The installer itself is Python.
Set-StrictMode -Version Latest
Set-Location $PSScriptRoot
python setup.py @args
if ($LASTEXITCODE -ne $null) { exit $LASTEXITCODE }
