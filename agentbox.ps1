# agentbox: run a coding agent in an isolated container with only the current folder mounted.
# Usage: agentbox [command...]   (default: bash)   e.g. `agentbox claude`

$engine     = if ($env:AGENTBOX_ENGINE)      { $env:AGENTBOX_ENGINE }      else { 'docker' }
$image      = if ($env:AGENTBOX_IMAGE)       { $env:AGENTBOX_IMAGE }       else { 'agent-box' }
$homeVolume = if ($env:AGENTBOX_HOME_VOLUME) { $env:AGENTBOX_HOME_VOLUME } else { 'agent-box-home' }
$envFile    = if ($env:AGENTBOX_ENV_FILE)    { $env:AGENTBOX_ENV_FILE }    else { Join-Path $HOME '.config\agent-box\env' }

# Container name from the folder name, made safe for Docker
$project = (Get-Location).ProviderPath
$name = (Split-Path -Leaf $project) -replace '[^a-zA-Z0-9_.-]', '-'
if (-not $name) { $name = 'root' }

$dockerArgs = @(
  'run', '-it', '--rm',
  '--name', "agent-box-$name",
  '--hostname', "agent-box-$name",
  '-v', "${homeVolume}:/home/agent-box",
  '-v', "${project}:/workspace/$name",
  '-w', "/workspace/$name"
)

if (Test-Path $envFile) {
  $dockerArgs += @('--env-file', $envFile)
}

# Extra engine flags, split on whitespace, e.g. $env:AGENTBOX_OPTS = "-p 3000:3000"
if ($env:AGENTBOX_OPTS) {
  $dockerArgs += $env:AGENTBOX_OPTS -split '\s+' | Where-Object { $_ }
}

$command = if ($args.Count -gt 0) { $args } else { @('bash') }

& $engine @dockerArgs $image @command
exit $LASTEXITCODE
